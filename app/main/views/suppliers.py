from datetime import datetime

from flask import abort, current_app, jsonify, request, url_for
from sqlalchemy.exc import IntegrityError, DataError
from sqlalchemy.sql.expression import true
from sqlalchemy.sql import cast
from sqlalchemy import Boolean

from .. import main
from ... import db
from ...models import (
    Supplier, AuditEvent, SupplierFramework, Framework, PriceSchedule, User, Domain, Application,
    ServiceRole, SupplierDomain
)

from sqlalchemy.sql import func, desc, or_, asc
from functools import reduce

from app.utils import (
    get_json_from_request, get_nonnegative_int_or_400, get_positive_int_or_400,
    get_valid_page_or_1, json_has_required_keys, pagination_links,
    validate_and_return_updater_request
)
from ...supplier_utils import validate_agreement_details_data
from dmapiclient.audit import AuditTypes
from dmutils.logging import notify_team
import json


@main.route('/suppliers', methods=['GET'])
def list_suppliers():
    page = get_valid_page_or_1()

    prefix = request.args.get('prefix', '')
    name = request.args.get('name', None)

    results_per_page = get_positive_int_or_400(
        request.args,
        'per_page',
        current_app.config['DM_API_SUPPLIERS_PAGE_SIZE']
    )

    if name is None:
        suppliers = Supplier.query.filter(Supplier.abn.is_(None) | (Supplier.abn != Supplier.DUMMY_ABN))
    else:
        suppliers = Supplier.query.filter((Supplier.name == name) | (Supplier.long_name == name))

    if prefix:
        if prefix == 'other':
            suppliers = suppliers.filter(
                Supplier.name.op('~')('^[^A-Za-z]'))
        else:
            # case insensitive LIKE comparison for matching supplier names
            suppliers = suppliers.filter(
                Supplier.name.ilike(prefix + '%'))

    suppliers = suppliers.distinct(Supplier.name, Supplier.code)

    try:
        if results_per_page > 0:
            paginator = suppliers.paginate(
                page=page,
                per_page=results_per_page,
            )
            links = pagination_links(
                paginator,
                '.list_suppliers',
                request.args
            )
            supplier_results = paginator.items
        else:
            links = {
                'self': url_for('.list_suppliers', _external=True, **request.args),
            }
            supplier_results = suppliers.all()
        supplier_data = [supplier.serializable for supplier in supplier_results]
    except DataError:
        abort(400, 'invalid framework')
    return jsonify(suppliers=supplier_data, links=links)


@main.route('/suppliers/<int:code>', methods=['GET'])
def get_supplier(code):
    supplier = Supplier.query.filter(
        Supplier.code == code
    ).first_or_404()

    supplier.get_service_counts()
    return jsonify(supplier=supplier.serializable)


@main.route('/suppliers/<int:code>', methods=['DELETE'])
def delete_supplier(code):
    supplier = Supplier.query.filter(
        Supplier.code == code
    ).first_or_404()

    try:
        db.session.delete(supplier)
        db.session.commit()
    except TransportError, e:
        return jsonify(message=str(e)), e.status_code
    except IntegrityError as e:
        db.session.rollback()
        return jsonify(message="Database Error: {0}".format(e)), 400

    return jsonify(message="done"), 200


@main.route('/suppliers', methods=['DELETE'])
def delete_suppliers():
    try:
        Supplier.query.delete()
        db.session.commit()
    except TransportError, e:
        return jsonify(message=str(e)), e.status_code
    except IntegrityError as e:
        db.session.rollback()
        return jsonify(message="Database Error: {0}".format(e)), 400

    return jsonify(message="done"), 200


@main.route('/suppliers/count', methods=['GET'])
def get_suppliers_stats():
    suppliers = {
        "total": Supplier.query.filter(Supplier.abn != Supplier.DUMMY_ABN).count()
    }

    return jsonify(suppliers=suppliers)


@main.route('/suppliers/search', methods=['GET'])
def supplier_search():
    search_query = get_json_from_request()

    new_domains = False

    offset = get_nonnegative_int_or_400(request.args, 'from', 0)
    result_count = get_positive_int_or_400(request.args, 'size', current_app.config['DM_API_SUPPLIERS_PAGE_SIZE'])

    try:
        sort_dir = search_query['sort'][0].values()[0]['order']
    except (KeyError, IndexError):
        sort_dir = 'asc'

    try:
        sort_by = search_query['sort'][0].values()[0]['sort_by']
    except (KeyError, IndexError):
        sort_by = None

    try:
        terms = search_query['query']['filtered']['filter']['terms']
    except (KeyError, IndexError):
        terms = {}

    roles_list = None
    seller_types_list = None

    if terms:
        new_domains = 'prices.serviceRole.role' not in terms

        try:
            if new_domains:
                roles_list = terms['domains.assessed']
            else:
                roles = terms['prices.serviceRole.role']
                roles_list = set(_['role'][7:] for _ in roles)
        except KeyError:
            pass

        try:
            seller_types_list = terms['seller_types']
        except:
            pass

    try:
        search_term = search_query['query']['match_phrase_prefix']['name']
    except KeyError:
        search_term = ''

    EXCLUDE_LEGACY_ROLES = not current_app.config['LEGACY_ROLE_MAPPING']

    if new_domains:
        q = db.session.query(Supplier).outerjoin(SupplierDomain).outerjoin(Domain)
    else:
        q = db.session.query(Supplier).outerjoin(PriceSchedule).outerjoin(ServiceRole)

    try:
        code = search_query['query']['term']['code']
        q = q.filter(Supplier.code == code)
    except KeyError:
        pass

    if roles_list is not None:
        if new_domains:
            if EXCLUDE_LEGACY_ROLES:
                # can use this more efficient and faster code once
                # lecacy roles have been fully migrated
                condition = reduce(or_, ((Domain.name == _) for _ in roles_list))
                q = q.filter(condition)
        else:
            condition = reduce(or_, (ServiceRole.name.like('%{}'.format(_)) for _ in roles_list))
            q = q.filter(condition)

    if seller_types_list is not None:
        def is_seller_type(typecode):
            return cast(Supplier.data[('seller_type', typecode)].astext, Boolean) == True  # noqa

        condition = reduce(or_, (is_seller_type(_) for _ in seller_types_list))
        q = q.filter(condition)

    if sort_by:
        if sort_by == 'latest':
            ob = [desc(Supplier.last_update_time)]
        else:
            ob = [asc(Supplier.name)]
    else:
        if sort_dir == 'desc':
            ob = [desc(Supplier.name)]
        else:
            ob = [asc(Supplier.name)]

    if search_term:
        ob = [
            desc(
                func.similarity(
                    search_term,
                    Supplier.name)
            ),
            desc(
                func.similarity(
                    search_term,
                    Supplier.summary)
            )
        ] + ob

    q = q.order_by(*ob)

    if search_term:
        NAME_MINIMUM = \
            current_app.config['SEARCH_MINIMUM_MATCH_SCORE_NAME']
        SUMMARY_MINIMUM = \
            current_app.config['SEARCH_MINIMUM_MATCH_SCORE_SUMMARY']

        condition = or_(
            func.similarity(search_term, Supplier.name) > NAME_MINIMUM,
            func.similarity(search_term, Supplier.summary) >= SUMMARY_MINIMUM
        )

        q = q.filter(condition)

    results = list(q)

    # remove 'hidden' example listing from result
    results = [_ for _ in results if _.abn != _.DUMMY_ABN]

    if roles_list and new_domains and not EXCLUDE_LEGACY_ROLES:
        # this code includes lecacy domains in results but is slower.
        # can be removed once fully migrated to new domains.
        results = [
            _ for _ in results
            if (
                set(_.assessed_domains) & set(roles_list)
            )
        ]

    sliced_results = results[offset:offset+result_count]

    result = {
        'hits': {
            'total': len(results),
            'hits': [{'_source': r} for r in sliced_results]
        }
    }

    try:
        return jsonify(result), 200
    except Exception as e:
        return jsonify(message=str(e)), 500


def update_supplier_data_impl(supplier, supplier_data, success_code):
    try:
        if 'prices' in supplier_data:
            db.session.query(PriceSchedule).filter(PriceSchedule.supplier_id == supplier.id).delete()

        supplier.update_from_json(supplier_data)

        db.session.add(supplier)
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        return jsonify(message="Database Error: {0}".format(e)), 400

    return jsonify(supplier=supplier.serializable), success_code


@main.route('/suppliers', methods=['POST'])
def create_supplier():
    request_data = get_json_from_request()
    if 'supplier' in request_data:
        supplier_data = request_data.get('supplier')
    else:
        abort(400)

    supplier = Supplier()
    return update_supplier_data_impl(supplier, supplier_data, 201)


@main.route('/suppliers/<int:code>', methods=['POST', 'PATCH'])
def update_supplier(code):
    request_data = get_json_from_request()
    if 'supplier' in request_data:
        supplier_data = request_data.get('supplier')
    else:
        abort(400)

    if request.method == 'POST':
        supplier = Supplier(code=code)
    else:
        assert request.method == 'PATCH'
        supplier = Supplier.query.filter(
            Supplier.code == code
        ).first_or_404()

    return update_supplier_data_impl(supplier, supplier_data, 200)


@main.route('/suppliers/<int:code>/frameworks/<framework_slug>/declaration', methods=['PUT'])
def set_a_declaration(code, framework_slug):
    framework = Framework.query.filter(
        Framework.slug == framework_slug
    ).first_or_404()

    supplier_framework = SupplierFramework.find_by_supplier_and_framework(
        code, framework_slug
    )
    if supplier_framework is not None:
        status_code = 200 if supplier_framework.declaration else 201
    else:
        supplier = Supplier.query.filter(
            Supplier.code == code
        ).first_or_404()

        supplier_framework = SupplierFramework(
            supplier_code=supplier.code,
            framework_id=framework.id,
            declaration={}
        )
        status_code = 201

    request_data = get_json_from_request()
    updater_json = validate_and_return_updater_request()
    json_has_required_keys(request_data, ['declaration'])

    supplier_framework.declaration = request_data['declaration'] or {}
    db.session.add(supplier_framework)
    db.session.add(
        AuditEvent(
            audit_type=AuditTypes.answer_selection_questions,
            db_object=supplier_framework,
            user=updater_json['updated_by'],
            data={'update': request_data['declaration']})
    )

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        abort(400, "Database Error: {}".format(e))

    return jsonify(declaration=supplier_framework.declaration), status_code


@main.route('/suppliers/<int:code>/frameworks/interest', methods=['GET'])
def get_registered_frameworks(code):
    supplier_frameworks = SupplierFramework.query.filter(
        SupplierFramework.supplier_code == code
    ).all()
    slugs = []
    for framework in supplier_frameworks:
        framework = Framework.query.filter(
            Framework.id == framework.framework_id
        ).first()
        slugs.append(framework.slug)

    return jsonify(frameworks=slugs)


@main.route('/suppliers/<int:code>/frameworks', methods=['GET'])
def get_supplier_frameworks_info(code):
    supplier = Supplier.query.filter(
        Supplier.code == code
    ).first_or_404()

    service_counts = SupplierFramework.get_service_counts(code)

    supplier_frameworks = SupplierFramework.query.filter(
        SupplierFramework.supplier == supplier
    ).all()

    return jsonify(frameworkInterest=[
        framework.serialize({
            'drafts_count': service_counts.get((framework.framework_id, 'not-submitted'), 0),
            'complete_drafts_count': service_counts.get((framework.framework_id, 'submitted'), 0),
            'services_count': service_counts.get((framework.framework_id, 'published'), 0)
        })
        for framework in supplier_frameworks]
    )


@main.route('/suppliers/<int:code>/frameworks/<framework_slug>', methods=['GET'])
def get_supplier_framework_info(code, framework_slug):
    supplier_framework = SupplierFramework.find_by_supplier_and_framework(
        code, framework_slug
    )
    if supplier_framework is None:
        abort(404)

    return jsonify(frameworkInterest=supplier_framework.serialize())


@main.route('/suppliers/<int:code>/frameworks/<framework_slug>', methods=['PUT'])
def register_framework_interest(code, framework_slug):

    framework = Framework.query.filter(
        Framework.slug == framework_slug
    ).first_or_404()

    supplier = Supplier.query.filter(
        Supplier.code == code
    ).first_or_404()

    json_payload = get_json_from_request()
    updater_json = validate_and_return_updater_request()
    json_payload.pop('updated_by')
    if json_payload:
        abort(400, "This PUT endpoint does not take a payload.")

    interest_record = SupplierFramework.query.filter(
        SupplierFramework.supplier_code == supplier.code,
        SupplierFramework.framework_id == framework.id
    ).first()
    if interest_record:
        return jsonify(frameworkInterest=interest_record.serialize()), 200

    if framework.status != 'open':
        abort(400, "'{}' framework is not open".format(framework_slug))

    interest_record = SupplierFramework(
        supplier_code=supplier.code,
        framework_id=framework.id,
        declaration={}
    )
    audit_event = AuditEvent(
        audit_type=AuditTypes.register_framework_interest,
        user=updater_json['updated_by'],
        data={'supplierId': supplier.code, 'frameworkSlug': framework_slug},
        db_object=supplier
    )

    try:
        db.session.add(interest_record)
        db.session.add(audit_event)
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        return jsonify(message="Database Error: {0}".format(e)), 400

    return jsonify(frameworkInterest=interest_record.serialize()), 201


@main.route('/suppliers/<int:code>/frameworks/<framework_slug>', methods=['POST'])
def update_supplier_framework_details(code, framework_slug):

    framework = Framework.query.filter(
        Framework.slug == framework_slug
    ).first_or_404()

    supplier = Supplier.query.filter(
        Supplier.code == code
    ).first_or_404()

    json_payload = get_json_from_request()
    updater_json = validate_and_return_updater_request()
    json_has_required_keys(json_payload, ["frameworkInterest"])
    update_json = json_payload["frameworkInterest"]

    interest_record = SupplierFramework.query.filter(
        SupplierFramework.supplier_code == supplier.code,
        SupplierFramework.framework_id == framework.id
    ).first()

    if not interest_record:
        abort(404, "code '{}' has not registered interest in {}".format(code, framework_slug))

    # `agreementDetails` shouldn't be passed in unless the framework has framework_agreement_details
    if 'agreementDetails' in update_json and framework.framework_agreement_details is None:
        abort(400, "Framework '{}' does not accept 'agreementDetails'".format(framework_slug))

    if (
            (framework.framework_agreement_details and framework.framework_agreement_details.get('frameworkAgreementVersion')) and  # noqa
            ('agreementDetails' in update_json or update_json.get('agreementReturned'))
    ):
        required_fields = ['signerName', 'signerRole']
        if update_json.get('agreementReturned'):
            required_fields.append('uploaderUserId')

        # Make a copy of the existing agreement_details with our new changes to be added and validate this
        # If invalid, 400
        agreement_details = interest_record.agreement_details.copy() if interest_record.agreement_details else {}

        if update_json.get('agreementDetails'):
            agreement_details.update(update_json['agreementDetails'])
        if update_json.get('agreementReturned'):
            agreement_details['frameworkAgreementVersion'] = framework.framework_agreement_details['frameworkAgreementVersion']  # noqa

        validate_agreement_details_data(
            agreement_details,
            enforce_required=False,
            required_fields=required_fields
        )

        if update_json.get('agreementDetails') and update_json['agreementDetails'].get('uploaderUserId'):
            user = User.query.filter(User.id == update_json['agreementDetails']['uploaderUserId']).first()
            if not user:
                abort(400, "No user found with id '{}'".format(update_json['agreementDetails']['uploaderUserId']))

        interest_record.agreement_details = agreement_details or None

    uniform_now = datetime.utcnow()

    if 'onFramework' in update_json:
        interest_record.on_framework = update_json['onFramework']
    if 'agreementReturned' in update_json:
        if update_json["agreementReturned"] is False:
            interest_record.agreement_returned_at = None
            interest_record.agreement_details = None
        else:
            interest_record.agreement_returned_at = uniform_now
    if update_json.get('countersigned'):
        interest_record.countersigned_at = uniform_now

    audit_event = AuditEvent(
        audit_type=AuditTypes.supplier_update,
        user=updater_json['updated_by'],
        data={'supplierId': supplier.code, 'frameworkSlug': framework_slug, 'update': update_json},
        db_object=supplier
    )

    try:
        db.session.add(interest_record)
        db.session.add(audit_event)
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        return jsonify(message="Database Error: {0}".format(e)), 400

    return jsonify(frameworkInterest=interest_record.serialize()), 200


@main.route('/domains', methods=['GET'])
def get_domains_list():
    result = [d.serializable for d in Domain.query.order_by('ordering').all()]
    return jsonify(domains=result)


@main.route('/suppliers/<int:code>/application', methods=['POST'])
def create_application_from_supplier(code):
    json_payload = get_json_from_request()
    json_has_required_keys(json_payload, ["current_user"])
    current_user = json_payload["current_user"]

    supplier = Supplier.query.filter(
        Supplier.code == code
    ).first_or_404()

    if 'application_id' in supplier.data:
        return abort(400, 'Supplier already has application')

    data = json.loads(supplier.json)

    data['status'] = 'saved'
    data = {key: data[key] for key in data if key not in ['id', 'contacts', 'domains', 'prices']}

    application = Application()
    application.update_from_json(data)

    db.session.add(application)
    db.session.add(AuditEvent(
        audit_type=AuditTypes.create_application,
        user='',
        data={},
        db_object=application
    ))

    db.session.flush()

    notification_message = '{}\nApplication Id:{}\nBy: {} ({})'.format(
        data['name'],
        application.id,
        current_user['name'],
        current_user['email_address']
    )
    notify_team('An existing seller has started a new application', notification_message)

    supplier.update_from_json({'application_id': application.id})
    users = User.query.filter(
        User.supplier_code == code and User.active == true()
    ).all()

    for user in users:
        user.application_id = application.id

    db.session.commit()

    return jsonify(application=application)


@main.route('/suppliers/<int:supplier_id>/domains/<int:domain_id>/<string:status>', methods=['POST'])
def assess_supplier_for_domain(supplier_id, domain_id, status):
    return update_domain_status(supplier_id, domain_id, status)


def update_domain_status(supplier_id, domain_id, status):
    supplier = Supplier.query.get(supplier_id)

    if supplier is None:
        abort(404, "Supplier '{}' does not exist".format(application_id))

    supplier.update_domain_assessment_status(domain_id, status)
    db.session.commit()
    db.session.refresh(supplier)
    return jsonify(supplier=supplier.serializable), 200

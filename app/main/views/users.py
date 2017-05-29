from datetime import datetime
from dmapiclient.audit import AuditTypes
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, DataError
from flask import jsonify, abort, request, current_app

from app import db, encryption
from app.main import main
from app.models import (
    AuditEvent, Contact, DraftService, Framework, Supplier, SupplierContact, SupplierFramework, SupplierUserInviteLog,
    User, Application, Brief
)
from app.utils import (
    get_json_from_request, get_positive_int_or_400, get_valid_page_or_1, json_has_required_keys, json_has_matching_id,
    pagination_links, validate_and_return_updater_request
)
from app.validation import validate_user_json_or_400, validate_user_auth_json_or_400

from collections import defaultdict
from app.emails import send_existing_seller_notification, send_existing_application_notification
from dmutils.logging import notify_team


@main.route('/users/auth', methods=['POST'])
def auth_user():
    json_payload = get_json_from_request()
    json_has_required_keys(json_payload, ["authUsers"])
    json_payload = json_payload["authUsers"]
    validate_user_auth_json_or_400(json_payload)

    user = User.get_by_email_address(json_payload['emailAddress'].lower())

    if user is None:
        return jsonify(authorization=False), 404
    elif encryption.authenticate_user(json_payload['password'], user) and user.active:
        user.logged_in_at = datetime.utcnow()
        user.failed_login_count = 0
        db.session.add(user)
        db.session.commit()

        return jsonify(users=user.serialize()), 200
    else:
        user.failed_login_count += 1
        db.session.add(user)
        db.session.commit()

        return jsonify(authorization=False), 403


@main.route('/users/<int:user_id>', methods=['GET'])
def get_user_by_id(user_id):
    user = User.query.filter(
        User.id == user_id
    ).first_or_404()
    return jsonify(users=user.serialize())


@main.route('/teammembers/<string:domain>', methods=['GET'])
def get_teammembers(domain):
    userlist = User.query.filter_by(role='buyer', active='true')
    teammembers = [{attr: getattr(_, attr) for attr in ["email_address", "id", "name"]}
                   for _ in userlist if all(
        [_.viewrow().email_domain == domain, _.active, '+' not in _.email_address]
    )]

    query = db.session.execute("""
        select name from govdomains
        where domain = :domain
    """, {'domain': domain})

    results = list(query)

    try:
        name = results[0].name
    except IndexError:
        name = 'Unknown'

    return jsonify(teammembers=teammembers, teamname=name)


@main.route('/users', methods=['GET'])
def list_users():
    user_query = User.query.order_by(User.id)
    page = get_valid_page_or_1()

    results_per_page = get_positive_int_or_400(
        request.args,
        'per_page',
        current_app.config['DM_API_USER_PAGE_SIZE']
    )

    # email_address is a primary key
    email_address = request.args.get('email_address')
    if email_address:
        user = user_query.filter(
            User.email_address == email_address.lower()
        ).first_or_404()

        return jsonify(
            users=[user.serialize()],
            links={}
        )

    supplier_code = request.args.get('supplier_code')
    if supplier_code is not None:
        try:
            supplier_code = int(supplier_code)
        except ValueError:
            abort(400, "Invalid supplier_code: {}".format(supplier_code))

        supplier = Supplier.query.filter(Supplier.code == supplier_code).all()
        if not supplier:
            abort(404, "supplier_code '{}' not found".format(supplier_code))

        user_query = user_query.filter(User.supplier_code == supplier_code)

    users = user_query.paginate(
        page=page,
        per_page=results_per_page,
    )

    return jsonify(
        users=[u.serialize() for u in users.items],
        links=pagination_links(
            users,
            '.list_users',
            request.args
        )
    )


@main.route('/users', methods=['POST'])
def create_user():

    json_payload = get_json_from_request()
    json_has_required_keys(json_payload, ["users"])
    json_payload = json_payload["users"]
    validate_user_json_or_400(json_payload)

    user = User.query.filter(
        User.email_address == json_payload['emailAddress'].lower()).first()

    if user:
        abort(409, "User already exists")

    if 'hashpw' in json_payload and not json_payload['hashpw']:
        password = json_payload['password']
    else:
        password = encryption.hashpw(json_payload['password'])

    now = datetime.utcnow()
    user = User(
        email_address=json_payload['emailAddress'].lower(),
        phone_number=json_payload.get('phoneNumber') or None,
        name=json_payload['name'],
        role=json_payload['role'],
        password=password,
        active=True,
        created_at=now,
        updated_at=now,
        password_changed_at=now
    )

    audit_data = {}

    if "supplierCode" in json_payload:
        user.supplier_code = json_payload['supplierCode']
        audit_data['supplier_code'] = user.supplier_code

    check_supplier_role(user.role, user.supplier_code)

    if "application_id" in json_payload:
        user.application_id = json_payload['application_id']
    elif user.supplier_code is not None:
        appl = Application.query.filter_by(supplier_code=user.supplier_code).first()
        user.application_id = appl and appl.id or None

    check_applicant_role(user.role, user.application_id)

    try:
        db.session.add(user)
        db.session.flush()

        audit = AuditEvent(
            audit_type=AuditTypes.create_user,
            user=json_payload['emailAddress'].lower(),
            data=audit_data,
            db_object=user
        )

        db.session.add(audit)
        db.session.commit()

        if user.role == 'buyer':
            notification_message = 'Domain: {}'.format(
                json_payload['emailAddress'].split('@')[-1]
            )

            notification_text = 'A new buyer has signed up'
            notify_team(notification_text, notification_message)

    except IntegrityError:
        db.session.rollback()
        abort(400, "Invalid supplier code or application id")
    except DataError:
        db.session.rollback()
        abort(400, "Invalid user role")

    return jsonify(users=user.serialize()), 201


@main.route('/users/<int:user_id>', methods=['POST'])
def update_user(user_id):
    """
        Update a user. Looks user up in DB, and updates where necessary.
    """
    update_details = validate_and_return_updater_request()

    user = User.query.filter(
        User.id == user_id
    ).first_or_404()

    json_payload = get_json_from_request()
    json_has_required_keys(json_payload, ["users"])
    user_update = json_payload["users"]

    json_has_matching_id(user_update, user_id)

    if 'password' in user_update:
        user.password = encryption.hashpw(user_update['password'])
        user.password_changed_at = datetime.utcnow()
        user_update['password'] = 'updated'
    if 'active' in user_update:
        user.active = user_update['active']
    if 'name' in user_update:
        user.name = user_update['name']
    if 'emailAddress' in user_update:
        user.email_address = user_update['emailAddress']
    if 'role' in user_update:
        if user.role == 'supplier' and user_update['role'] != user.role:
            user.supplier_code = None
            user_update.pop('supplierCode', None)
        user.role = user_update['role']
    if 'supplierCode' in user_update:
        user.supplier_code = user_update['supplierCode']
    if 'application_id' in user_update:
        user.application_id = user_update['application_id']
    if 'locked' in user_update and not user_update['locked']:
        user.failed_login_count = 0
    if 'termsAcceptedAt' in user_update:
        user.terms_accepted_at = user_update['termsAcceptedAt']

    check_supplier_role(user.role, user.supplier_code)
    check_applicant_role(user.role, user.application_id)

    audit = AuditEvent(
        audit_type=AuditTypes.update_user,
        user=update_details.get('updated_by', 'no user data'),
        data={
            'user': user.email_address,
            'update': user_update
        },
        db_object=user
    )

    db.session.add(user)
    db.session.add(audit)

    try:
        db.session.commit()
        return jsonify(users=user.serialize()), 200
    except (IntegrityError, DataError):
        db.session.rollback()
        abort(400, "Could not update user with: {0}".format(user_update))


@main.route('/users/export/<framework_slug>', methods=['GET'])
def export_users_for_framework(framework_slug):

    # 400 if framework slug is invalid
    framework = Framework.query.filter(Framework.slug == framework_slug).first()
    if not framework:
        abort(400, 'invalid framework')

    if framework.status == 'coming':
        abort(400, 'framework not yet open')

    supplier_frameworks_and_suppliers_and_users = db.session.query(
        SupplierFramework, Supplier, User
    ).join(
        Supplier, User, Framework
    ).filter(
        Framework.slug == framework_slug
    ).filter(
        User.active.is_(True)
    ).all()

    submitted_draft_counts_per_supplier = {}
    user_rows = []

    for sf, s, u in supplier_frameworks_and_suppliers_and_users:

        # always get the declaration status
        declaration_status = sf.declaration.get('status') if sf.declaration else 'unstarted'
        application_status = ''
        application_result = ''
        framework_agreement = ''

        # if framework is pending, live, or expired
        if framework.status != 'open':

            # get number of completed draft services per supplier
            # `application_status` is based on a complete declaration and at least one completed draft service
            if sf.supplier_code not in submitted_draft_counts_per_supplier.keys():
                submitted_draft_counts_per_supplier[sf.supplier_code] = db.session.query(
                    func.count()
                ).filter(
                    DraftService.supplier_code == sf.supplier_code,
                    DraftService.framework_id == sf.framework_id,
                    DraftService.status == 'submitted'
                ).scalar()

            submitted_draft_count = submitted_draft_counts_per_supplier[sf.supplier_code]
            application_status = \
                'application' if submitted_draft_count and declaration_status == 'complete' else 'no_application'
            if sf.on_framework is None:
                application_result = 'no result'
            else:
                application_result = 'pass' if sf.on_framework else 'fail'
            framework_agreement = bool(sf.agreement_returned_at)

        user_rows.append({
            'user_email': u.email_address,
            'user_name': u.name,
            'supplier_code': s.code,
            'declaration_status': declaration_status,
            'application_status': application_status,
            'framework_agreement': framework_agreement,
            'application_result': application_result
        })

    return jsonify(users=[user for user in user_rows])


def invite_response(db_results):
    def serialize_result(result):
        return {
            'contact': result[0].serialize(),
            'supplierCode': result[1],
            'supplierName': result[2],
        }

    return jsonify(results=[serialize_result(r) for r in db_results])


@main.route('/users/supplier-invite/list-candidates', methods=['GET'])
def list_supplier_account_invite_candidates():
    # NB: outer joins allow missing rows (nulls appear instead)
    joined_tables = db.session.query(Contact, Supplier.code, Supplier.name).select_from(SupplierContact) \
        .join(Supplier) \
        .join(Contact) \
        .outerjoin(SupplierUserInviteLog) \
        .outerjoin(User, Contact.email == User.email_address)
    # Current logic is that anyone who doesn't have an account and hasn't been invited yet gets an invite
    results = joined_tables.filter(User.id.is_(None)).filter(SupplierUserInviteLog.invite_sent.is_(None))

    return invite_response(results)


@main.route('/users/supplier-invite/list-unclaimed-invites', methods=['GET'])
def list_unclaimed_supplier_account_invites():
    # Supplier invites that still aren't matched with an existing account
    joined_tables = db.session.query(Contact, Supplier.code, Supplier.name) \
        .select_from(Supplier) \
        .join(SupplierUserInviteLog) \
        .join(Contact) \
        .outerjoin(User, Contact.email == User.email_address)
    results = joined_tables.filter(User.id.is_(None))

    return invite_response(results)


@main.route('/users/supplier-invite', methods=['POST'])
def record_supplier_invite():
    json_data = get_json_from_request()
    json_has_required_keys(json_data, ('supplierCode', 'email'))

    supplier_contact = SupplierContact.query.join(Supplier).join(Contact) \
        .filter(Supplier.code == json_data['supplierCode']) \
        .filter(Contact.email == json_data['email']) \
        .first()
    if supplier_contact is None:
        abort(400, 'No matching supplier and contact found')

    log_entry = SupplierUserInviteLog(supplier_id=supplier_contact.supplier_id, contact_id=supplier_contact.contact_id)
    db.session.merge(log_entry)
    db.session.commit()

    return jsonify(message='done')


@main.route('/users/count', methods=['GET'])
def get_buyers_stats():
    account_type = request.args.get('account_type', 'buyer')

    not_internal = \
        User.email_address.contains("+").is_(False) | \
        User.email_address.contains("digital.gov.au").is_(False)

    q_count = User.query.filter(
        User.active.is_(True),
        User.role == account_type,
        not_internal).count()

    buyers = {
        'total': q_count
    }

    return jsonify(buyers=buyers)


@main.route('/users/checkduplicates', methods=['POST'])
def get_duplicate_users():
    json_payload = get_json_from_request()
    json_has_required_keys(json_payload, ["email_address"])
    email_address = json_payload["email_address"]
    domain = email_address.split('@')[-1]

    if domain in current_app.config['GENERIC_EMAIL_DOMAINS']:
        return jsonify(duplicate=None)

    supplier_code = db.session.execute("""
        select distinct(supplier_code) from vuser
        where email_domain = :domain
    """, {'domain': domain}).fetchone()

    if (supplier_code and supplier_code[0]):
        send_existing_seller_notification(email_address, supplier_code[0])
        duplicate_audit_event(email_address, {'supplier_code': supplier_code[0]})
        return jsonify(duplicate={"supplier_code": supplier_code[0]})

    application_id = db.session.execute("""
        select distinct(application_id) from vuser
        where email_domain = :domain
    """, {'domain': domain}).fetchone()

    if (application_id and application_id[0]):
        send_existing_application_notification(email_address, application_id[0])
        duplicate_audit_event(email_address, {'application_id': application_id[0]})
        return jsonify(duplicate={"application_id": application_id[0]})

    return jsonify(duplicate=None)


def duplicate_audit_event(email_address, data):
    audit = AuditEvent(
        audit_type=AuditTypes.duplicate_supplier,
        user=email_address,
        data=data,
        db_object=None
    )

    db.session.add(audit)
    db.session.commit()


def check_supplier_role(role, supplier_code):
    if role == 'supplier' and supplier_code is None:
        abort(400, "'supplier_code' is required for users with 'supplier' role")
    elif role != 'supplier' and supplier_code is not None:
        abort(400, "'supplier_code' is only valid for users with 'supplier' role, not '{}'".format(role))


def check_applicant_role(role, application_id):
    if role == 'applicant' and application_id is None:
        abort(400, "'application id' is required for users with 'applicant' role")
    elif role != 'applicant' and role != 'supplier' and application_id is not None:
        abort(400, "'application_id' is only valid for users with 'applicant' or 'supplier' role, not '{}'"
              .format(role))

import os

from flask import jsonify, request, current_app, Response
from flask_login import current_user, login_required
from app.api import api
from app.api.services import briefs, brief_responses_service, brief_responses_contact_service, lots
from app.emails import send_brief_response_received_email
from dmapiclient.audit import AuditTypes
from ...models import (db, AuditEvent, Brief, BriefResponse, BriefResponseAnswer,
                       BriefResponseContact, Lot, Supplier, Framework, ValidationError)
from sqlalchemy.exc import DataError
from ...utils import (
    get_json_from_request
)
from dmutils.file import s3_upload_file_from_request, s3_download_file
import mimetypes
import rollbar
import json


def _can_do_brief_response(brief_id):
    try:
        brief = Brief.query.get(brief_id)
    except DataError:
        brief = None

    if brief is None:
        abort("Invalid brief ID '{}'".format(brief_id))

    if brief.status != 'live':
        abort("Brief must be live")

    if brief.framework.status != 'live':
        abort("Brief framework must be live")

    if not hasattr(current_user, 'role') or current_user.role != 'supplier':
        forbidden("Only supplier role users can respond to briefs")

    try:
        supplier = Supplier.query.filter(
            Supplier.code == current_user.supplier_code
        ).first()
    except DataError:
        supplier = None

    if not supplier:
        forbidden("Invalid supplier Code '{}'".format(current_user.supplier_code))

    def domain(email):
        return email.split('@')[-1]

    current_user_domain = domain(current_user.email_address) \
        if domain(current_user.email_address) not in current_app.config.get('GENERIC_EMAIL_DOMAINS') \
        else None

    if brief.data.get('sellerSelector', '') == 'someSellers':
        seller_domain_list = [domain(x).lower() for x in brief.data['sellerEmailList']]
        if current_user.email_address not in brief.data['sellerEmailList'] \
                and (not current_user_domain or current_user_domain.lower() not in seller_domain_list):
            forbidden("Supplier not selected for this brief")
    if brief.data.get('sellerSelector', '') == 'oneSeller':
        if current_user.email_address.lower() != brief.data['sellerEmail'].lower() \
                and (not current_user_domain or
                     current_user_domain.lower() != domain(brief.data['sellerEmail'].lower())):
            forbidden("Supplier not selected for this brief")

    if len(supplier.frameworks) == 0 \
            or 'digital-marketplace' != supplier.frameworks[0].framework.slug \
            or len(supplier.assessed_domains) == 0:
        abort(make_response(jsonify(
            errorMessage="Supplier does not have Digital Marketplace framework "
                         "or does not have at least one assessed domain"), 400))

    lot = lots.first(slug='digital-professionals')
    if brief.lot_id == lot.id:
        # Check if there are more than 3 brief response already from this supplier when professional aka specialists
        brief_response_count = brief_responses_service.find(supplier_code=supplier.code, brief_id=brief.id).count()
        if (brief_response_count > 2):  # TODO magic number
            abort(make_response(jsonify(
                errorMessage="There are already 3 brief response for supplier '{}'".format(supplier.code)), 400))
    else:
        # Check if brief response already exists from this supplier when outcome for all other types
        if brief_responses_service.find(supplier_code=supplier.code, brief_id=brief.id).one_or_none():
            abort(make_response(jsonify(
                errorMessage="Brief response already exists for supplier '{}'".format(supplier.code)), 400))

    return supplier, brief


@api.route('/brief/<int:brief_id>', methods=["GET"])
def get_brief(brief_id):
    brief = Brief.query.filter(
        Brief.id == brief_id
    ).first_or_404()
    if brief.status == 'draft':
        brief_user_ids = [user.id for user in brief.users]
        if hasattr(current_user, 'role') and current_user.role == 'buyer' and current_user.id in brief_user_ids:
            return jsonify(brief.serialize(with_users=True))
        else:
            return forbidden("Unauthorised to view brief or brief does not exist")
    else:
        return jsonify(brief.serialize(with_users=False))


@api.route('/brief/<int:brief_id>/responses', methods=['GET'])
@login_required
def get_brief_responses(brief_id):
    """All brief responses (role=supplier)
    ---
    tags:
      - "Brief Responses"
    security:
      - basicAuth: []
    parameters:
      - name: brief_id
        in: path
        type: number
        required: true
    definitions:
      BriefResponses:
        properties:
          briefResponses:
            type: array
            items:
              $ref: '#/definitions/BriefResponse'
      BriefResponse:
        type: object
        properties:
          id:
            type: number
          data:
            type: object
          brief_id:
            type: number
          supplier_code:
            type: number
    responses:
      200:
        description: A list of brief responses
        schema:
          $ref: '#/definitions/BriefResponse'
      404:
        description: brief_id not found
    """
    brief_responses = brief_responses_service.get_brief_responses(brief_id, current_user.supplier_code)

    if not brief_responses:
        abort(make_response(jsonify(errorMessage="Invalid brief id '{}'".format(brief_id)), 404))

    return jsonify(briefResponses=brief_responses)


@api.route('/brief/<int:brief_id>/respond/documents/<string:supplier_code>/<slug>', methods=['POST'])
@login_required
def upload_brief_response_file(brief_id, supplier_code, slug):
    supplier, brief = _can_do_brief_response(brief_id)
    return jsonify({"filename": s3_upload_file_from_request(request, slug,
                                                            os.path.join(brief.framework.slug, 'documents',
                                                                         'brief-' + str(brief_id),
                                                                         'supplier-' + str(supplier.code)))
                    })


@api.route('/brief/<int:brief_id>/respond/documents/<int:supplier_code>/<slug>', methods=['GET'])
@login_required
def download_brief_response_file(brief_id, supplier_code, slug):
    brief = Brief.query.filter(
        Brief.id == brief_id
    ).first_or_404()
    brief_user_ids = [user.id for user in brief.users]
    if hasattr(current_user, 'role') and (current_user.role == 'buyer' and current_user.id in brief_user_ids) \
            or (current_user.role == 'supplier' and current_user.supplier_code == supplier_code):
        file = s3_download_file(slug, os.path.join(brief.framework.slug, 'documents',
                                                   'brief-' + str(brief_id),
                                                   'supplier-' + str(supplier_code)))

        mimetype = mimetypes.guess_type(slug)[0] or 'binary/octet-stream'
        return Response(file, mimetype=mimetype)
    else:
        return forbidden("Unauthorised to view brief or brief does not exist")


@api.route('/brief/<int:brief_id>/respond', methods=["POST"])
@login_required
def post_brief_response(brief_id):
    brief_response_json = get_json_from_request()
    supplier, brief = _can_do_brief_response(brief_id)

    try:
        brief_response = BriefResponse(
            data=brief_response_json,
            supplier=supplier,
            brief=brief
        )

        if brief_responses_contact_service.find(supplier_code=supplier.code, brief_id=brief.id).one_or_none() is None:
            # create a brief response contact when it doesn't exist
            brief_response_contact = BriefResponseContact(
                supplier=supplier,
                brief=brief,
                email_address=current_user.email_address
            )
            db.session.add(brief_response_contact)

        def add_brief_response_answer_to_session(brief_response, attr, value):
            brief_response_answer = BriefResponseAnswer(
                brief_response=brief_response,
                question_enum=attr,
                answer=value
            )
            brief_response_answer.validate()
            db.session.add(brief_response_answer)
            return brief_response_answer

        for attr, value in brief_response_json.items():
            if (attr == 'essentialRequirements' or attr == 'niceToHaveRequirements' or attr == 'attachedDocumentURL'):
                # interestingly, these two variables can come in as an array or object
                if (type(value) is dict):
                    for k, v in sorted(value.items()):
                        brief_response.brief_response_answers.append(
                            add_brief_response_answer_to_session(brief_response, attr, v))
                else:
                    for v in value:
                        brief_response.brief_response_answers.append(
                            add_brief_response_answer_to_session(brief_response, attr, v))
            elif (attr == 'respondToEmailAddress'):
                pass  # new tables stores this somewhere else
            else:
                brief_response.brief_response_answers.append(
                    add_brief_response_answer_to_session(brief_response, attr, value))

        brief_response.validate()
        db.session.add(brief_response)
        db.session.flush()

    except ValidationError as e:
        print e
        brief_response_json['brief_id'] = brief_id
        rollbar.report_exc_info(extra_data=brief_response_json)
        message = ""
        if 'essentialRequirements' in e.message and e.message['essentialRequirements'] == 'answer_required':
            message = "Essential requirements must be completed"
            del e.message['essentialRequirements']
        if len(e.message) > 0:
            message += json.dumps(e.message)
        return jsonify(errorMessage=message), 400
    except Exception as e:
        print e
        brief_response_json['brief_id'] = brief_id
        rollbar.report_exc_info(extra_data=brief_response_json)
        return jsonify(errorMessage=e.message), 400

    try:
        send_brief_response_received_email(supplier, brief, brief_response)
    except Exception as e:
        brief_response_json['brief_id'] = brief_id
        rollbar.report_exc_info(extra_data=brief_response_json)

    audit = AuditEvent(
        audit_type=AuditTypes.create_brief_response,
        user=current_user.email_address,
        data={
            'briefResponseId': brief_response.id,
            'briefResponseJson': brief_response_json,
        },
        db_object=brief_response,
    )
    audit_service.log_audit_event(audit, {'audit_type': AuditTypes.create_brief_response,
                                          'briefResponseId': brief_response.id})

    return jsonify(briefResponses=brief_response.serialize()), 201


@api.route('/framework/<string:framework_slug>', methods=["GET"])
def get_framework(framework_slug):
    framework = Framework.query.filter(
        Framework.slug == framework_slug
    ).first_or_404()

    return jsonify(framework.serialize())

from flask import jsonify, abort, request, current_app
from sqlalchemy.exc import IntegrityError
from app.jiraapi import get_marketplace_jira
from app.main import main
from app.models import db, Application, Agreement, SignedAgreement, User, AuditEvent
from app.utils import (
    get_json_from_request, json_has_required_keys,
    pagination_links, get_valid_page_or_1,
    get_positive_int_or_400, validate_and_return_updater_request
)
import pendulum
from sqlalchemy.sql.expression import true
from dmapiclient.audit import AuditTypes
from app.emails import send_approval_notification, send_rejection_notification, \
    send_submitted_existing_seller_notification, send_submitted_new_seller_notification, \
    send_revert_notification


def get_application_json():
    json_payload = get_json_from_request()
    json_has_required_keys(json_payload, ['application'])
    return json_payload['application']


def save_application(application):
    db.session.add(application)

    try:
        db.session.flush()
    except IntegrityError as e:
        db.session.rollback()
        abort(400, e.orig)

    db.session.commit()


@main.route('/applications', methods=['POST'])
def create_application():
    application_json = get_application_json()

    application = Application()
    application.update_from_json(application_json)

    save_application(application)
    db.session.add(AuditEvent(
        audit_type=AuditTypes.create_application,
        user='',
        data={},
        db_object=application
    ))
    db.session.commit()

    return jsonify(application=application.serializable), 201


@main.route('/applications/<int:application_id>', methods=['PATCH'])
def update_application(application_id):
    application_json = get_application_json()

    application = Application.query.get(application_id)
    if application is None:
        abort(404, "Application '{}' does not exist".format(application_id))

    if application.status == 'submitted' and application_json.get('status') == 'saved':
        db.session.add(AuditEvent(
            audit_type=AuditTypes.revert_application,
            user='',
            data={},
            db_object=application
        ))

    application.update_from_json(application_json)
    save_application(application)

    return jsonify(application=application.serializable), 200


@main.route('/applications/<int:application_id>/approve', methods=['POST'])
def approve_application(application_id):
    application_response = application_approval(application_id, True)
    send_approval_notification(application_id)
    return application_response


@main.route('/applications/<int:application_id>/reject', methods=['POST'])
def reject_application(application_id):
    application_response = application_approval(application_id, False)
    # rejection email is disabled for now
    # send_rejection_notification(application_id)
    return application_response


@main.route('/applications/<int:application_id>/revert', methods=['POST'])
def revert_application(application_id):
    updater_json = validate_and_return_updater_request()
    json_payload = request.get_json(force=True)
    message = json_payload.get('message', None)

    if not message:
        message = None
    if message is not None and message.isspace():
        message = None

    application = Application.query.get(application_id)

    if application is None:
        abort(404, "Application '{}' does not exist".format(application_id))
    if application.status != 'submitted':
        abort(400, "Application '{}' is not in submitted state for reverting ".format(application_id))

    db.session.add(AuditEvent(
        audit_type=AuditTypes.revert_application,
        user=updater_json['updated_by'],
        data={},
        db_object=application
    ))

    application.status = 'saved'
    db.session.commit()
    # post request from react RevertNotification form sends message as empty string
    # to indicate we should run the revert logic but not send an email
    if message is not None:
        send_revert_notification(application_id, message)
    return jsonify(application=application.serializable), 200


def application_approval(application_id, result):
    updater_json = validate_and_return_updater_request()

    application = Application.query.get(application_id)

    if application is None:
        abort(404, "Application '{}' does not exist".format(application_id))

    db.session.add(AuditEvent(
        audit_type=(AuditTypes.approve_application if result else AuditTypes.reject_application),
        user=updater_json['updated_by'],
        data={},
        db_object=application
    ))
    application.set_approval(approved=result)
    db.session.commit()
    return jsonify(application=application.serializable), 200


@main.route('/applications/<int:application_id>', methods=['GET'])
def get_application_by_id(application_id):
    application = Application.query.filter(
        Application.id == application_id
    ).first_or_404()
    if application.status == 'deleted':
        abort(404)
    return jsonify(application=application.serializable)


@main.route('/prioritise/<int:application_id>', methods=['POST'])
def prioritise_application(application_id):
    brief_closing_date = request.get_json()
    if type(brief_closing_date) is not unicode:
        abort(400, 'Invalid  %s type argument received. Expected unicode') % (type(brief_closing_date))

    application = Application.query.filter(
        Application.id == application_id
    ).first_or_404()
    application.create_approval_task(closing_date=brief_closing_date)
    return jsonify(application=application.serializable)


@main.route('/applications/<int:application_id>', methods=['DELETE'])
def delete_application(application_id):
    """
    Delete a Application
    :param application_id:
    :return:
    """
    updater_json = validate_and_return_updater_request()
    application = Application.query.filter(
        Application.id == application_id
    ).first_or_404()

    db.session.add(AuditEvent(
        audit_type=AuditTypes.delete_application,
        user=updater_json['updated_by'],
        data={},
        db_object=application
    ))
    application.status = 'deleted'

    users = User.query.filter(
        User.application_id == application_id
    ).all()

    for user in users:
        user.application = None

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        abort(400, "Database Error: {0}".format(e))

    return jsonify(message="done"), 200


def applications_list_response(with_task_status=False, status=None):
    if status:
        applications = Application.query.filter(Application.status == status)
    else:
        applications = Application.query.filter(Application.status != 'deleted')

    return format_applications(applications, with_task_status)


def format_applications(applications, with_task_status):
    ordering = request.args.get('order_by', 'application.created_at desc')
    order_by = ordering.split(',')

    applications = applications.order_by(*order_by)

    page = get_valid_page_or_1()
    results_per_page = get_positive_int_or_400(
        request.args,
        'per_page',
        current_app.config['DM_API_PAGE_SIZE']
    )

    applications = applications.paginate(
        page=page,
        per_page=1000
    )

    apps_results = [_.serializable for _ in applications.items]

    if with_task_status and current_app.config['JIRA_FEATURES']:
        jira = get_marketplace_jira()
        tasks_by_id = jira.assessment_tasks_by_application_id()

        def annotate_app(app):
            try:
                app['tasks'] = tasks_by_id.get(str(app['id']), None)
            except KeyError:
                pass
            return app

        apps_results = [annotate_app(_) for _ in apps_results]

    return jsonify(
        applications=apps_results,
        links=pagination_links(
            applications,
            '.list_applications',
            request.args
        )
    )


@main.route('/applications/<int:application_id>/submit', methods=['POST'])
def submit_application(application_id):
    current_time = pendulum.now('UTC').to_iso8601_string(extended=True)

    application = Application.query.get(application_id)
    if application is None:
        abort(404, "Application '{}' does not exist".format(application_id))

    if application.status == 'submitted':
        abort(400, 'Application is already submitted')

    json_payload = get_json_from_request()
    json_has_required_keys(json_payload, ['user_id'])
    user_id = json_payload['user_id']

    user = User.query.get(user_id)

    if user.application_id != application.id:
        abort(400, 'User is not authorized to submit application')

    current_agreement = Agreement.query.filter(
        Agreement.is_current == true()
    ).first_or_404()

    db.session.add(AuditEvent(
        audit_type=AuditTypes.submit_application,
        user=user_id,
        data={},
        db_object=application
    ))

    application.submit_for_approval()

    application.update_from_json({'submitted_at': current_time})

    if application.type != 'edit':
        # only create signed agreements on initial applications
        signed_agreement = SignedAgreement()
        signed_agreement.user_id = user_id
        signed_agreement.agreement_id = current_agreement.id
        signed_agreement.signed_at = current_time
        signed_agreement.application_id = application_id

        db.session.add(signed_agreement)

        if application.supplier_code:
            send_submitted_existing_seller_notification(application.id)
        else:
            send_submitted_new_seller_notification(application.id)

    db.session.commit()
    return jsonify(application=application.serializable,
                   signed_agreement=signed_agreement)


@main.route('/applications', methods=['GET'])
def list_applications():
    return applications_list_response(with_task_status=False)


@main.route('/applications/status/<string:status>', methods=['GET'])
def list_applications_by_status(status):
    return applications_list_response(with_task_status=False, status=status)


@main.route('/applications/tasks', methods=['GET'])
def list_applications_taskstatus():
    return applications_list_response(with_task_status=True)


@main.route('/applications/search/<string:keyword>', methods=['GET'])
def search_applications(keyword):
    if not keyword:
        return applications_list_response(with_task_status=False)

    if keyword.isdigit():
        applications = Application.query.filter(Application.id == keyword)
    else:
        applications = Application.query.filter(
            Application.data["name"].astext.ilike('%{}%'.format(keyword)))

    return format_applications(applications, False)


@main.route('/tasks', methods=['GET'])
def list_task_status():
    jira = get_marketplace_jira()
    tasks_by_id = jira.assessment_tasks_by_application_id()
    return jsonify(tasks=tasks_by_id)

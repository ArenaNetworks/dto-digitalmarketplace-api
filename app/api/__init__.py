import rollbar
from dmutils.csrf import check_valid_csrf
from dmutils.user import User as LoginUser
from sqlalchemy.orm import noload
from flask import Blueprint, request, current_app
from flask_login import LoginManager
from app.models import User
from app.api.business import supplier_business
from base64 import b64decode
from app import encryption
from app.api.helpers import abort

api = Blueprint('api', __name__)
login_manager = LoginManager()


@api.record_once
def on_load(state):
    login_manager.init_app(state.app)


@login_manager.user_loader
def load_user(userid):
    user = User.query.options(
        noload('*')
    ).get(int(userid))

    if user is not None:
        notification_count = get_notification_count(user)
        user = LoginUser(user.id, user.email_address, user.supplier_code, None, user.locked,
                         user.active, user.name, user.role, user.terms_accepted_at, user.application_id,
                         user.frameworks, notification_count)

    return user


def get_notification_count(user):
    notification_count = None
    if user.role == 'supplier':
        errors_warnings = supplier_business.get_supplier_messages(user.supplier_code, False)
        notification_count = len(errors_warnings.errors + errors_warnings.warnings)

    return notification_count


@api.before_request
def check_csrf_token():
    if request.method in ('POST', 'PATCH', 'PUT', 'DELETE'):
        new_csrf_valid = check_valid_csrf()

        if not (new_csrf_valid):
            rollbar.report_message('csrf.invalid_token: Aborting request check_csrf_token()', 'error', request)
            abort('Invalid CSRF token. Please try again.')


@api.after_request
def add_cache_control(response):
    response.headers['Cache-control'] = 'no-cache, no-store'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = 0
    return response


@login_manager.request_loader
def load_user_from_request(request):
    if not current_app.config.get('BASIC_AUTH'):
        return None

    payload = get_token_from_headers(request.headers)

    if payload is None:
        return None

    email_address, password = b64decode(payload).split(':', 1)
    user = User.get_by_email_address(email_address.lower())

    if user is not None:
        if encryption.authenticate_user(password, user):
            notification_count = get_notification_count(user)
            user = LoginUser(user.id, user.email_address, user.supplier_code, None, user.locked,
                             user.active, user.name, user.role, user.terms_accepted_at, user.application_id,
                             user.frameworks, notification_count)
            return user


from app.api.views import (briefs,  # noqa
                           brief_responses,
                           users,
                           feedback,
                           messages,
                           suppliers,
                           seller_dashboard,
                           seller_edit,
                           tasks,
                           dashboards,
                           opportunities,
                           key_values)

from app.api.views.reports import (  # noqa
    brief,
    brief_response,
    suppliers
)

from app.api.business.validators import (  # noqa
    application_validator,
    supplier_validator
)


def get_token_from_headers(headers):
    print headers
    auth_header = headers.get('Authorization', '')
    if auth_header[:6] != 'Basic ':
        return None
    return auth_header[6:]

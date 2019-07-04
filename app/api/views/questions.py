from flask import request, jsonify
from flask_login import login_required, current_user
from app.api import api
from app.api.business import questions_business
from app.api.helpers import get_email_domain, role_required
from app.api.business.errors import (
    NotFoundError,
    ValidationError
)
from ...utils import get_json_from_request


@api.route('/brief/<int:brief_id>/questions', methods=['GET'])
@login_required
@role_required('buyer')
def get_questions(brief_id):
    result = questions_business.get_questions(brief_id)

    return jsonify(result), 200


@api.route('/brief/<int:brief_id>/answers', methods=['GET'])
@login_required
@role_required('buyer')
def get_answers(brief_id):
    result = questions_business.get_answers(brief_id)

    return jsonify(result), 200


@api.route('/brief/<int:brief_id>/publish-answer', methods=['POST'])
@login_required
@role_required('buyer')
def publish_answer(brief_id):

    data = get_json_from_request()
    try:
        questions_business.publish_answer({
            'email_address': current_user.email_address,
            'user_id': current_user.id
        }, brief_id, data)

    except NotFoundError as nfe:
        not_found(nfe.message)
    except ValidationError as ve:
        abort(ve.message)

    return jsonify(success=True), 200

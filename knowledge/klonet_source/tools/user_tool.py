from flask_login import current_user
from flask import current_app


def get_user_name(data, method):
    if not current_app.config.get('LOGIN_DISABLED'):
        return current_user.name
    try:
        if method == "POST":
            return data["user"]
        if method == "GET":
            return data
    except:
        pass
    try:
        if method == "POST":
            return data["user_name"]
        if method == "GET":
            return data
    except:
        pass
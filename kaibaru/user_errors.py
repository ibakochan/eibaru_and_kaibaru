from functools import wraps

from django.core.exceptions import ValidationError
from django.http import JsonResponse


def format_validation_error(exc):
    """
    Flatten Django / DRF ValidationError objects into a single
    user-facing string. Avoids the ugly \"['message']\" form of str(e).

    Use error_dict rather than message_dict: message_dict is a property
    that raises AttributeError for non-field ValidationErrors.
    """
    if exc is None:
        return ""

    error_dict = getattr(exc, "error_dict", None)
    if error_dict:
        parts = []
        for messages in error_dict.values():
            if isinstance(messages, (list, tuple)):
                parts.extend(
                    format_validation_error(message)
                    if isinstance(message, Exception)
                    else str(message)
                    for message in messages
                    if message
                )
            elif messages:
                parts.append(str(messages))
        if parts:
            return " ".join(parts)

    messages = getattr(exc, "messages", None)
    if messages:
        return " ".join(str(message) for message in messages if message)

    message = getattr(exc, "message", None)
    if message:
        return str(message)

    detail = getattr(exc, "detail", None)
    if detail is not None:
        return format_validation_error_value(detail)

    return str(exc)


def format_validation_error_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = [
            format_validation_error_value(item)
            for item in value.values()
        ]
        return " ".join(part for part in parts if part)
    if isinstance(value, (list, tuple)):
        parts = [format_validation_error_value(item) for item in value]
        return " ".join(part for part in parts if part)
    return str(value)


def json_validation_error_response(exc, status=400):
    return JsonResponse(
        {"error": format_validation_error(exc)},
        status=status,
    )


def json_validation_errors(view_func):
    """
    Convert uncaught Django ValidationError into a JSON 400 so the
    frontend can show the restriction message instead of a generic
    catch-all.
    """

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        try:
            return view_func(*args, **kwargs)
        except ValidationError as exc:
            return json_validation_error_response(exc)

    return wrapper

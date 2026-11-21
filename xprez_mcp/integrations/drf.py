"""Django REST framework errors as tool results. DRF stays optional."""

import sys


def error_value(error):
    """A readable result for a DRF exception, or None for anything else."""
    if sys.modules.get("rest_framework") is None:
        return None
    from rest_framework.exceptions import APIException, ValidationError

    if isinstance(error, ValidationError):
        detail = error.detail
        if isinstance(detail, dict):
            summary = "; ".join(
                f"{field}: {' '.join(str(message) for message in _messages(errors))}"
                for field, errors in detail.items()
            )
            return {"error": f"Invalid data. {summary}", "fields": detail}
        return "Invalid data. " + " ".join(str(item) for item in _messages(detail))
    if isinstance(error, APIException):
        return str(error.detail)
    return None


def _messages(errors):
    if isinstance(errors, dict):
        return [message for value in errors.values() for message in _messages(value)]
    if isinstance(errors, (list, tuple)):
        return [message for value in errors for message in _messages(value)]
    return [errors]

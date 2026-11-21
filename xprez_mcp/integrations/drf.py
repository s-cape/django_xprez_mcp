"""Django REST framework serializers for writes, and DRF errors as tool results.

The only module that knows DRF's API; DRF stays optional. Reading needs no
adapter: any ``serializer(instance, many=..., context=...).data`` works.
"""

import sys
from typing import NamedTuple


class SerializerField(NamedTuple):
    read_only: bool
    required: bool
    nullable: bool
    is_file: bool


def serializer_fields(serializer_class):
    """``{name: SerializerField}`` for every field of the serializer."""
    return {
        name: SerializerField(
            read_only=field.read_only,
            required=field.required,
            nullable=getattr(field, "allow_null", False),
            is_file=is_file_field(field),
        )
        for name, field in serializer_class().fields.items()
    }


def validate(serializer_class, data, instance, context):
    """``validated_data`` for ``data``; partial when updating ``instance``.
    Raises DRF's ``ValidationError``, which ``error_value`` turns readable."""
    serializer = serializer_class(
        instance=instance,
        data=data,
        partial=instance is not None,
        context=context,
    )
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


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


def is_file_field(field):
    """A DRF ``FileField`` or ``ImageField`` that takes uploads. Other fields
    on a model file field keep their own values: strings, URLs, and subclasses
    that read something else (``Base64ImageField`` and the like override
    ``to_internal_value`` to decode a string)."""
    if sys.modules.get("rest_framework") is None:
        return False
    from rest_framework.fields import FileField, ImageField

    if not isinstance(field, FileField):
        return False
    reads = type(field).to_internal_value
    return reads in (FileField.to_internal_value, ImageField.to_internal_value)


def _messages(errors):
    if isinstance(errors, dict):
        return [message for value in errors.values() for message in _messages(value)]
    if isinstance(errors, (list, tuple)):
        return [message for value in errors for message in _messages(value)]
    return [errors]

"""Model fields as tool arguments: their types and which ones can be written."""

import datetime
import decimal
import inspect
import uuid
from typing import Any, Literal, Optional

from django.core.exceptions import FieldDoesNotExist
from django.db import models

from ..core.files import DEFAULT_SOURCES, file_argument


def field_annotation(field):
    """The Python type of a model field's value, as used for schemas."""
    if field.many_to_many or field.one_to_many:
        return list[field_annotation(field.related_model._meta.pk)]
    if field.is_relation:
        annotation = field_annotation(field.target_field)
    elif field.choices:
        values = []
        for value, label in field.flatchoices:
            if value not in ("", None):
                values.append(value)
        annotation = Literal[tuple(values)] if values else base_annotation(field)
    else:
        annotation = base_annotation(field)
    if field.null and annotation is not Any:
        annotation = Optional[annotation]
    return annotation


def base_annotation(field):
    """The type of a field's own value: no relations, choices or null."""
    if isinstance(field, models.BooleanField):
        return bool
    if isinstance(field, (models.IntegerField, models.AutoField)):
        return int
    if isinstance(field, models.FloatField):
        return float
    if isinstance(field, models.DecimalField):
        return decimal.Decimal
    if isinstance(field, models.DateTimeField):
        return datetime.datetime
    if isinstance(field, models.DateField):
        return datetime.date
    if isinstance(field, models.UUIDField):
        return uuid.UUID
    if isinstance(field, models.JSONField):
        return Any
    if isinstance(field, (models.CharField, models.TextField, models.FileField)):
        return str
    return Any


def is_writable(opts, name):
    """An editable model field that a ``ModelForm`` can write."""
    try:
        field = opts.pk if name == "pk" else opts.get_field(name)
    except FieldDoesNotExist:
        return False
    if field.auto_created or not field.editable or field.primary_key:
        return False
    if field.one_to_many:
        return False
    return field.concrete or field.many_to_many


def upload_prefix(field):
    """The prefix of every upload's name for ``field``: the static part of a
    string ``upload_to`` (before any ``strftime`` code), or None when there
    is none (a callable or empty ``upload_to``)."""
    upload_to = field.upload_to
    if callable(upload_to) or not upload_to:
        return None
    upload_to = str(upload_to)
    if "%" in upload_to:  # "images/%Y/" -> "images/", "img_%Y" -> "img_"
        prefix = upload_to.split("%", 1)[0]
    else:  # a folder, with or without its slash
        prefix = upload_to.rstrip("/") + "/"
    return prefix.lstrip("/") or None


def file_field(opts, name):
    """The ``FileField`` called ``name``, or None."""
    try:
        field = opts.get_field(name)
    except FieldDoesNotExist:
        return None
    return field if isinstance(field, models.FileField) else None


def write_parameter(
    opts,
    name,
    partial,
    required=None,
    file_sources=DEFAULT_SOURCES,
    nullable=None,
    as_file=True,
):
    """``(name, annotation, default)`` of a writable field.

    A file field takes a file argument unless ``as_file`` is false (a
    serializer field that is not a file field), and ``null`` (clear) when
    ``nullable``, by default when it has ``blank=True``: a form keeps the file
    of a field that is not blank.
    """
    try:
        field = opts.get_field(name)
    except FieldDoesNotExist:
        field = None
    if field is None:
        annotation = Any
    elif isinstance(field, models.FileField) and as_file:
        if nullable is None:
            nullable = field.blank
        annotation = file_argument(file_sources, nullable)
    else:
        annotation = field_annotation(field)
    if required is None:
        if field is None or field.many_to_many:
            required = False
        else:
            required = not (field.blank or field.null or field.has_default())
    required = required and not partial
    return (name, annotation, inspect.Parameter.empty if required else None)

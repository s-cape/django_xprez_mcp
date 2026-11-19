"""``ModelProvider``: tools and resources generated from a Django model."""

import base64
import datetime
import decimal
import inspect
import uuid
from functools import cached_property
from typing import Annotated, Any, Literal

from django.core.exceptions import (
    FieldDoesNotExist,
    ImproperlyConfigured,
    PermissionDenied,
)
from django.db import models, transaction
from django.db.models import prefetch_related_objects
from django.db.models import Q
from django.forms import ModelForm, modelform_factory
from django.http import Http404
from django.utils.text import slugify

from . import schema
from .errors import InvalidArguments, ToolError
from .items import Item, ResourceSpec, ToolSpec
from .permissions import check_permission
from .providers import Provider

ACTIONS = ("list", "get", "create", "update", "delete")
WRITE_ACTIONS = ("create", "update", "delete")
EXPOSURES = ("tools", "generic")
RESERVED_ARGUMENTS = {"query", "limit", "cursor", "ordering"}

_HINTS = {
    "list": {"read_only": True},
    "get": {"read_only": True},
    "create": {},
    "update": {"idempotent": True},
    "delete": {"destructive": True},
}

# Lookups whose value is not of the field's own type.
_STRING_LOOKUPS = {
    "contains",
    "icontains",
    "startswith",
    "istartswith",
    "endswith",
    "iendswith",
    "regex",
    "iregex",
    "search",
}
_INT_LOOKUPS = {"year", "month", "day", "week", "week_day", "iso_week_day", "quarter"}
_INT_LOOKUPS |= {"hour", "minute", "second", "iso_year"}


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
        annotation = Literal[tuple(values)] if values else _base_annotation(field)
    else:
        annotation = _base_annotation(field)
    if field.null and annotation is not Any:
        annotation = annotation | None
    return annotation


def _base_annotation(field):
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
    if field.one_to_many or isinstance(field, models.FileField):
        return False
    return field.concrete or field.many_to_many


def write_parameter(opts, name, partial, required=None):
    """``(name, annotation, default)`` of a writable field."""
    try:
        field = opts.get_field(name)
    except FieldDoesNotExist:
        field = None
    annotation = Any if field is None else field_annotation(field)
    if required is None:
        if field is None or field.many_to_many:
            required = False
        else:
            required = not (field.blank or field.null or field.has_default())
    required = required and not partial
    return (name, annotation, inspect.Parameter.empty if required else None)


def form_error(form):
    """A readable ``ToolError`` from an invalid form or formset."""
    errors = {
        field: [error["message"] for error in messages]
        for field, messages in form.errors.get_json_data().items()
    }
    summary = "; ".join(
        f"{field}: {' '.join(messages)}" for field, messages in errors.items()
    )
    return ToolError(f"Invalid data. {summary}", data={"fields": errors})


def make_function(qualname, module, parameters, impl):
    """A function with a computed signature, so generated tools get schemas and
    argument conversion from the same machinery as hand-written ones."""

    def func(self, ctx, **kwargs):
        return impl(self, ctx, **kwargs)

    positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
    keyword = inspect.Parameter.KEYWORD_ONLY
    params = [
        inspect.Parameter("self", positional),
        inspect.Parameter("ctx", positional),
    ]
    annotations = {}
    for name, annotation, default in parameters:
        params.append(
            inspect.Parameter(name, keyword, default=default, annotation=annotation)
        )
        annotations[name] = annotation
    func.__signature__ = inspect.Signature(params)
    func.__annotations__ = annotations
    func.__name__ = qualname.rsplit(".", 1)[-1]
    func.__qualname__ = qualname
    func.__module__ = module
    return func


def _encode_cursor(offset):
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(cursor):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        offset = int(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        raise InvalidArguments("cursor is invalid.") from None
    if offset < 0:
        raise InvalidArguments("cursor is invalid.")
    return offset


class ModelProvider(Provider):
    """A provider generated from a model: list, get, create, update, delete.

    Register it with ``@mcp.register(Model)`` or set ``model`` on the class.
    """

    model = None
    name = None  # tool prefix, generic model name, resource path
    fields = None
    detail_fields = None
    write_fields = None
    serializer = None
    detail_serializer = None
    form = ModelForm
    lookup_field = "pk"
    search_fields = ()
    filters = {}
    ordering = None
    ordering_fields = ()
    actions = ("list", "get")
    permissions = {}
    tool_names = {}
    descriptions = {}
    resources = False
    resource_scheme = None
    exposure = None  # "tools" | "generic"; None follows MCPServer(models=...)
    related_labels = None  # None follows MCPServer(related_labels=...)
    page_size = 20
    max_page_size = 100

    # Admin-style preset: permissions = ModelProvider.django_model_permissions
    django_model_permissions = {
        "list": "{app_label}.view_{model_name}",
        "get": "{app_label}.view_{model_name}",
        "create": "{app_label}.add_{model_name}",
        "update": "{app_label}.change_{model_name}",
        "delete": "{app_label}.delete_{model_name}",
    }

    def __init__(self, server, model=None):
        super().__init__(server, model=model or type(self).model)
        if self.model is None:
            raise ImproperlyConfigured(
                f"{type(self).__qualname__} needs a model: register it with "
                "mcp.register(Model, Provider) or set Provider.model."
            )
        self.check()

    # --- configuration ------------------------------------------------------

    @property
    def opts(self):
        return self.model._meta

    def check(self):
        label = type(self).__qualname__
        unknown = set(self.actions) - set(ACTIONS)
        if unknown:
            raise ImproperlyConfigured(f"{label}: unknown actions {sorted(unknown)}.")
        if self.exposure is not None and self.exposure not in EXPOSURES:
            raise ImproperlyConfigured(
                f"{label}: exposure must be one of {EXPOSURES} or None."
            )
        if (self.fields is None) == (self.serializer is None):
            raise ImproperlyConfigured(
                f"{label} needs fields or a serializer (not both); no field is "
                "exposed by default."
            )
        if self.detail_fields is not None and self.detail_serializer is not None:
            raise ImproperlyConfigured(
                f"{label}: set detail_fields or detail_serializer, not both."
            )
        if self.lookup_field != "pk":
            self.get_model_field(self.lookup_field)
        clash = RESERVED_ARGUMENTS & set(self.get_filters())
        if clash:
            raise ImproperlyConfigured(f"{label}: reserved filter names {clash}.")
        if self.serializer is None and self.write_fields is not None:
            invalid = [
                name for name in self.write_fields if not self._is_writable(name)
            ]
            if invalid:
                raise ImproperlyConfigured(
                    f"{label}: {invalid} are not editable model fields; writes "
                    "without a serializer go through a ModelForm."
                )

    def get_model_field(self, name):
        try:
            return self.opts.pk if name == "pk" else self.opts.get_field(name)
        except FieldDoesNotExist:
            raise ImproperlyConfigured(
                f"{type(self).__qualname__}: {self.opts.label} has no field {name!r}."
            ) from None

    def get_related_labels(self):
        if self.related_labels is not None:
            return self.related_labels
        return self.server.related_labels

    def get_exposure(self):
        return self.exposure or self.server.models

    def get_name(self):
        if self.name:
            return self.name
        return slugify(str(self.opts.verbose_name_plural)).replace("-", "_")

    @property
    def lookup_argument(self):
        return "id" if self.lookup_field == "pk" else self.lookup_field

    @cached_property
    def lookup_annotation(self):
        annotation = field_annotation(self.get_model_field(self.lookup_field))
        if annotation is Any:
            return str
        return annotation

    def get_tool_name(self, action):
        return self.tool_names.get(action) or f"{self.get_name()}_{action}"

    def get_description(self, action):
        if action in self.descriptions:
            return self.descriptions[action]
        singular = str(self.opts.verbose_name)
        plural = str(self.opts.verbose_name_plural)
        by = self.lookup_argument
        if action == "list":
            parts = [f"List {plural}."]
            if self.has_search():
                fields = ", ".join(f.lstrip("^=@") for f in self.search_fields)
                parts.append(f"query searches {fields}." if fields else "")
            parts.append("Pass next_cursor as cursor for the next page.")
            return " ".join(part for part in parts if part)
        if action == "get":
            return f"Get one {singular} by {by}."
        if action == "create":
            return f"Create a {singular}."
        if action == "update":
            return f"Update a {singular} by {by}. Only the fields given change."
        return f"Delete a {singular} by {by}."

    # --- permissions --------------------------------------------------------

    def get_permissions(self):
        """``permissions`` with ``{app_label}`` and ``{model_name}`` filled in."""
        names = {"app_label": self.opts.app_label, "model_name": self.opts.model_name}

        def fill(permission):
            if isinstance(permission, str):
                return permission.format(**names)
            if isinstance(permission, (list, tuple)):
                return [fill(item) for item in permission]
            return permission

        return {action: fill(value) for action, value in self.permissions.items()}

    def writes_allowed(self):
        """Writes of third-party providers need ``ALLOW_WRITES``."""
        return self.server.writes_allowed(None, self)

    def has_action_permission(self, ctx, action):
        if action not in self.actions:
            return False
        if action in WRITE_ACTIONS and not self.writes_allowed():
            return False
        return check_permission(self.get_permissions().get(action), ctx)

    def has_object_permission(self, ctx, action, instance):
        return True

    def generic_actions(self):
        """The generic tools this provider takes part in."""
        return set(self.actions)

    def allows(self, ctx, action):
        """The provider and the action are allowed (not per object)."""
        return self.has_permission(ctx) and self.has_action_permission(ctx, action)

    # --- querysets ----------------------------------------------------------

    def get_queryset(self, ctx):
        return self.model._default_manager.all()

    def has_search(self):
        return bool(self.search_fields) or (
            type(self).search is not ModelProvider.search
        )

    def search(self, ctx, queryset, query):
        """Admin-style: every word must match one of ``search_fields``
        (``^`` starts with, ``=`` exact, ``@`` full text, default contains)."""
        lookups = [self._search_lookup(field) for field in self.search_fields]
        if not lookups:
            return queryset
        for word in query.split():
            condition = Q()
            for lookup in lookups:
                condition |= Q(**{lookup: word})
            queryset = queryset.filter(condition)
        if any(self._spans_many(lookup) for lookup in lookups):
            queryset = queryset.distinct()
        return queryset

    @staticmethod
    def _search_lookup(field):
        if field.startswith("^"):
            return f"{field[1:]}__istartswith"
        if field.startswith("="):
            return f"{field[1:]}__iexact"
        if field.startswith("@"):
            return f"{field[1:]}__search"
        return f"{field}__icontains"

    def _resolve_path(self, path):
        """(last field, remaining lookup, crosses a to-many relation)."""
        model, field, many = self.model, None, False
        parts = path.split("__")
        for index, part in enumerate(parts):
            if model is None:
                return field, "__".join(parts[index:]), many
            try:
                field = model._meta.pk if part == "pk" else model._meta.get_field(part)
            except FieldDoesNotExist:
                if field is None:
                    raise ImproperlyConfigured(
                        f"{type(self).__qualname__}: {self.opts.label} has no "
                        f"field {part!r}."
                    ) from None
                return field, "__".join(parts[index:]), many
            if field.many_to_many or field.one_to_many:
                many = True
            model = field.related_model if field.is_relation else None
        return field, "", many

    def _spans_many(self, lookup):
        return self._resolve_path(lookup)[2]

    def get_filters(self):
        """``{argument: lookup or f(ctx, queryset, value)}``."""
        if isinstance(self.filters, dict):
            return dict(self.filters)
        return {name: name for name in self.filters}

    def filter_annotation(self, spec):
        if callable(spec):
            return schema.type_hints(spec).get("value", str)
        field, lookup, _ = self._resolve_path(spec)
        if field.is_relation:
            base = field_annotation(field.related_model._meta.pk)
        elif field.choices or not field.null:
            base = field_annotation(field)
        else:
            base = _base_annotation(field)
        last = lookup.rsplit("__", 1)[-1] if lookup else ""
        if last in ("in", "range"):
            return list[base]
        if last == "isnull":
            return bool
        if last in _STRING_LOOKUPS:
            return str
        if last in _INT_LOOKUPS:
            return int
        return base

    def filter_queryset(self, ctx, queryset, values):
        many = False
        for name, spec in self.get_filters().items():
            value = values.get(name)
            if value is None:
                continue
            if callable(spec):
                queryset = spec(ctx, queryset, value)
            else:
                queryset = queryset.filter(**{spec: value})
                many = many or self._spans_many(spec)
        return queryset.distinct() if many else queryset

    def get_ordering(self):
        if self.ordering:
            return list(self.ordering)
        return list(self.opts.ordering) or ["pk"]

    def get_list_ordering(self, ctx):
        return self.get_ordering()

    def get_object(self, ctx, value, action="get"):
        queryset = self.get_queryset(ctx)
        try:
            instance = queryset.get(**{self.lookup_field: value})
        except self.model.DoesNotExist:
            raise Http404(
                f"No {self.opts.verbose_name} with {self.lookup_argument} {value!r}."
            ) from None
        if not self.has_object_permission(ctx, action, instance):
            raise PermissionDenied(
                f"You cannot {action} this {self.opts.verbose_name}."
            )
        return instance

    # --- serialization ------------------------------------------------------

    def get_serializer_context(self, ctx):
        return {"ctx": ctx, "request": ctx.request, "user": ctx.user}

    def serialize(self, ctx, value, many=False, detail=False):
        """``get`` and write results use the detail configuration when set."""
        serializer, fields = self.serializer, self.fields
        if detail and (self.detail_serializer or self.detail_fields) is not None:
            serializer, fields = self.detail_serializer, self.detail_fields
        if serializer is not None:
            context = self.get_serializer_context(ctx)
            return serializer(value, many=many, context=context).data
        if many:
            value = list(value)
            self.prefetch_labels(value, fields)
            return [self.to_dict(instance, fields) for instance in value]
        return self.to_dict(value, fields)

    def prefetch_labels(self, instances, names):
        """Load the labelled related records of a page in one query each."""
        if not instances or not self.get_related_labels():
            return
        lookups = []
        opts = instances[0]._meta
        for name in names:
            try:
                field = opts.get_field(name)
            except FieldDoesNotExist:
                continue
            if field.is_relation and (field.concrete or field.auto_created):
                if field.auto_created and not field.concrete:
                    name = field.get_accessor_name()
                lookups.append(name)
        if lookups:
            prefetch_related_objects(instances, *lookups)

    @staticmethod
    def label(related):
        return None if related is None else {"id": related.pk, "display": str(related)}

    def to_dict(self, instance, fields):
        return {name: self.field_value(instance, name) for name in fields}

    def field_value(self, instance, name):
        opts = instance._meta
        try:
            field = opts.pk if name == "pk" else opts.get_field(name)
        except FieldDoesNotExist:
            value = getattr(instance, name)
            return value() if callable(value) else value
        labels = field.is_relation and self.get_related_labels()
        if field.many_to_many or field.one_to_many:
            accessor = field.get_accessor_name() if field.auto_created else name
            rows = getattr(instance, accessor).all()
            if labels:
                return [self.label(related) for related in rows]
            return [related.pk for related in rows]
        if field.is_relation:
            if field.auto_created and not field.concrete:  # reverse one-to-one
                related = getattr(instance, field.get_accessor_name(), None)
                return self.label(related) if labels else getattr(related, "pk", None)
            if not field.concrete:  # GenericForeignKey: no column, any model
                related = getattr(instance, name, None)
                return None if related is None else str(related)
            if labels:
                return self.label(getattr(instance, name))
            return getattr(instance, field.attname)
        if isinstance(field, models.FileField):
            file = getattr(instance, name)
            if not file:
                return None
            try:
                return file.url
            except ValueError:
                return file.name
        return field.value_from_object(instance)

    # --- writes -------------------------------------------------------------

    def get_write_fields(self):
        if self.write_fields is not None:
            names = list(self.write_fields)
        elif self.fields is not None:
            names = [name for name in self.fields if self._is_writable(name)]
        else:
            names = [
                name
                for name, field in self.serializer().fields.items()
                if not field.read_only
            ]
        return names

    def _is_writable(self, name):
        return is_writable(self.opts, name)

    def write_parameters(self, partial):
        serializer_fields = {}
        if self.serializer is not None:
            serializer_fields = self.serializer().fields
        params = []
        for name in self.get_write_fields():
            required = None
            if name in serializer_fields:
                required = serializer_fields[name].required
            params.append(write_parameter(self.opts, name, partial, required))
        return params

    def validate(self, ctx, data, instance=None):
        """Validated data for ``perform_create`` / ``perform_update``."""
        if self.serializer is not None:
            serializer = self.serializer(
                instance=instance,
                data=data,
                partial=instance is not None,
                context=self.get_serializer_context(ctx),
            )
            serializer.is_valid(raise_exception=True)
            return dict(serializer.validated_data)
        form_class = modelform_factory(self.model, form=self.form, fields=list(data))
        form = form_class(data=data, instance=instance)
        if not form.is_valid():
            raise form_error(form)
        return {name: form.cleaned_data[name] for name in data}

    def _split_many(self, data):
        many = {}
        for name in list(data):
            try:
                field = self.opts.get_field(name)
            except FieldDoesNotExist:
                continue
            if field.many_to_many:
                many[name] = data.pop(name)
        return many

    def perform_create(self, ctx, data):
        many = self._split_many(data)
        instance = self.model(**data)
        instance.save()
        for name, values in many.items():
            getattr(instance, name).set(values)
        return instance

    def perform_update(self, ctx, instance, data):
        many = self._split_many(data)
        for name, value in data.items():
            setattr(instance, name, value)
        instance.save()
        for name, values in many.items():
            getattr(instance, name).set(values)
        return instance

    def perform_delete(self, ctx, instance):
        instance.delete()

    # --- actions ------------------------------------------------------------

    def run_list(
        self, ctx, query=None, limit=None, cursor=None, ordering=None, **filters
    ):
        if query is not None and not self.has_search():
            raise InvalidArguments(f"{self.get_name()} cannot be searched.")
        limit = self.page_size if limit is None else limit
        if not 1 <= limit <= self.max_page_size:
            raise InvalidArguments(f"limit must be between 1 and {self.max_page_size}.")
        offset = _decode_cursor(cursor) if cursor else 0
        queryset = self.get_queryset(ctx)
        if query:
            queryset = self.search(ctx, queryset, query)
        queryset = self.filter_queryset(ctx, queryset, filters)
        ordering = [ordering] if ordering else self.get_list_ordering(ctx)
        queryset = queryset.order_by(*ordering)
        count = queryset.count()
        page = list(queryset[offset : offset + limit])
        more = offset + limit < count
        return {
            "results": self.serialize(ctx, page, many=True),
            "count": count,
            "next_cursor": _encode_cursor(offset + limit) if more else None,
        }

    def run_get(self, ctx, **kwargs):
        instance = self.get_object(ctx, kwargs[self.lookup_argument], "get")
        return self.serialize(ctx, instance, detail=True)

    def run_create(self, ctx, **data):
        data = self.validate(ctx, data)
        with transaction.atomic():
            instance = self.perform_create(ctx, data)
        return self.serialize(ctx, instance, detail=True)

    def run_update(self, ctx, **kwargs):
        value = kwargs.pop(self.lookup_argument)
        instance = self.get_object(ctx, value, "update")
        data = self.validate(ctx, kwargs, instance)
        with transaction.atomic():
            instance = self.perform_update(ctx, instance, data)
        return self.serialize(ctx, instance, detail=True)

    def run_delete(self, ctx, **kwargs):
        value = kwargs[self.lookup_argument]
        instance = self.get_object(ctx, value, "delete")
        with transaction.atomic():
            self.perform_delete(ctx, instance)
        return {"deleted": True, self.lookup_argument: value}

    def run(self, ctx, action, **kwargs):
        return getattr(self, f"run_{action}")(ctx, **kwargs)

    # --- generated items ----------------------------------------------------

    def action_parameters(self, action):
        lookup = (self.lookup_argument, self.lookup_annotation, inspect.Parameter.empty)
        if action == "list":
            params = []
            if self.has_search():
                params.append(
                    ("query", Annotated[str | None, "Free text search"], None)
                )
            for name, spec in self.get_filters().items():
                params.append((name, self.filter_annotation(spec) | None, None))
            if self.ordering_fields:
                options = []
                for field in self.ordering_fields:
                    options += [field, f"-{field}"]
                params.append(
                    (
                        "ordering",
                        Annotated[Literal[tuple(options)] | None, "Sort order"],
                        None,
                    )
                )
            limit = f"Results per page, 1-{self.max_page_size}"
            params.append(("limit", Annotated[int, limit], self.page_size))
            cursor = "next_cursor from the previous page"
            params.append(("cursor", Annotated[str | None, cursor], None))
            return params
        if action in ("get", "delete"):
            return [lookup]
        if action == "create":
            return self.write_parameters(partial=False)
        # the lookup identifies the record, so update cannot change it
        fields = self.write_parameters(partial=True)
        return [lookup, *(param for param in fields if param[0] != lookup[0])]

    @cached_property
    def action_functions(self):
        """``{action: function}`` with computed signatures, for all actions."""
        functions = {}
        for action in self.actions:

            def impl(provider, ctx, _action=action, **kwargs):
                return provider.run(ctx, _action, **kwargs)

            functions[action] = make_function(
                f"{type(self).__qualname__}.{action}",
                type(self).__module__,
                self.action_parameters(action),
                impl,
            )
        return functions

    @cached_property
    def _argument_functions(self):
        return {}

    def argument_function(self, action):
        """Like the action's function but without the lookup argument: generic
        mode passes the lookup separately."""
        functions = self._argument_functions
        if action not in functions:
            params = [
                param
                for param in self.action_parameters(action)
                if action in ("list", "create") or param[0] != self.lookup_argument
            ]
            functions[action] = make_function(
                f"{type(self).__qualname__}.{action}",
                type(self).__module__,
                params,
                lambda provider, ctx, **kwargs: None,
            )
        return functions[action]

    def action_permission(self, action):
        return lambda ctx: self.has_action_permission(ctx, action)

    def get_action_specs(self):
        return [
            ToolSpec(
                self.action_functions[action],
                name=self.get_tool_name(action),
                description=self.get_description(action),
                permission=self.action_permission(action),
                **_HINTS[action],
            )
            for action in self.actions
        ]

    def get_resource_specs(self):
        if not self.resources:
            return []
        scheme = self.resource_scheme or self.opts.app_label
        name = self.get_name()
        specs = []
        if "list" in self.actions:

            def read_list(provider, ctx):
                return provider.run_list(ctx)

            func = make_function(
                f"{type(self).__qualname__}.{name}",
                type(self).__module__,
                [],
                read_list,
            )
            specs.append(
                ResourceSpec(
                    func,
                    f"{scheme}://{name}",
                    name=name,
                    description=f"The first page of {self.opts.verbose_name_plural}.",
                    mime_type="application/json",
                    permission=self.action_permission("list"),
                )
            )
        if "get" in self.actions:
            argument = self.lookup_argument

            def read_one(provider, ctx, **kwargs):
                return provider.run_get(ctx, **kwargs)

            func = make_function(
                f"{type(self).__qualname__}.{name}_detail",
                type(self).__module__,
                [(argument, self.lookup_annotation, inspect.Parameter.empty)],
                read_one,
            )
            spec = ResourceSpec(
                func,
                f"{scheme}://{name}/{{{argument}}}",
                name=f"{name}_detail",
                description=f"One {self.opts.verbose_name} by {argument}.",
                mime_type="application/json",
                permission=self.action_permission("get"),
            )
            if self.lookup_annotation is str:
                # unbound: completions are called with the provider, like methods
                spec.completions[argument] = type(self).complete_lookup
            specs.append(spec)
        return specs

    def complete_lookup(self, ctx, value):
        queryset = self.get_queryset(ctx).filter(
            **{f"{self.lookup_field}__istartswith": value}
        )
        return queryset.order_by(self.lookup_field).values_list(
            self.lookup_field, flat=True
        )[:100]

    def get_items(self):
        items = super().get_items()
        if self.get_exposure() == "tools":
            items += [Item(spec, self) for spec in self.get_action_specs()]
        items += [Item(spec, self) for spec in self.get_resource_specs()]
        return items

    # --- generic mode -------------------------------------------------------

    def describe(self, ctx):
        """What generic mode's ``models_list`` shows for this model."""
        actions = [action for action in self.actions if self.allows(ctx, action)]
        entry = {
            "model": self.get_name(),
            "name": str(self.opts.verbose_name),
            "lookup": self.lookup_argument,
            "actions": actions,
        }
        if self.fields is not None:
            entry["fields"] = list(self.fields)
        if "list" in actions:
            entry["search"] = self.has_search()
            entry["filters"] = {
                name: schema.annotation_schema(self.filter_annotation(spec))
                for name, spec in self.get_filters().items()
            }
        if "create" in actions or "update" in actions:
            entry["writable"] = {
                name: schema.annotation_schema(annotation)
                for name, annotation, _ in self.write_parameters(partial=True)
            }
        return entry

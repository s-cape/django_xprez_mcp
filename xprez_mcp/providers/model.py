"""``ModelProvider``: tools and resources generated from a Django model."""

import base64
import inspect
import mimetypes
import posixpath
from contextlib import contextmanager
from functools import cached_property
from typing import Annotated, Any, Literal, Optional

from django.core.exceptions import (
    FieldDoesNotExist,
    ImproperlyConfigured,
    PermissionDenied,
    SuspiciousFileOperation,
)
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models, transaction
from django.db.models import prefetch_related_objects
from django.db.models import Q
from django.forms import ModelForm, modelform_factory
from django.http import Http404
from django.utils.datastructures import MultiValueDict
from django.utils.text import slugify

from ..core import files as file_arguments
from ..core import schema
from ..core.files import DEFAULT_SOURCES, SOURCES, FileValue
from ..core.items import Item, ResourceSpec, ToolSpec
from ..errors import InvalidArguments, ToolError
from ..permissions import allows, validate_permission
from .base import Provider
from .fields import (
    base_annotation,
    field_annotation,
    file_field,
    is_writable,
    upload_prefix,
    write_parameter,
)

ACTIONS = ("list", "get", "create", "update", "delete")
WRITE_ACTIONS = ("create", "update", "delete")
EXPOSURES = ("tools", "generic")
# any one suffices: like the admin, change permission also lets you read
MODEL_PERMISSIONS = {
    "list": ("view", "change"),
    "get": ("view", "change"),
    "create": ("add",),
    "update": ("change",),
    "delete": ("delete",),
}
RESERVED_ARGUMENTS = {"query", "limit", "cursor", "ordering"}

HINTS = {
    "list": {"read_only": True},
    "get": {"read_only": True},
    "create": {},
    "update": {"idempotent": True},
    "delete": {"destructive": True},
    "admin_action": {},
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


def signature_function(qualname, module, params):
    """A function with ``params`` as its signature and no body, for validation."""
    return make_function(qualname, module, params, lambda provider, ctx, **kwargs: None)


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
    name_template = "{plural}"  # without name; also {app_label}, {model_name}
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
    exposure = None  # "tools" | "generic"; None follows MCPServer(exposure=...)
    related_labels = None  # None follows MCPServer(related_labels=...)
    page_size = 20
    max_page_size = 100
    file_sources = DEFAULT_SOURCES  # ["data"]; "path" and "url" are opt-in
    max_file_size = 10 * 1024 * 1024  # bytes, uploads and fetches; None: no limit
    file_fetch_timeout = 10  # seconds

    # require the model's view/add/change/delete permission per action, as
    # the admin does, on top of permissions
    model_permissions = False

    def __init__(self, server, model=None):
        super().__init__(server, model=model or type(self).model)
        if self.model is None:
            raise ImproperlyConfigured(
                f"{type(self).__qualname__} needs a model: register it with "
                "mcp.register(Model, Provider) or set Provider.model."
            )
        self.configure()
        self.check()

    def configure(self):
        """Subclass hook, run once the model is known and before ``check``."""

    # --- configuration ------------------------------------------------------

    @property
    def opts(self):
        return self.model._meta

    def check(self):
        label = type(self).__qualname__
        if not getattr(self.model, "mcp_exposed", True):
            raise ImproperlyConfigured(
                f"{label}: {self.opts.label} sets mcp_exposed = False."
            )
        unknown = set(self.actions) - set(ACTIONS)
        if unknown:
            raise ImproperlyConfigured(f"{label}: unknown actions {sorted(unknown)}.")
        self.get_permissions()  # raises on a value that is not a permission
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
        unknown = set(self.file_sources) - set(SOURCES)
        declared = getattr(type(self), "write_fields", None) or ()
        if not self.file_sources and any(file_field(self.opts, n) for n in declared):
            raise ImproperlyConfigured(
                f"{label}: write_fields names file fields but file_sources is "
                "empty; add a source or leave them out of write_fields."
            )
        if unknown:
            raise ImproperlyConfigured(
                f"{label}: unknown file_sources {sorted(unknown)}; use {SOURCES}."
            )
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
        return self.exposure or self.server.exposure

    def get_name(self):
        if self.name:
            return self.name
        return self.name_template.format(
            app_label=self.opts.app_label,
            model_name=self.opts.model_name,
            plural=slugify(str(self.opts.verbose_name_plural)).replace("-", "_"),
        )

    @property
    def lookup_argument(self):
        return "id" if self.lookup_field == "pk" else self.lookup_field

    @property
    def lookup_name(self):
        """The field in results whose value is passed as ``id``."""
        if self.lookup_field == "pk":
            return self.opts.pk.name
        return self.lookup_field

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
        """``permissions``, each validated: ``{action: f(ctx) -> bool or bool}``."""
        label = type(self).__qualname__
        return {
            action: validate_permission(value, f"{label}.permissions[{action!r}]")
            for action, value in self.permissions.items()
        }

    def has_model_permission(self, ctx, action):
        """The model permission the admin requires for ``action``."""
        codenames = MODEL_PERMISSIONS.get(action)
        if codenames is None:
            return True
        user = ctx.user
        return user is not None and any(
            user.has_perm(f"{self.opts.app_label}.{codename}_{self.opts.model_name}")
            for codename in codenames
        )

    def writes_allowed(self):
        """Writes of third-party providers need ``ALLOW_WRITES``."""
        return self.server.provider_writes_allowed(self)

    def has_action_permission(self, ctx, action):
        if action not in self.actions:
            return False
        if action in WRITE_ACTIONS and not self.writes_allowed():
            return False
        if self.model_permissions and not self.has_model_permission(ctx, action):
            return False
        return allows(self.get_permissions().get(action), ctx)

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
            base = base_annotation(field)
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

    def with_pk_tiebreak(self, ordering):
        """``ordering`` ending in the pk: cursors are offsets, so ties repeat rows."""
        names = {o.lstrip("-") for o in ordering if isinstance(o, str)}
        if {"pk", self.opts.pk.name} & names:
            return list(ordering)
        else:
            return list(ordering) + ["pk"]

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
                if _supports_prefetch(instances[0], name):
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
            required = nullable = None
            as_file = True
            if name in serializer_fields:
                required = serializer_fields[name].required
                # null is passed to the serializer, which decides
                nullable = getattr(serializer_fields[name], "allow_null", False)
                as_file = _is_serializer_file_field(serializer_fields[name])
            if as_file and not self.file_sources and file_field(self.opts, name):
                continue  # no source: file fields are read-only
            params.append(
                write_parameter(
                    self.opts,
                    name,
                    partial,
                    required,
                    self.file_sources,
                    nullable,
                    as_file,
                )
            )
        return params

    def validate(self, ctx, data, instance=None):
        """Validated data for ``perform_create`` / ``perform_update``."""
        data, files = self.resolve_files(ctx, data)
        if self.serializer is not None:
            for name, file in files.items():
                data[name] = None if file is False else file
            serializer = self.serializer(
                instance=instance,
                data=data,
                partial=instance is not None,
                context=self.get_serializer_context(ctx),
            )
            with closing_stored_files(files):
                serializer.is_valid(raise_exception=True)
            return dict(serializer.validated_data)
        names = [*data, *files]
        form_class = modelform_factory(self.model, form=self.form, fields=names)
        form = form_class(data=data, files=form_files(files), instance=instance)
        with closing_stored_files(files):
            valid = form.is_valid()
        if not valid:
            raise form_error(form)
        check_cleared(form, files)
        cleaned = {name: form.cleaned_data[name] for name in names}
        for name in files:
            if cleaned[name] is False:  # cleared: the model stores an empty name
                cleaned[name] = ""
        return cleaned

    # --- files --------------------------------------------------------------

    def resolve_files(self, ctx, data, opts=None):
        """``(data, files)``: file arguments taken out of ``data`` as files a form
        accepts, ``False`` where the argument was ``null`` (clear)."""
        opts = opts or self.opts
        data, files = dict(data), {}
        for name, value in list(data.items()):
            field = file_field(opts, name)
            if field is None or not (value is None or isinstance(value, FileValue)):
                continue
            del data[name]
            files[name] = (
                False if value is None else self.resolve_file(ctx, field, value)
            )
        return data, files

    def resolve_file(self, ctx, field, value):
        """An uploaded file (``data``, ``url``) or a stored one (``path``)."""
        if value.source == "path":
            return self.stored_file(ctx, field, value.value)
        content_type = None
        if value.source == "url":
            content, name, content_type = self.fetch_file(ctx, field, value.value)
            # the client did not choose this name, so it must not fail the form
            limit = field.max_length
            if limit is not None:
                limit = max(limit - len(upload_prefix(field) or ""), 8)
            name = file_arguments.shorten_name(name, limit)
        else:
            content, name = value.value, value.name
        if self.max_file_size is not None and len(content) > self.max_file_size:
            raise ToolError(
                f"{field.name}: the file is larger than {self.max_file_size} bytes."
            )
        if content_type is None:
            content_type = mimetypes.guess_type(name)[0]
        # a field file not yet saved, so a failed write can delete what the
        # save stored (see removing_uploads_on_error)
        upload = field.attr_class(None, field, name)
        upload.file = SimpleUploadedFile(name, content, content_type)
        upload._committed = False
        upload._mcp_upload = True
        return upload

    @contextmanager
    def removing_uploads_on_error(self, values):
        """Delete files a failed write already stored: the transaction rolls
        back the rows but not the storage."""
        uploads = [value for value in values if getattr(value, "_mcp_upload", False)]
        try:
            yield
        except BaseException:
            for upload in uploads:
                if upload._committed and upload.name:
                    upload.storage.delete(upload.name)
            raise

    def stored_file(self, ctx, field, path):
        """The file at ``path`` in the field's storage, assigned without a copy.

        Only paths ``allows_file_path`` accepts; remove ``"path"`` from
        ``file_sources`` to refuse them all.
        """
        if not self.allows_file_path(ctx, field, path):
            prefix = upload_prefix(field)
            if type(self).allows_file_path is not ModelProvider.allows_file_path:
                raise ToolError(f"{field.name}: path {path!r} is not allowed.")
            if prefix is None:
                raise ToolError(f"{field.name} does not accept stored paths.")
            raise ToolError(f"{field.name}: path must be under {prefix!r}.")
        storage = field.storage
        try:
            exists = storage.exists(path)
        except SuspiciousFileOperation:
            exists = False
        if exists:
            try:
                directories = storage.listdir(posixpath.dirname(path))[0]
            except (NotImplementedError, OSError):
                directories = []
            exists = posixpath.basename(path) not in directories
        if not exists:
            raise ToolError(f"{field.name}: no file at {path!r}.")
        # validation reads the file (an ImageField all of it), so bound it
        if self.max_file_size is not None and storage.size(path) > self.max_file_size:
            raise ToolError(
                f"{field.name}: the file is larger than {self.max_file_size} bytes."
            )
        return field.attr_class(None, field, path)

    def allows_file_path(self, ctx, field, path):
        """Whether ``path`` may be referenced: by default only files under the
        static folder of the field's ``upload_to``, where its own uploads go,
        so a record cannot point at another field's (private) files. A
        callable or empty ``upload_to`` accepts no path until overridden."""
        prefix = upload_prefix(field)
        return prefix is not None and path.startswith(prefix)

    def fetch_file(self, ctx, field, url):
        """``(content, name, content_type)`` for a ``url`` argument.

        Only http(s) to public addresses, no proxies; override for an allowlist
        of hosts or a fetcher of your own.
        """
        return file_arguments.fetch(
            url, max_size=self.max_file_size, timeout=self.file_fetch_timeout
        )

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

    def paginate_queryset(self, queryset, limit, cursor, serialize):
        """``{results, count, next_cursor}`` for one page of ``queryset``."""
        limit = self.page_size if limit is None else limit
        if not 1 <= limit <= self.max_page_size:
            raise InvalidArguments(f"limit must be between 1 and {self.max_page_size}.")
        offset = _decode_cursor(cursor) if cursor else 0
        count = queryset.count()
        page = list(queryset[offset : offset + limit])
        more = offset + limit < count
        return {
            "results": serialize(page),
            "count": count,
            "next_cursor": _encode_cursor(offset + limit) if more else None,
        }

    def run_list(
        self, ctx, query=None, limit=None, cursor=None, ordering=None, **filters
    ):
        if query is not None and not self.has_search():
            raise InvalidArguments(f"{self.get_name()} cannot be searched.")
        queryset = self.get_queryset(ctx)
        if query:
            queryset = self.search(ctx, queryset, query)
        queryset = self.filter_queryset(ctx, queryset, filters)
        ordering = [ordering] if ordering else self.get_list_ordering(ctx)
        queryset = queryset.order_by(*self.with_pk_tiebreak(ordering))
        return self.paginate_queryset(
            queryset, limit, cursor, lambda page: self.serialize(ctx, page, many=True)
        )

    def run_get(self, ctx, **kwargs):
        instance = self.get_object(ctx, kwargs[self.lookup_argument], "get")
        return self.serialize(ctx, instance, detail=True)

    def run_create(self, ctx, **data):
        data = self.validate(ctx, data)
        with self.removing_uploads_on_error(data.values()), transaction.atomic():
            instance = self.perform_create(ctx, data)
        return self.serialize(ctx, instance, detail=True)

    def run_update(self, ctx, **kwargs):
        value = kwargs.pop(self.lookup_argument)
        instance = self.get_object(ctx, value, "update")
        data = self.validate(ctx, kwargs, instance)
        with self.removing_uploads_on_error(data.values()), transaction.atomic():
            instance = self.perform_update(ctx, instance, data)
        return self.serialize(ctx, instance, detail=True)

    def run_delete(self, ctx, **kwargs):
        value = kwargs[self.lookup_argument]
        instance = self.get_object(ctx, value, "delete")
        with transaction.atomic():
            self.perform_delete(ctx, instance)
        return {"deleted": True, self.lookup_argument: value}

    def run(self, ctx, action, /, **kwargs):
        """``action`` is positional-only so a filter or field may be named action."""
        return getattr(self, f"run_{action}")(ctx, **kwargs)

    # --- generated items ----------------------------------------------------

    def action_parameters(self, action):
        lookup = (self.lookup_argument, self.lookup_annotation, inspect.Parameter.empty)
        if action == "list":
            params = []
            if self.has_search():
                params.append(
                    ("query", Annotated[Optional[str], "Free text search"], None)
                )
            for name, spec in self.get_filters().items():
                params.append((name, Optional[self.filter_annotation(spec)], None))
            if self.ordering_fields:
                options = []
                for field in self.ordering_fields:
                    options += [field, f"-{field}"]
                params.append(
                    (
                        "ordering",
                        Annotated[Optional[Literal[tuple(options)]], "Sort order"],
                        None,
                    )
                )
            limit = f"Results per page, 1-{self.max_page_size}"
            params.append(("limit", Annotated[int, limit], self.page_size))
            cursor = "next_cursor from the previous page"
            params.append(("cursor", Annotated[Optional[str], cursor], None))
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
            functions[action] = signature_function(
                f"{type(self).__qualname__}.{action}",
                type(self).__module__,
                params,
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
                **HINTS[action],
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
        if not self.allows(ctx, "list"):
            return []
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
            "lookup": self.lookup_name,
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


def form_files(files):
    """``files`` for a form: a ``MultiValueDict``, as widgets taking several
    files call ``getlist``."""
    return MultiValueDict({name: [file] for name, file in files.items()})


@contextmanager
def closing_stored_files(files):
    """Close the stored files (``path``) that validation opened; uploads stay
    open for the save."""
    try:
        yield
    finally:
        for file in files.values():
            if file and getattr(file, "_committed", False):
                file.close()


def check_cleared(form, files, prefix=""):
    """Refuse a ``null`` the form ignored: a required form field keeps its
    file when sent ``False``."""
    for name, file in files.items():
        if file is False and form.cleaned_data.get(name):
            raise ToolError(f"{prefix}{name} is required and cannot be cleared.")


def _is_serializer_file_field(field):
    """A DRF ``FileField`` (``ImageField`` included); other serializer fields
    on a model file field (base64 strings, URLs) keep their own values."""
    try:
        from rest_framework.fields import FileField
    except ImportError:
        return False
    return isinstance(field, FileField)


def _supports_prefetch(instance, name):
    """Whether Django can prefetch the accessor; django-polymorphic replaces
    parent links with plain properties, which it cannot."""
    accessor = getattr(type(instance), name, None)
    if isinstance(accessor, property):  # reading it would query
        return False
    if not _is_prefetcher(accessor):  # to-many: the manager prefetches
        accessor = getattr(instance, name, None)
    return _is_prefetcher(accessor)


def _is_prefetcher(value):
    # get_prefetch_queryset is Django < 5.0, get_prefetch_querysets 5.0+
    return hasattr(value, "get_prefetch_querysets") or hasattr(
        value, "get_prefetch_queryset"
    )

"""``AdminProvider``: a ``ModelProvider`` driven by a ``ModelAdmin``.

Every permission, queryset, form and save goes through the ``ModelAdmin``, so a
user can do through MCP exactly what they can do in that admin.
"""

import datetime
import decimal
import inspect
import uuid
from functools import cached_property
from typing import Annotated, Literal, Optional, Union

import django
from django.contrib import admin
from django.contrib.admin.utils import flatten, flatten_fieldsets, lookup_field
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import (
    FieldDoesNotExist,
    ImproperlyConfigured,
    ObjectDoesNotExist,
    PermissionDenied,
)
from django.db import models, transaction
from django.forms.models import _get_foreign_key
from django.http import Http404, HttpRequest
from django.utils.datastructures import MultiValueDict
from django.utils.text import slugify

from ...core import schema
from ...core.items import Item, ToolSpec
from ...errors import InvalidArguments, ToolError
from ..fields import file_field, is_writable, write_parameter
from ..model import (
    check_cleared,
    closing_stored_files,
    HINTS,
    ModelProvider,
    form_error,
    form_files,
    make_function,
    signature_function,
)
from . import request as admin_requests

INLINE_PERMISSIONS = {
    "create": "has_add_permission",
    "update": "has_change_permission",
    "delete": "has_delete_permission",
}
INLINE_OPERATIONS = tuple(INLINE_PERMISSIONS)
INLINE_ACTIONS = {f"inline_{operation}": operation for operation in INLINE_PERMISSIONS}
_PLAIN = (
    str,
    int,
    float,
    bool,
    type(None),
    list,
    dict,
    datetime.date,
    datetime.time,
    decimal.Decimal,
    uuid.UUID,
)


def plain(value):
    """A value the JSON encoder can handle; anything else as its string."""
    if isinstance(value, models.Model):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, str):
        return str(value)  # SafeString and lazy strings
    if isinstance(value, _PLAIN):
        return value
    return str(value)


class AdminProvider(ModelProvider):
    """Tools generated from the ``ModelAdmin`` registered for the model.

    Only ``list`` and ``get`` are exposed by default; ``actions`` opts in to
    writes, ``admin_actions`` to ``ModelAdmin`` actions.
    """

    admin_site = None  # default: django.contrib.admin.site
    admin_class = None  # default: the ModelAdmin registered on admin_site
    admin_actions = ()
    inlines = None  # InlineModelAdmin classes; None: all of the ModelAdmin's
    inline_names = {}
    hidden_fields = ()  # never read nor written, with what the admin's form masks

    def __init__(self, server, model=None):
        super().__init__(server, model=model)
        self.exposed_inlines  # resolves inlines now, filling unsupported

    # --- configuration ------------------------------------------------------

    @property
    def site(self):
        return self.model_admin.admin_site

    def get_model_admin(self):
        site = self.admin_site or admin.site
        if self.admin_class is not None:
            return self.admin_class(self.model, site)
        try:
            return site._registry[self.model]
        except KeyError:
            raise ImproperlyConfigured(
                f"{type(self).__qualname__}: {self.model._meta.label} is not "
                f"registered on {site!r}."
            ) from None

    def configure(self):
        """Fill what the class leaves unset from the ``ModelAdmin``."""
        self.model_admin = self.get_model_admin()
        self.unsupported = []
        label = type(self).__qualname__
        if self.permissions:
            raise ImproperlyConfigured(
                f"{label}: AdminProvider uses the admin's permissions; "
                "permissions is not supported."
            )
        if "delete_selected" in self.admin_actions:
            raise ImproperlyConfigured(f"{label}: delete_selected is never exposed.")
        model_admin = self.model_admin
        if not getattr(model_admin, "mcp_exposed", True):
            raise ImproperlyConfigured(
                f"{label}: {type(model_admin).__qualname__} sets mcp_exposed = False."
            )
        # list columns and filters then follow the admin's get_* per call
        self.fields_from_admin = self.fields is None and self.serializer is None
        if self.fields_from_admin:
            self.fields = self.admin_list_fields(model_admin.list_display)
        if not self.search_fields:
            self.search_fields = tuple(model_admin.search_fields)
        self.filters_from_admin = not self.filters
        if self.filters_from_admin:
            self.filters, unsupported = self.admin_filters(model_admin.list_filter)
            self.unsupported += unsupported
        if self.write_fields is None and self.serializer is None:
            self.write_fields = self.admin_write_fields()
        hidden = self.get_hidden_fields(model_admin)
        # matching on a hidden field would reveal it through counts
        self.search_fields = tuple(
            name for name in self.search_fields if not _touches(name, hidden)
        )
        self.filters = {
            name: spec
            for name, spec in self.get_filters().items()
            if callable(spec) or not _touches(spec, hidden)
        }
        if self.fields is not None:
            self.fields = [name for name in self.fields if name not in hidden]
        if self.write_fields is not None:
            self.write_fields = [
                name for name in self.write_fields if name not in hidden
            ]

    def get_hidden_fields(self, model_admin):
        """``hidden_fields`` and the fields the admin's form shows masked, on the
        model and its inlines."""
        hidden = set(self.hidden_fields)
        for name, field in model_admin.form.base_fields.items():
            if isinstance(field, ReadOnlyPasswordHashField):
                hidden.add(name)
        return hidden

    @cached_property
    def display_callables(self):
        return _display_callables(self.model_admin.list_display)

    def list_display(self, ctx):
        """``(fields, callables)`` of the list results this user gets."""
        if not self.fields_from_admin:
            return self.fields, self.display_callables
        entries = self.model_admin.get_list_display(self.admin_request(ctx))
        hidden = self.get_hidden_fields(self.model_admin)
        fields = [
            name for name in self.admin_list_fields(entries) if name not in hidden
        ]
        return fields, _display_callables(entries)

    def admin_list_fields(self, list_display):
        names = [self.lookup_name]
        for entry in list_display:
            if entry == "action_checkbox":
                continue
            if callable(entry):
                name = entry.__name__
            elif entry == "__str__":
                name = "display"
            else:
                name = entry
            if name not in names:
                names.append(name)
        return names

    def static_request(self):
        """For reading the admin's configuration outside a call."""
        request = HttpRequest()
        request.user = AnonymousUser()
        request.current_app = self.site.name
        return request

    def static_fields(self, model_admin, obj=None):
        """``(fields, readonly_fields)`` without a user, for schemas."""
        request = self.static_request()
        try:
            fields = flatten(model_admin.get_fields(request, obj))
            readonly = list(model_admin.get_readonly_fields(request, obj))
        except Exception:  # an override that needs a real user
            if model_admin.fieldsets:
                fields = flatten_fieldsets(model_admin.fieldsets)
            else:
                fields = flatten(model_admin.fields or [])
            readonly = list(model_admin.readonly_fields)
        return fields, readonly

    def admin_write_fields(self):
        fields, readonly = self.static_fields(self.model_admin)
        if not fields:
            fields = [field.name for field in self.opts.get_fields()]
        return [
            name
            for name in fields
            if name not in readonly and is_writable(self.opts, name)
        ]

    def admin_filters(self, list_filter):
        """``(filters, unsupported entries)`` of a ``list_filter``."""
        filters, unsupported = {}, []
        for entry in list_filter:
            if isinstance(entry, (list, tuple)):
                entry = entry[0]  # (field, FieldListFilter): filter by the field
            if isinstance(entry, str):
                try:
                    field, rest, _ = self._resolve_path(entry)
                except ImproperlyConfigured:
                    field, rest = None, "?"
                if field is None or rest:
                    unsupported += [f"list_filter {entry!r}"]
                    continue
                if isinstance(field, models.DateField) and not field.is_relation:
                    filters[f"{entry}__gte"] = f"{entry}__gte"
                    filters[f"{entry}__lte"] = f"{entry}__lte"
                else:
                    filters[entry] = entry
            elif isinstance(entry, type) and issubclass(entry, admin.SimpleListFilter):
                filters[entry.parameter_name] = self.simple_filter(entry)
            else:
                unsupported += [f"list_filter {entry!r}"]
        return filters, unsupported

    def simple_filter(self, filter_class):
        name = filter_class.parameter_name

        def apply(ctx, queryset, value: Annotated[str, str(filter_class.title)]):
            request = self.admin_request(ctx)
            param = [value] if django.VERSION >= (5, 0) else value
            instance = filter_class(
                request, {name: param}, self.model, self.model_admin
            )
            options = [str(key) for key, _ in instance.lookup_choices]
            if value not in options:
                raise InvalidArguments(f"{name} must be one of {options}.")
            result = instance.queryset(request, queryset)
            return queryset if result is None else result

        apply.__name__ = name
        return apply

    # --- the request --------------------------------------------------------

    def admin_request(self, ctx, write=False, data=None, files=None):
        """A copy of the MCP request for admin code, one per call and site."""
        factory = None
        if type(self).get_admin_request is not AdminProvider.get_admin_request:
            factory = self.get_admin_request
        return admin_requests.get_admin_request(
            ctx, self.site, write=write, data=data, files=files, factory=factory
        )

    def get_admin_request(self, ctx):
        return admin_requests.build_admin_request(ctx, self.site)

    def with_messages(self, ctx, result):
        return admin_requests.with_messages(ctx, self.site, result)

    # --- permissions --------------------------------------------------------

    def has_permission(self, ctx):
        request = self.admin_request(ctx)
        return self.site.has_permission(
            request
        ) and self.model_admin.has_module_permission(request)

    def has_admin_permission(self, request, action, obj=None):
        model_admin = self.model_admin
        if action in ("list", "get"):
            return model_admin.has_view_or_change_permission(request, obj)
        if action == "create":
            return model_admin.has_add_permission(request)
        if action == "update":
            return model_admin.has_change_permission(request, obj)
        if action == "delete":
            return model_admin.has_delete_permission(request, obj)
        return False

    def has_action_permission(self, ctx, action):
        if action == "admin_action":
            return self.writes_allowed() and bool(self.available_admin_actions(ctx))
        if action == "inline_list":
            return bool(self.exposed_inlines) and self.has_action_permission(ctx, "get")
        if action.startswith("inline_"):
            return bool(self.exposed_inlines) and self.has_action_permission(
                ctx, "update"
            )
        if not super().has_action_permission(ctx, action):
            return False
        else:
            return self.has_admin_permission(self.admin_request(ctx), action)

    def has_object_permission(self, ctx, action, instance):
        return self.has_admin_permission(self.admin_request(ctx), action, instance)

    def generic_actions(self):
        found = set(self.actions)
        if self.admin_actions:
            found.add("admin_action")
        if self.exposed_inlines and "get" in self.actions:
            found.add("inline_list")
        if self.exposed_inlines and "update" in self.actions:
            found.update(INLINE_ACTIONS.keys())
        return found

    # --- reads --------------------------------------------------------------

    def get_queryset(self, ctx):
        return self.model_admin.get_queryset(self.admin_request(ctx))

    def has_search(self):
        return bool(self.search_fields) or type(self).search is not AdminProvider.search

    def search(self, ctx, queryset, query):
        request = self.admin_request(ctx)
        hidden = self.get_hidden_fields(self.model_admin)
        search_fields = self.model_admin.get_search_fields(request)
        if any(_touches(name, hidden) for name in search_fields):
            return super().search(ctx, queryset, query)  # without the hidden ones
        queryset, duplicates = self.model_admin.get_search_results(
            request, queryset, query
        )
        return queryset.distinct() if duplicates else queryset

    def filter_queryset(self, ctx, queryset, values):
        if self.filters_from_admin:
            entries = self.model_admin.get_list_filter(self.admin_request(ctx))
            offered, _ = self.admin_filters(entries)
            refused = sorted(
                name
                for name, value in values.items()
                if value is not None and name not in offered
            )
            if refused:
                raise InvalidArguments(f"You cannot filter by {', '.join(refused)}.")
        return super().filter_queryset(ctx, queryset, values)

    def get_list_ordering(self, ctx):
        ordering = self.model_admin.get_ordering(self.admin_request(ctx))
        return list(ordering) if ordering else self.get_ordering()

    def field_value(self, instance, name, callables=None):
        if callables is None:
            callables = self.display_callables
        if name == "display":
            return str(instance)
        # errors the admin shows as its empty value: the changelist catches
        # ObjectDoesNotExist, read-only fields AttributeError and ValueError too
        if name in callables:
            try:
                return plain(callables[name](instance))
            except ObjectDoesNotExist:
                return None
        return self._admin_field_value(instance, name, self.model_admin)

    def inline_field_value(self, inline, instance, name):
        return self._admin_field_value(instance, name, inline)

    def _admin_field_value(self, instance, name, model_admin):
        try:
            instance._meta.get_field(name)
        except FieldDoesNotExist:
            if name == "pk":
                return instance.pk
            try:
                _, _, value = lookup_field(name, instance, model_admin)
            except (AttributeError, ValueError, ObjectDoesNotExist):
                return None
            return plain(value)
        # field types with their own value classes (timezones, money, ...)
        return plain(super().field_value(instance, name))

    def detail_names(self, request, obj):
        names = [self.lookup_name]
        hidden = self.get_hidden_fields(self.model_admin)
        for name in flatten(self.model_admin.get_fields(request, obj)):
            if name not in names and name not in hidden:
                names.append(name)
        return names

    def serialize(self, ctx, value, many=False, detail=False):
        if many and self.fields_from_admin:
            names, callables = self.list_display(ctx)
            rows = list(value)
            self.prefetch_labels(rows, names)
            return [
                {
                    name: self.field_value(row, name, callables=callables)
                    for name in names
                }
                for row in rows
            ]
        configured = self.detail_serializer or self.detail_fields
        if many or not detail or self.serializer is not None or configured:
            return super().serialize(ctx, value, many=many, detail=detail)
        request = self.admin_request(ctx)
        names = self.detail_names(request, value)
        data = {name: self.field_value(value, name) for name in names}
        inlines = self.serialize_inlines(ctx, request, value)
        if inlines:
            data["inlines"] = inlines
        return data

    def run_list(self, ctx, **kwargs):
        return self.with_messages(ctx, super().run_list(ctx, **kwargs))

    def run_get(self, ctx, **kwargs):
        return self.with_messages(ctx, super().run_get(ctx, **kwargs))

    # --- writes -------------------------------------------------------------

    def check_settable(self, request, names, instance):
        """Refuse fields the admin does not let this user edit."""
        model_admin = self.model_admin
        allowed = set(flatten_fieldsets(model_admin.get_fieldsets(request, instance)))
        allowed -= set(model_admin.get_readonly_fields(request, instance))
        allowed -= self.get_hidden_fields(model_admin)
        refused = sorted(name for name in names if name not in allowed)
        if refused:
            raise ToolError(f"You cannot set {', '.join(refused)}.")

    def admin_form(self, request, data, instance, files=None):
        """A bound admin form for just the fields sent, validated."""
        files = files or {}
        self.check_settable(request, [*data, *files], instance)
        return self.bound_admin_form(request, data, instance, files)

    def bound_admin_form(self, request, data, instance, files):
        """``admin_form`` once ``check_settable`` has passed."""
        names = [*data, *files]
        form_class = self.model_admin.get_form(
            request, instance, change=instance is not None, fields=names
        )
        form = form_class(data=data, files=form_files(files), instance=instance)
        with closing_stored_files(files):
            valid = form.is_valid()
        if not valid:
            raise form_error(form)
        check_cleared(form, files)
        return form

    def admin_write(self, ctx, data, instance):
        """``(request, form)`` of a create or an update, files resolved.

        The files go to the form only: the request stays the call's shared one,
        so what admin code sets on it is seen by the rest of the call.
        """
        request = self.admin_request(ctx, write=True)
        self.check_settable(request, data, instance)
        data, files = self.resolve_files(ctx, data)
        return request, self.bound_admin_form(request, data, instance, files)

    def run_create(self, ctx, **data):
        request, form = self.admin_write(ctx, data, None)
        model_admin = self.model_admin
        uploads = form.cleaned_data.values()
        with self.removing_uploads_on_error(uploads), transaction.atomic():
            obj = model_admin.save_form(request, form, change=False)
            model_admin.save_model(request, obj, form, change=False)
            model_admin.save_related(request, form, [], change=False)
            message = model_admin.construct_change_message(request, form, [], add=True)
            model_admin.log_addition(request, obj, message)
        return self.with_messages(ctx, self.serialize(ctx, obj, detail=True))

    def run_update(self, ctx, **kwargs):
        value = kwargs.pop(self.lookup_argument)
        instance = self.get_object(ctx, value, "update")
        request, form = self.admin_write(ctx, kwargs, instance)
        model_admin = self.model_admin
        uploads = form.cleaned_data.values()
        with self.removing_uploads_on_error(uploads), transaction.atomic():
            obj = model_admin.save_form(request, form, change=True)
            model_admin.save_model(request, obj, form, change=True)
            model_admin.save_related(request, form, [], change=True)
            message = model_admin.construct_change_message(request, form, [])
            model_admin.log_change(request, obj, message)
        return self.with_messages(ctx, self.serialize(ctx, obj, detail=True))

    def run_delete(self, ctx, **kwargs):
        value = kwargs[self.lookup_argument]
        instance = self.get_object(ctx, value, "delete")
        request = self.admin_request(ctx, write=True)
        model_admin = self.model_admin
        _, _, perms_needed, protected = model_admin.get_deleted_objects(
            [instance], request
        )
        name = self.opts.verbose_name
        if perms_needed:
            raise PermissionDenied(
                f"Deleting this {name} would also delete "
                f"{', '.join(sorted(map(str, perms_needed)))}, which you cannot "
                "delete."
            )
        if protected:
            raise ToolError(
                f"This {name} cannot be deleted: {len(protected)} protected "
                "related objects."
            )
        with transaction.atomic():
            if hasattr(model_admin, "log_deletions"):  # Django 5.1+
                model_admin.log_deletions(request, [instance])
            else:
                model_admin.log_deletion(request, instance, str(instance))
            model_admin.delete_model(request, instance)
        return self.with_messages(ctx, {"deleted": True, self.lookup_argument: value})

    # --- admin actions ------------------------------------------------------

    def available_admin_actions(self, ctx):
        """``{name: (func, name, description)}`` of ``admin_actions`` this user
        may run, as ``get_actions`` decides."""
        if not self.admin_actions:
            return {}
        actions = self.model_admin.get_actions(self.admin_request(ctx))
        return {name: actions[name] for name in self.admin_actions if name in actions}

    def find_admin_action(self, name):
        """``(func, name, description)``: ``actions`` entries may be functions."""
        model_admin = self.model_admin
        for entry in model_admin.actions or ():
            found = model_admin.get_action(entry)
            if found is not None and found[1] == name:
                return found
        return model_admin.get_action(name)

    def action_description(self, name):
        found = self.find_admin_action(name)
        if found is None:
            raise ImproperlyConfigured(
                f"{type(self).__qualname__}: unknown admin action {name!r}."
            )
        description = found[2]
        try:
            return str(description) % admin.utils.model_format_dict(self.opts)
        except (TypeError, KeyError, ValueError):
            return str(description)

    def run_admin_action(self, ctx, action, ids):
        available = self.available_admin_actions(ctx)
        if action not in available:
            raise InvalidArguments(
                f"Unknown action {action!r}. Available: {sorted(available)}."
            )
        func = available[action][0]
        request = self.admin_request(ctx, write=True)
        queryset = self.get_queryset(ctx).filter(**{f"{self.lookup_field}__in": ids})
        count = queryset.count()
        if not count:
            raise ToolError(f"No {self.opts.verbose_name_plural} match ids.")
        with transaction.atomic():
            response = func(self.model_admin, request, queryset)
            if response is not None:  # raising rolls the action back
                raise ToolError(
                    f"The action {action!r} needs the admin's own page; run it there."
                )
        return self.with_messages(ctx, {"action": action, "count": count})

    # --- inlines ------------------------------------------------------------

    @cached_property
    def exposed_inlines(self):
        """``{name: (InlineModelAdmin class, foreign key)}``."""
        classes = self.model_admin.inlines if self.inlines is None else self.inlines
        found = {}
        for inline_class in classes:
            if not _exposed(inline_class, inline_class.model):
                continue
            try:
                fk = _get_foreign_key(
                    self.model, inline_class.model, fk_name=inline_class.fk_name
                )
            except ValueError:  # generic relations and the like
                self.unsupported.append(f"inline {inline_class.__qualname__}")
                continue
            name = self.inline_names.get(inline_class) or slugify(
                str(inline_class.model._meta.verbose_name_plural)
            ).replace("-", "_")
            found[name] = (inline_class, fk)
        return found

    def get_exposed_inline(self, name):
        """``(InlineModelAdmin class, foreign key)`` for ``name``."""
        try:
            return self.exposed_inlines[name]
        except KeyError:
            raise InvalidArguments(
                f"Unknown inline {name!r}. Available: {sorted(self.exposed_inlines)}."
            ) from None

    def static_inline(self, name):
        inline_class, _ = self.get_exposed_inline(name)
        return inline_class(self.model, self.site)

    def get_inline(self, request, parent, name):
        """``(FormSet, inline, prefix, fk)`` as the change form builds them."""
        inline_class, fk = self.get_exposed_inline(name)
        prefixes = {}
        for formset_class, inline in self.model_admin.get_formsets_with_inlines(
            request, parent
        ):
            prefix = formset_class.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1 or not prefix:
                prefix = f"{prefix}-{prefixes[prefix]}"
            if type(inline) is inline_class:
                return formset_class, inline, prefix, fk
        raise PermissionDenied(
            f"You cannot see {name} of this {self.opts.verbose_name}."
        )

    def inline_rows(self, request, inline, parent, fk):
        queryset = inline.get_queryset(request).filter(**{fk.name: parent})
        ordering = inline.get_ordering(request) or inline.model._meta.ordering
        return queryset.order_by(*(list(ordering or []) + ["pk"]))

    def inline_field_names(self, inline, fk, request=None, parent=None, editable=False):
        """Inline fields, minus hidden ones and the foreign key to the parent."""
        if request is None:
            fields, readonly = self.static_fields(inline)
        else:
            fields = flatten(inline.get_fields(request, parent))
            readonly = inline.get_readonly_fields(request, parent)
        skipped = {*self.get_hidden_fields(inline), fk.name}
        if editable:
            skipped.update(readonly)
        return [name for name in fields if name not in skipped]

    def inline_row_names(self, inline, request, parent, fk):
        names = [inline.model._meta.pk.name]
        for name in self.inline_field_names(inline, fk, request, parent):
            if name not in names:
                names.append(name)
        return names

    def inline_row(self, request, inline, parent, row, fk):
        names = self.inline_row_names(inline, request, parent, fk)
        return {name: self.inline_field_value(inline, row, name) for name in names}

    def serialize_inlines(self, ctx, request, parent):
        result = {}
        for name in self.exposed_inlines:
            try:
                _, inline, _, fk = self.get_inline(request, parent, name)
            except PermissionDenied:
                continue
            rows = self.inline_rows(request, inline, parent, fk)
            page = list(rows[: self.max_page_size])
            self.prefetch_labels(
                page, self.inline_row_names(inline, request, parent, fk)
            )
            result[name] = {
                "results": [
                    self.inline_row(request, inline, parent, row, fk) for row in page
                ],
                "count": rows.count(),
            }
        return result

    def run(self, ctx, action, /, **kwargs):
        if action in INLINE_ACTIONS:
            return self.run_inline(ctx, INLINE_ACTIONS[action], **kwargs)
        else:
            return super().run(ctx, action, **kwargs)

    def run_inline_list(self, ctx, inline, parent, limit=None, cursor=None):
        parent_obj = self.get_object(ctx, parent, "get")
        request = self.admin_request(ctx)
        _, inline_admin, _, fk = self.get_inline(request, parent_obj, inline)
        rows = self.inline_rows(request, inline_admin, parent_obj, fk)
        result = self.paginate_queryset(
            rows,
            limit,
            cursor,
            lambda page: [
                self.inline_row(request, inline_admin, parent_obj, row, fk)
                for row in page
            ],
        )
        return self.with_messages(ctx, result)

    def run_inline(self, ctx, operation, inline, parent, row=None, **data):
        """Create, change or delete one inline row through the whole formset."""
        parent_obj = self.get_object(ctx, parent, "update")
        request = self.admin_request(ctx, write=True)
        model_admin = self.model_admin
        formset_class, inline_admin, prefix, fk = self.get_inline(
            request, parent_obj, inline
        )
        allowed = getattr(inline_admin, INLINE_PERMISSIONS[operation])
        if not allowed(request, parent_obj):
            raise PermissionDenied(f"You cannot {operation} {inline} here.")
        if data:
            allowed = self.inline_field_names(
                inline_admin, fk, request, parent_obj, editable=True
            )
            refused = sorted(name for name in data if name not in allowed)
            if refused:
                raise ToolError(f"You cannot set {', '.join(refused)}.")

        queryset = inline_admin.get_queryset(request)
        unbound = formset_class(instance=parent_obj, prefix=prefix, queryset=queryset)
        initial = unbound.initial_forms
        post = MultiValueDict()

        def put(key, value):
            post.setlist(key, list(value) if isinstance(value, list) else [value])

        target = None
        for index, form in enumerate(initial):
            for name in form.fields:
                put(f"{prefix}-{index}-{name}", form[name].value())
            if row is not None and str(form.instance.pk) == str(row):
                target = index
        total = len(initial)
        if operation == "create":
            target, total = total, total + 1
            empty = unbound.empty_form
            for name in empty.fields:
                put(f"{prefix}-{target}-{name}", empty[name].value())
        elif target is None:
            raise Http404(f"No {inline} row {row!r} here.")
        data, files = self.resolve_files(ctx, data, inline_admin.model._meta)
        if operation == "delete":
            put(f"{prefix}-{target}-DELETE", True)
        for name, value in data.items():
            put(f"{prefix}-{target}-{name}", value)
        put(f"{prefix}-TOTAL_FORMS", total)
        put(f"{prefix}-INITIAL_FORMS", len(initial))
        put(f"{prefix}-MIN_NUM_FORMS", unbound.min_num)
        put(f"{prefix}-MAX_NUM_FORMS", unbound.max_num)
        sent_files = files
        files = {f"{prefix}-{target}-{name}": file for name, file in files.items()}
        request = self.admin_request(ctx, write=True, data=post, files=_uploads(files))

        kwargs = model_admin.get_formset_kwargs(
            request, parent_obj, inline_admin, prefix
        )
        kwargs["files"] = form_files(files)
        formset = formset_class(**kwargs)
        if not inline_admin.has_change_permission(request, parent_obj):
            # as the change form: view-only rows are not validated
            for index, form in enumerate(formset.initial_forms):
                if not (operation == "delete" and index == target):
                    form._errors = {}
                    form.cleaned_data = form.initial
        with closing_stored_files(sent_files):
            valid = formset.is_valid()
        if not valid:
            raise self.formset_error(formset, target)
        if operation != "delete":
            check_cleared(formset.forms[target], sent_files)
        parent_form = model_admin.get_form(request, parent_obj, change=True, fields=[])(
            instance=parent_obj
        )
        uploads = sent_files.values()
        with self.removing_uploads_on_error(uploads), transaction.atomic():
            model_admin.save_formset(request, parent_form, formset, change=True)
            message = model_admin.construct_change_message(
                request, parent_form, [formset]
            )
            model_admin.log_change(request, parent_obj, message)
        if operation == "delete":
            result = {"deleted": True, "row": row}
        else:
            if operation == "create":
                row = formset.new_objects[0]
            else:
                row = inline_admin.get_queryset(request).get(
                    pk=formset.forms[target].instance.pk
                )
            result = self.inline_row(request, inline_admin, parent_obj, row, fk)
        return self.with_messages(ctx, result)

    @staticmethod
    def formset_error(formset, target):
        errors = {}
        for index, form in enumerate(formset.forms):
            if form.errors:
                error = form_error(form)
                if index == target:
                    return ToolError(error.message, data=error.data)
                errors[f"row {index}"] = error.data["fields"]
        messages = list(formset.non_form_errors())
        summary = " ".join(messages) or "Another row is invalid."
        return ToolError(
            f"Invalid data. {summary}",
            data={"fields": errors, "errors": messages},
        )

    # --- generated items ----------------------------------------------------

    def inline_write_parameters(self, name, partial):
        inline_class, fk = self.get_exposed_inline(name)
        inline = self.static_inline(name)
        opts = inline_class.model._meta
        return [
            write_parameter(opts, field, partial, file_sources=self.file_sources)
            for field in self.inline_field_names(inline, fk, editable=True)
            if is_writable(opts, field)
            and (self.file_sources or not file_field(opts, field))
        ]

    def inline_parameters(self, name, operation, with_parent=True):
        inline_class, _ = self.get_exposed_inline(name)
        parent = ("parent", self.lookup_annotation, inspect.Parameter.empty)
        row_annotation = Annotated[
            Union[int, str], f"The {inline_class.model._meta.verbose_name}'s id"
        ]
        row = ("row", row_annotation, inspect.Parameter.empty)
        if operation == "list":
            limit = f"Results per page, 1-{self.max_page_size}"
            cursor = "next_cursor from the previous page"
            params = [
                ("limit", Annotated[int, limit], self.page_size),
                ("cursor", Annotated[Optional[str], cursor], None),
            ]
            return [parent, *params] if with_parent else params
        if operation == "create":
            params = self.inline_write_parameters(name, partial=False)
            return [parent, *params] if with_parent else params
        if operation == "delete":
            return [parent, row] if with_parent else []
        params = self.inline_write_parameters(name, partial=True)
        return [parent, row, *params] if with_parent else params

    def inline_argument_function(self, inline, operation):
        """The fields of an inline write, for generic mode's ``data``."""
        return signature_function(
            f"{type(self).__qualname__}.{inline}_{operation}",
            type(self).__module__,
            self.inline_parameters(inline, operation, with_parent=False),
        )

    def get_inline_tool_specs(self):
        specs = []
        base = self.get_name()
        operations = ["list"]
        if "update" in self.actions:
            operations += INLINE_OPERATIONS
        for name in self.exposed_inlines:
            inline_opts = self.exposed_inlines[name][0].model._meta
            plural = inline_opts.verbose_name_plural
            singular = inline_opts.verbose_name
            parent = self.opts.verbose_name
            for operation in operations:

                def impl(provider, ctx, _name=name, _operation=operation, **kwargs):
                    return provider.run(
                        ctx, f"inline_{_operation}", inline=_name, **kwargs
                    )

                func = make_function(
                    f"{type(self).__qualname__}.{name}_{operation}",
                    type(self).__module__,
                    self.inline_parameters(name, operation),
                    impl,
                )
                description = {
                    "list": f"List {plural} of a {parent}. Pass next_cursor as "
                    "cursor for the next page.",
                    "create": f"Add a {singular} to a {parent}.",
                    "update": f"Change a {singular} of a {parent}. Only the fields "
                    "given change.",
                    "delete": f"Remove a {singular} from a {parent}.",
                }[operation]
                key = f"inline_{operation}"
                tool_key = f"{name}_{operation}"
                specs.append(
                    ToolSpec(
                        func,
                        name=self.tool_names.get(tool_key) or f"{base}_{tool_key}",
                        description=self.descriptions.get(tool_key, description),
                        permission=self.inline_permission(name, key),
                        **HINTS[operation],
                    )
                )
        return specs

    def inline_permission(self, name, key):
        def check(ctx):
            if not self.has_action_permission(ctx, key):
                return False
            if key == "inline_list":
                return True
            inline = self.static_inline(name)
            request = self.admin_request(ctx)
            operation = key.removeprefix("inline_")
            return getattr(inline, INLINE_PERMISSIONS[operation])(request, None)

        return check

    def get_admin_action_specs(self):
        if not self.admin_actions:
            return []
        names = tuple(self.admin_actions)
        lines = [f"{name}: {self.action_description(name)}" for name in names]
        func = make_function(
            f"{type(self).__qualname__}.action",
            type(self).__module__,
            [
                ("action", Literal[names], inspect.Parameter.empty),
                (
                    "ids",
                    Annotated[list[self.lookup_annotation], self.lookup_argument],
                    inspect.Parameter.empty,
                ),
            ],
            lambda provider, ctx, action, ids: provider.run_admin_action(
                ctx, action, ids
            ),
        )
        description = (
            f"Run an admin action on {self.opts.verbose_name_plural}. "
            + " ".join(f"{line}." for line in lines)
        )
        return [
            ToolSpec(
                func,
                name=self.tool_names.get("action") or f"{self.get_name()}_action",
                description=self.descriptions.get("action", description),
                permission=self.action_permission("admin_action"),
            )
        ]

    def get_items(self):
        items = super().get_items()
        if self.get_exposure() == "tools":
            specs = self.get_admin_action_specs() + self.get_inline_tool_specs()
            items += [Item(spec, self) for spec in specs]
        return items

    def describe(self, ctx):
        entry = super().describe(ctx)
        if self.fields_from_admin:
            entry["fields"] = self.list_display(ctx)[0]
        if self.has_action_permission(ctx, "admin_action"):
            entry["admin_actions"] = {
                name: self.action_description(name)
                for name in self.available_admin_actions(ctx)
            }
        if self.has_action_permission(ctx, "inline_list"):
            writes = self.has_action_permission(ctx, "inline_update")
            inlines = {}
            for name, (_, fk) in self.exposed_inlines.items():
                inline_admin = self.static_inline(name)
                inline = {"fields": self.inline_field_names(inline_admin, fk)}
                if writes:
                    inline["writable"] = {
                        param[0]: schema.annotation_schema(param[1])
                        for param in self.inline_parameters(
                            name, "update", with_parent=False
                        )
                    }
                inlines[name] = inline
            entry["inlines"] = inlines
        return entry


def _uploads(files):
    """``request.FILES`` of a write: the uploads, without the cleared fields."""
    return {name: file for name, file in files.items() if file is not False}


def _touches(path, names):
    """Whether a search or filter path goes through one of ``names``."""
    return any(part in names for part in path.lstrip("^=@").split("__"))


def _display_callables(list_display):
    return {entry.__name__: entry for entry in list_display if callable(entry)}


def _exposed(model_admin, model):
    """``mcp_exposed = False`` on an admin, an inline or a model keeps it out."""
    return getattr(model_admin, "mcp_exposed", True) and getattr(
        model, "mcp_exposed", True
    )


def _excluded(model, exclude):
    opts = model._meta
    for entry in exclude:
        if entry is model:
            return True
        if isinstance(entry, str) and entry.lower() in (
            opts.app_label,
            opts.label_lower,
        ):
            return True
    return False


def build_admin_providers(server, configs):
    """The providers ``MCPServer.register_admin`` stands for."""
    seen = {
        provider.model
        for provider in server._registry.values()
        if provider.model is not None
    }
    providers = []
    for config in configs:
        site = config["site"]
        base = config["provider_class"] or AdminProvider
        for model, model_admin in list(site._registry.items()):
            if (
                model in seen
                or not _exposed(model_admin, model)
                or _excluded(model, config["exclude"])
            ):
                continue
            attrs = {
                "actions": list(config["model_actions"].get(model, config["actions"])),
                "admin_site": site,
                "__module__": type(model_admin).__module__,
                "__qualname__": f"{model.__name__}AdminProvider",
            }
            for option in ("exposure", "name_template"):
                if config[option] is not None:
                    attrs[option] = config[option]
            provider_class = type(f"{model.__name__}AdminProvider", (base,), attrs)
            provider = provider_class(server, model=model)
            server.trust_writes(provider)
            providers.append(provider)
            seen.add(model)
    return providers

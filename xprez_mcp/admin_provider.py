"""``AdminProvider``: a ``ModelProvider`` driven by a ``ModelAdmin``.

Every permission, queryset, form and save goes through the ``ModelAdmin``, so a
user can do through MCP exactly what they can do in that admin.
"""

import copy
import datetime
import decimal
import inspect
import uuid
from functools import cached_property
from typing import Annotated, Literal

import django
from django.contrib import admin
from django.contrib.admin.utils import flatten_fieldsets, lookup_field
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.base import BaseStorage
from django.core.exceptions import (
    FieldDoesNotExist,
    ImproperlyConfigured,
    PermissionDenied,
)
from django.db import models, transaction
from django.forms.models import _get_foreign_key
from django.http import Http404, HttpRequest, QueryDict
from django.utils.datastructures import MultiValueDict
from django.utils.text import slugify

from . import schema
from .errors import InvalidArguments, ToolError
from .items import Item, ToolSpec
from .model_provider import (
    WRITE_ACTIONS,
    ModelProvider,
    _decode_cursor,
    _encode_cursor,
    form_error,
    is_writable,
    make_function,
    write_parameter,
)

INLINE_OPERATIONS = ("create", "update", "delete")
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


class CollectedMessages(BaseStorage):
    """Message storage keeping what admin code adds, for the tool result."""

    def _get(self, *args, **kwargs):
        return [], True

    def _store(self, messages, response, *args, **kwargs):
        return []

    def collected(self):
        return [
            {"level": message.level_tag, "message": str(message.message)}
            for message in self._queued_messages
        ]


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

    def __init__(self, server, model=None):
        model = model or type(self).model
        if model is None:
            raise ImproperlyConfigured(
                f"{type(self).__qualname__} needs a model: register it with "
                "mcp.register(Model, Provider) or set Provider.model."
            )
        self.model = model
        self.model_admin = self.get_model_admin()
        self.unsupported = []
        self.configure()
        super().__init__(server, model=model)
        self.inline_specs  # resolves inlines now, filling unsupported

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
        label = type(self).__qualname__
        if self.permissions:
            raise ImproperlyConfigured(
                f"{label}: AdminProvider uses the admin's permissions; "
                "permissions is not supported."
            )
        if "delete_selected" in self.admin_actions:
            raise ImproperlyConfigured(f"{label}: delete_selected is never exposed.")
        model_admin = self.model_admin
        if self.fields is None and self.serializer is None:
            self.fields = self.admin_list_fields()
        if not self.search_fields:
            self.search_fields = tuple(model_admin.search_fields)
        if not self.filters:
            self.filters = self.admin_filters()
        if self.write_fields is None and self.serializer is None:
            self.write_fields = self.admin_write_fields()

    @property
    def lookup_name(self):
        """The lookup's key in results."""
        if self.lookup_field == "pk":
            return self.model._meta.pk.name
        return self.lookup_field

    @cached_property
    def display_callables(self):
        return {
            entry.__name__: entry
            for entry in self.model_admin.list_display
            if callable(entry)
        }

    def admin_list_fields(self):
        names = [self.lookup_name]
        for entry in self.model_admin.list_display:
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
            fields = list(model_admin.get_fields(request, obj))
            readonly = list(model_admin.get_readonly_fields(request, obj))
        except Exception:  # an override that needs a real user
            if model_admin.fieldsets:
                fields = flatten_fieldsets(model_admin.fieldsets)
            else:
                fields = list(model_admin.fields or [])
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

    def admin_filters(self):
        filters = {}
        for entry in self.model_admin.list_filter:
            if isinstance(entry, (list, tuple)):
                entry = entry[0]  # (field, FieldListFilter): filter by the field
            if isinstance(entry, str):
                try:
                    field, rest, _ = self._resolve_path(entry)
                except ImproperlyConfigured:
                    field, rest = None, "?"
                if field is None or rest:
                    self.unsupported.append(f"list_filter {entry!r}")
                    continue
                if isinstance(field, models.DateField) and not field.is_relation:
                    filters[f"{entry}__gte"] = f"{entry}__gte"
                    filters[f"{entry}__lte"] = f"{entry}__lte"
                else:
                    filters[entry] = entry
            elif isinstance(entry, type) and issubclass(entry, admin.SimpleListFilter):
                filters[entry.parameter_name] = self.simple_filter(entry)
            else:
                self.unsupported.append(f"list_filter {entry!r}")
        return filters

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

    def admin_request(self, ctx, write=False):
        """A copy of the MCP request for admin code, one per call and site."""
        requests = ctx.__dict__.setdefault("_admin_requests", {})
        request = requests.get(self.site.name)
        if request is None:
            request = self.get_admin_request(ctx)
            requests[self.site.name] = request
        request.method = "POST" if write else "GET"
        return request

    def get_admin_request(self, ctx):
        if ctx.request is not None:
            request = copy.copy(ctx.request)
        else:
            request = HttpRequest()
        request.GET = QueryDict()
        request.POST = QueryDict()
        try:
            request.FILES = MultiValueDict()
        except AttributeError:  # WSGIRequest.FILES has no setter
            request._files = MultiValueDict()
        request.user = ctx.user if ctx.user is not None else AnonymousUser()
        request.current_app = self.site.name
        request._messages = CollectedMessages(request)
        return request

    def with_messages(self, ctx, result):
        request = ctx.__dict__.get("_admin_requests", {}).get(self.site.name)
        collected = request._messages.collected() if request is not None else []
        if collected and isinstance(result, dict):
            result = {**result, "messages": collected}
        return result

    # --- permissions --------------------------------------------------------

    def has_permission(self, ctx):
        request = self.admin_request(ctx)
        return self.site.has_permission(
            request
        ) and self.model_admin.has_module_permission(request)

    def admin_permission(self, request, action, obj=None):
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
        if action == "action":
            return self.writes_allowed() and bool(self.available_admin_actions(ctx))
        if action == "children_list":
            return bool(self.inline_specs) and self.has_action_permission(ctx, "get")
        if action.startswith("child_"):
            return bool(self.inline_specs) and self.has_action_permission(ctx, "update")
        if action not in self.actions:
            return False
        if action in WRITE_ACTIONS and not self.writes_allowed():
            return False
        return self.admin_permission(self.admin_request(ctx), action)

    def has_object_permission(self, ctx, action, instance):
        return self.admin_permission(self.admin_request(ctx), action, instance)

    def generic_actions(self):
        found = set(self.actions)
        if self.admin_actions:
            found.add("action")
        if self.inline_specs and "get" in self.actions:
            found.add("children_list")
        if self.inline_specs and "update" in self.actions:
            found.update(f"child_{operation}" for operation in INLINE_OPERATIONS)
        return found

    # --- reads --------------------------------------------------------------

    def get_queryset(self, ctx):
        return self.model_admin.get_queryset(self.admin_request(ctx))

    def has_search(self):
        return bool(self.search_fields) or type(self).search is not AdminProvider.search

    def search(self, ctx, queryset, query):
        request = self.admin_request(ctx)
        queryset, duplicates = self.model_admin.get_search_results(
            request, queryset, query
        )
        return queryset.distinct() if duplicates else queryset

    def get_list_ordering(self, ctx):
        ordering = list(self.model_admin.get_ordering(self.admin_request(ctx)) or [])
        if not ordering:
            return self.get_ordering()
        if not {"pk", "-pk", self.opts.pk.name, f"-{self.opts.pk.name}"} & set(
            ordering
        ):
            ordering.append("pk")  # stable pages, as the changelist does
        return ordering

    def field_value(self, instance, name, model_admin=None):
        model_admin = model_admin or self.model_admin
        if name == "display":
            return str(instance)
        if model_admin is self.model_admin and name in self.display_callables:
            return plain(self.display_callables[name](instance))
        try:
            instance._meta.get_field(name)
        except FieldDoesNotExist:
            if name == "pk":
                return instance.pk
            _, _, value = lookup_field(name, instance, model_admin)
            return plain(value)
        return super().field_value(instance, name)

    def detail_names(self, model_admin, request, obj, exclude=()):
        if model_admin is self.model_admin:
            names = [self.lookup_name]
        else:  # an inline: obj is the parent, the rows are the inline's model
            names = [model_admin.model._meta.pk.name]
        for name in model_admin.get_fields(request, obj):
            if name not in names and name not in exclude:
                names.append(name)
        return names

    def serialize(self, ctx, value, many=False, detail=False):
        configured = self.detail_serializer or self.detail_fields
        if many or not detail or self.serializer is not None or configured:
            return super().serialize(ctx, value, many=many, detail=detail)
        request = self.admin_request(ctx)
        names = self.detail_names(self.model_admin, request, value)
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

    def admin_form(self, request, data, instance):
        """A bound admin form for just the fields sent, validated."""
        model_admin = self.model_admin
        allowed = set(flatten_fieldsets(model_admin.get_fieldsets(request, instance)))
        allowed -= set(model_admin.get_readonly_fields(request, instance))
        refused = sorted(name for name in data if name not in allowed)
        if refused:
            raise ToolError(f"You cannot set {', '.join(refused)}.")
        form_class = model_admin.get_form(
            request, instance, change=instance is not None, fields=list(data)
        )
        form = form_class(data=data, files={}, instance=instance)
        if not form.is_valid():
            raise form_error(form)
        return form

    def run_create(self, ctx, **data):
        request = self.admin_request(ctx, write=True)
        model_admin = self.model_admin
        form = self.admin_form(request, data, None)
        with transaction.atomic():
            obj = model_admin.save_form(request, form, change=False)
            model_admin.save_model(request, obj, form, change=False)
            model_admin.save_related(request, form, [], change=False)
            message = model_admin.construct_change_message(request, form, [], add=True)
            model_admin.log_addition(request, obj, message)
        return self.with_messages(ctx, self.serialize(ctx, obj, detail=True))

    def run_update(self, ctx, **kwargs):
        value = kwargs.pop(self.lookup_argument)
        instance = self.get_object(ctx, value, "update")
        request = self.admin_request(ctx, write=True)
        model_admin = self.model_admin
        form = self.admin_form(request, kwargs, instance)
        with transaction.atomic():
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
    def inline_specs(self):
        """``{name: (InlineModelAdmin class, foreign key)}``."""
        classes = self.model_admin.inlines if self.inlines is None else self.inlines
        found = {}
        for inline_class in classes:
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

    def static_inline(self, name):
        inline_class, _ = self.inline_specs[name]
        return inline_class(self.model, self.site)

    def get_inline(self, request, parent, name):
        """``(FormSet, inline, prefix, fk)`` as the change form builds them."""
        if name not in self.inline_specs:
            raise InvalidArguments(
                f"Unknown inline {name!r}. Available: {sorted(self.inline_specs)}."
            )
        inline_class, fk = self.inline_specs[name]
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

    def inline_row(self, request, inline, parent, row, fk):
        names = self.detail_names(inline, request, parent, exclude=(fk.name,))
        return {name: self.field_value(row, name, inline) for name in names}

    def serialize_inlines(self, ctx, request, parent):
        result = {}
        for name in self.inline_specs:
            try:
                _, inline, _, fk = self.get_inline(request, parent, name)
            except PermissionDenied:
                continue
            rows = self.inline_rows(request, inline, parent, fk)
            page = list(rows[: self.max_page_size])
            result[name] = {
                "results": [
                    self.inline_row(request, inline, parent, row, fk) for row in page
                ],
                "count": rows.count(),
            }
        return result

    def run_children_list(self, ctx, inline, parent, limit=None, cursor=None):
        parent_obj = self.get_object(ctx, parent, "get")
        request = self.admin_request(ctx)
        _, inline_admin, _, fk = self.get_inline(request, parent_obj, inline)
        limit = self.page_size if limit is None else limit
        if not 1 <= limit <= self.max_page_size:
            raise InvalidArguments(f"limit must be between 1 and {self.max_page_size}.")
        offset = _decode_cursor(cursor) if cursor else 0
        rows = self.inline_rows(request, inline_admin, parent_obj, fk)
        count = rows.count()
        page = list(rows[offset : offset + limit])
        more = offset + limit < count
        return self.with_messages(
            ctx,
            {
                "results": [
                    self.inline_row(request, inline_admin, parent_obj, row, fk)
                    for row in page
                ],
                "count": count,
                "next_cursor": _encode_cursor(offset + limit) if more else None,
            },
        )

    def run_child(self, ctx, operation, inline, parent, child=None, **data):
        """Create, change or delete one inline row through the whole formset."""
        parent_obj = self.get_object(ctx, parent, "update")
        request = self.admin_request(ctx, write=True)
        model_admin = self.model_admin
        formset_class, inline_admin, prefix, fk = self.get_inline(
            request, parent_obj, inline
        )
        permission = {
            "create": inline_admin.has_add_permission,
            "update": inline_admin.has_change_permission,
            "delete": inline_admin.has_delete_permission,
        }[operation]
        if not permission(request, parent_obj):
            raise PermissionDenied(f"You cannot {operation} {inline} here.")
        if data:
            fields = set(inline_admin.get_fields(request, parent_obj))
            fields -= set(inline_admin.get_readonly_fields(request, parent_obj))
            fields.discard(fk.name)
            refused = sorted(name for name in data if name not in fields)
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
            if child is not None and str(form.instance.pk) == str(child):
                target = index
        total = len(initial)
        if operation == "create":
            target, total = total, total + 1
            empty = unbound.empty_form
            for name in empty.fields:
                put(f"{prefix}-{target}-{name}", empty[name].value())
        elif target is None:
            raise Http404(f"No {inline} row {child!r} here.")
        if operation == "delete":
            put(f"{prefix}-{target}-DELETE", True)
        for name, value in data.items():
            put(f"{prefix}-{target}-{name}", value)
        put(f"{prefix}-TOTAL_FORMS", total)
        put(f"{prefix}-INITIAL_FORMS", len(initial))
        put(f"{prefix}-MIN_NUM_FORMS", unbound.min_num)
        put(f"{prefix}-MAX_NUM_FORMS", unbound.max_num)
        request.POST = post

        formset = formset_class(
            **model_admin.get_formset_kwargs(request, parent_obj, inline_admin, prefix)
        )
        if not inline_admin.has_change_permission(request, parent_obj):
            # as the change form: view-only rows are not validated
            for index, form in enumerate(formset.initial_forms):
                if not (operation == "delete" and index == target):
                    form._errors = {}
                    form.cleaned_data = form.initial
        if not formset.is_valid():
            raise self.formset_error(formset, target)
        parent_form = model_admin.get_form(request, parent_obj, change=True, fields=[])(
            instance=parent_obj
        )
        with transaction.atomic():
            model_admin.save_formset(request, parent_form, formset, change=True)
            message = model_admin.construct_change_message(
                request, parent_form, [formset]
            )
            model_admin.log_change(request, parent_obj, message)
        if operation == "delete":
            result = {"deleted": True, "id": child}
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
        inline_class, fk = self.inline_specs[name]
        inline = self.static_inline(name)
        opts = inline_class.model._meta
        fields, readonly = self.static_fields(inline)
        return [
            write_parameter(opts, field, partial)
            for field in fields
            if field not in readonly and field != fk.name and is_writable(opts, field)
        ]

    def inline_parameters(self, name, operation, with_lookups=True):
        inline_class, _ = self.inline_specs[name]
        parent = ("parent", self.lookup_annotation, inspect.Parameter.empty)
        child_annotation = Annotated[
            int | str, f"The {inline_class.model._meta.verbose_name}'s id"
        ]
        child = ("id", child_annotation, inspect.Parameter.empty)
        if operation == "list":
            limit = f"Results per page, 1-{self.max_page_size}"
            cursor = "next_cursor from the previous page"
            params = [
                ("limit", Annotated[int, limit], self.page_size),
                ("cursor", Annotated[str | None, cursor], None),
            ]
            return [parent, *params] if with_lookups else params
        if operation == "create":
            params = self.inline_write_parameters(name, partial=False)
            return [parent, *params] if with_lookups else params
        if operation == "delete":
            return [parent, child] if with_lookups else []
        params = self.inline_write_parameters(name, partial=True)
        return [parent, child, *params] if with_lookups else params

    def inline_argument_function(self, name, operation):
        """The fields of an inline write, for generic mode's ``data``."""
        return make_function(
            f"{type(self).__qualname__}.{name}_{operation}",
            type(self).__module__,
            self.inline_parameters(name, operation, with_lookups=False),
            lambda provider, ctx, **kwargs: None,
        )

    def get_inline_specs(self):
        specs = []
        base = self.get_name()
        operations = ["list"]
        if "update" in self.actions:
            operations += INLINE_OPERATIONS
        for name in self.inline_specs:
            plural = self.inline_specs[name][0].model._meta.verbose_name_plural
            singular = self.inline_specs[name][0].model._meta.verbose_name
            parent = self.opts.verbose_name
            for operation in operations:

                def impl(provider, ctx, _name=name, _operation=operation, **kwargs):
                    parent_value = kwargs.pop("parent")
                    if _operation == "list":
                        return provider.run_children_list(
                            ctx, _name, parent_value, **kwargs
                        )
                    child = kwargs.pop("id", None)
                    return provider.run_child(
                        ctx, _operation, _name, parent_value, child, **kwargs
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
                key = "children_list" if operation == "list" else f"child_{operation}"
                hints = {
                    "list": {"read_only": True},
                    "create": {},
                    "update": {"idempotent": True},
                    "delete": {"destructive": True},
                }[operation]
                tool_key = f"{name}_{operation}"
                specs.append(
                    ToolSpec(
                        func,
                        name=self.tool_names.get(tool_key) or f"{base}_{tool_key}",
                        description=self.descriptions.get(tool_key, description),
                        permission=self.inline_permission(name, key),
                        **hints,
                    )
                )
        return specs

    def inline_permission(self, name, key):
        def check(ctx):
            if not self.has_action_permission(ctx, key):
                return False
            if key == "children_list":
                return True
            inline = self.static_inline(name)
            request = self.admin_request(ctx)
            operation = key[len("child_") :]
            return {
                "create": inline.has_add_permission,
                "update": inline.has_change_permission,
                "delete": inline.has_delete_permission,
            }[operation](request, None)

        return check

    def get_admin_action_spec(self):
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
                permission=self.action_permission("action"),
            )
        ]

    def get_items(self):
        items = super().get_items()
        if self.get_exposure() == "tools":
            specs = self.get_admin_action_spec() + self.get_inline_specs()
            items += [Item(spec, self) for spec in specs]
        return items

    def describe(self, ctx):
        entry = super().describe(ctx)
        if self.has_action_permission(ctx, "action"):
            entry["admin_actions"] = {
                name: self.action_description(name)
                for name in self.available_admin_actions(ctx)
            }
        if self.has_action_permission(ctx, "children_list"):
            writes = self.has_action_permission(ctx, "child_update")
            inlines = {}
            for name in self.inline_specs:
                fields, _ = self.static_fields(self.static_inline(name))
                fk = self.inline_specs[name][1]
                inline = {"fields": [field for field in fields if field != fk.name]}
                if writes:
                    inline["writable"] = {
                        param[0]: schema.annotation_schema(param[1])
                        for param in self.inline_parameters(name, "update", False)
                    }
                inlines[name] = inline
            entry["inlines"] = inlines
        return entry


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
            if model in seen or _excluded(model, config["exclude"]):
                continue
            attrs = {
                "actions": list(config["model_actions"].get(model, config["actions"])),
                "admin_site": site,
                "__module__": type(model_admin).__module__,
                "__qualname__": f"{model.__name__}AdminProvider",
            }
            if config["exposure"] is not None:
                attrs["exposure"] = config["exposure"]
            provider_class = type(f"{model.__name__}AdminProvider", (base,), attrs)
            provider = provider_class(server, model=model)
            server.mark_own(provider)
            providers.append(provider)
            seen.add(model)
    return providers

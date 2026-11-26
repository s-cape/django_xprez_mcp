"""Argument markers for ``Annotated``: lookups and number/length constraints.

``Lookup`` turns the value the client sends into an object before the tool
runs; ``Range`` and ``Length`` (and the matching markers of the
``annotated_types`` package) constrain a value and show the limits in the
schema.
"""

import decimal
import types
from typing import Annotated, Union, get_args, get_origin

from django.core.exceptions import ImproperlyConfigured

from ..errors import InvalidArguments

_CONTAINERS = (list, set, frozenset, tuple)
_NUMBERS = (int, float, decimal.Decimal)
_SEQUENCES = (str, *_CONTAINERS)
# ``X | Y`` unions; Python 3.9 has none.
_UNION_TYPE = getattr(types, "UnionType", Union)


class Lookup:
    """``Annotated[Model, Lookup(resolver, **options)]``: the client sends a
    reference (a string, or ``type``), the tool gets ``resolver(ctx, value,
    **options)``. A string names a method of the tool's provider.

    The resolver returns the object or raises ``ToolError``,
    ``PermissionDenied`` or ``ObjectDoesNotExist``.
    """

    def __init__(self, resolver, *, type=str, **options):
        if not (callable(resolver) or isinstance(resolver, str)):
            raise TypeError("Lookup needs a callable or a provider method name.")
        self.resolver = resolver
        self.type = type
        self.options = options

    def __repr__(self):
        return f"Lookup({self.resolver!r})"

    def get_resolver(self, provider):
        if not isinstance(self.resolver, str):
            return self.resolver
        return getattr(provider, self.resolver)

    def resolve(self, provider, ctx, value):
        return self.get_resolver(provider)(ctx, value, **self.options)


class Range:
    """``Annotated[int, Range(1, 52)]``: a number from ``min`` to ``max``
    (inclusive; either may be None)."""

    def __init__(self, min=None, max=None):
        self.ge = min
        self.le = max

    def __repr__(self):
        return f"Range({self.ge!r}, {self.le!r})"


class Length:
    """``Annotated[str, Length(1, 200)]``: a string, or a list of items, from
    ``min`` to ``max`` long (inclusive; either may be None)."""

    def __init__(self, min=None, max=None):
        self.min_length = min
        self.max_length = max

    def __repr__(self):
        return f"Length({self.min_length!r}, {self.max_length!r})"


class OutOfRange(InvalidArguments):
    """The value has the right type but breaks a constraint."""


# --- lookups -----------------------------------------------------------------


def wire_annotation(annotation):
    """The annotation as the client sees it: each lookup replaced by its input
    type, other ``Annotated`` metadata (a description, constraints) kept."""
    if get_origin(annotation) is Annotated:
        base, *metadata = get_args(annotation)
        lookups = [item for item in metadata if isinstance(item, Lookup)]
        if len(lookups) > 1:
            raise ImproperlyConfigured(f"{annotation!r} has more than one Lookup.")
        if lookups:
            base = lookups[0].type
            metadata = [item for item in metadata if not isinstance(item, Lookup)]
        else:
            base = wire_annotation(base)
        return Annotated[(base, *metadata)] if metadata else base
    args = get_args(annotation)
    if not args or not _has_lookup(annotation):
        return annotation
    wired = tuple(wire_annotation(arg) for arg in args)
    if _is_union(annotation):
        return Union[wired]
    return get_origin(annotation)[wired]


def find_lookups(annotation):
    """The ``Lookup`` markers in an annotation. A lookup may be the whole
    annotation, optional (``X | None``), or the items of a list or the
    values of a dict."""
    if get_origin(annotation) is Annotated:
        base, *metadata = get_args(annotation)
        found = [item for item in metadata if isinstance(item, Lookup)]
        return found or find_lookups(base)
    origin, args = get_origin(annotation), get_args(annotation)
    if _is_union(annotation):
        arms = [arg for arg in args if arg is not type(None)]
        found = [lookup for arm in arms for lookup in find_lookups(arm)]
        if found and len(arms) > 1:
            raise ImproperlyConfigured(
                f"{annotation!r}: a Lookup can only be optional (X | None), "
                "not part of a union of other types."
            )
        return found
    if origin in _CONTAINERS:
        return find_lookups(args[0]) if args else []
    if origin is dict and len(args) == 2:
        return find_lookups(args[1])
    return []


def resolve_lookups(annotation, value, provider, ctx):
    """``value`` (already converted) with every lookup in ``annotation``
    replaced by what its resolver returns."""
    if value is None:
        return None
    if get_origin(annotation) is Annotated:
        base, *metadata = get_args(annotation)
        for item in metadata:
            if isinstance(item, Lookup):
                return item.resolve(provider, ctx, value)
        return resolve_lookups(base, value, provider, ctx)
    origin, args = get_origin(annotation), get_args(annotation)
    if _is_union(annotation):
        for arm in args:
            if find_lookups(arm):
                return resolve_lookups(arm, value, provider, ctx)
        return value
    if origin in _CONTAINERS and args:
        items = [resolve_lookups(args[0], item, provider, ctx) for item in value]
        return items if isinstance(value, list) else type(value)(items)
    if origin is dict and len(args) == 2:
        return {
            key: resolve_lookups(args[1], item, provider, ctx)
            for key, item in value.items()
        }
    return value


# --- constraints -------------------------------------------------------------

_NUMBER_LIMITS = ("ge", "gt", "le", "lt", "multiple_of")
_LENGTH_LIMITS = ("min_length", "max_length")
_KEYWORDS = {
    "ge": "minimum",
    "gt": "exclusiveMinimum",
    "le": "maximum",
    "lt": "exclusiveMaximum",
    "multiple_of": "multipleOf",
}


def limits(annotation, metadata):
    """``{"ge": 1, "le": 52, ...}`` for a value of ``annotation`` from the
    ``Range`` / ``Length`` and ``annotated_types`` markers in ``metadata``;
    later markers win.

    A misplaced ``Range`` or ``Length`` raises ``TypeError``. A misplaced
    ``annotated_types`` marker is ignored, as it may be meant for another
    library, and so are the kinds this does not know (``Predicate``...).
    """
    kind = _kind(annotation)
    found = {}
    for item in metadata:
        ours = isinstance(item, (Range, Length))
        if not (ours or _is_annotated_types(item)):
            continue
        for marker in _flatten(item):
            for key in (*_NUMBER_LIMITS, *_LENGTH_LIMITS):
                value = getattr(marker, key, None)
                if value is None:
                    continue
                if key in _NUMBER_LIMITS and kind not in ("number", "decimal"):
                    if ours:
                        raise TypeError(
                            "Number limits apply to int, float or Decimal, not "
                            f"{annotation!r}."
                        )
                    continue
                if key in _LENGTH_LIMITS and kind not in ("string", "array"):
                    if ours:
                        raise TypeError(
                            f"Length limits apply to str or a list, not {annotation!r}."
                        )
                    continue
                found[key] = value
    return found


def limits_schema(annotation, found):
    """``(keywords, note)``: the JSON Schema keywords of ``found`` for a value
    of ``annotation``, and words for the description where the schema has
    none (a ``Decimal`` is sent as a string)."""
    kind = _kind(annotation)
    if kind == "decimal":
        return {}, f"Must be {_number_words(found)}." if found else None
    keywords = {}
    for key, value in found.items():
        if key == "min_length":
            keywords["minItems" if kind == "array" else "minLength"] = value
        elif key == "max_length":
            keywords["maxItems" if kind == "array" else "maxLength"] = value
        else:
            keywords[_KEYWORDS[key]] = value
    return keywords, None


def check_limits(found, value, path):
    if value is None or not found:
        return
    if isinstance(value, _NUMBERS) and not isinstance(value, bool):
        if not set(_NUMBER_LIMITS) & set(found):
            return
        ge, gt, le, lt, step = (found.get(key) for key in _NUMBER_LIMITS)
        if (
            _is_nan(value)  # compares false with every bound
            or (ge is not None and value < ge)
            or (gt is not None and value <= gt)
            or (le is not None and value > le)
            or (lt is not None and value >= lt)
            or (step is not None and value % step != 0)
        ):
            raise OutOfRange(f"{path} must be {_number_words(found)}.")
    elif isinstance(value, _SEQUENCES):
        low, high = found.get("min_length"), found.get("max_length")
        size = len(value)
        if (low is not None and size < low) or (high is not None and size > high):
            if isinstance(value, str):
                words = _length_words(low, high, "character")
                raise OutOfRange(f"{path} must be {words} long.")
            raise OutOfRange(f"{path} must have {_length_words(low, high, 'item')}.")


# --- helpers -----------------------------------------------------------------


def _has_lookup(annotation):
    try:
        return bool(find_lookups(annotation))
    except ImproperlyConfigured:
        return True


def _is_union(annotation):
    origin = get_origin(annotation)
    return origin is Union or origin is _UNION_TYPE


def _is_annotated_types(item):
    # duck-typed by module, so the package is never imported here
    return type(item).__module__.split(".")[0] == "annotated_types"


def _flatten(item):
    # annotated_types.Interval is a group: iterating yields Gt / Ge / Lt / Le
    if _is_annotated_types(item) and hasattr(item, "__iter__"):
        return list(item)
    return [item]


def _kind(annotation):
    if annotation is decimal.Decimal:
        return "decimal"
    if annotation in _NUMBERS:
        return "number"
    if annotation is str:
        return "string"
    if annotation in _CONTAINERS or get_origin(annotation) in _CONTAINERS:
        return "array"
    return None


def _is_nan(value):
    if isinstance(value, decimal.Decimal):
        return value.is_nan()
    return value != value


def _number_words(found):
    ge, gt, le, lt, step = (found.get(key) for key in _NUMBER_LIMITS)
    words = []
    if ge is not None and le is not None:
        words.append(f"between {ge} and {le}")
        ge = le = None
    if ge is not None:
        words.append(f"at least {ge}")
    if gt is not None:
        words.append(f"greater than {gt}")
    if le is not None:
        words.append(f"at most {le}")
    if lt is not None:
        words.append(f"less than {lt}")
    if step is not None:
        words.append(f"a multiple of {step}")
    return " and ".join(words)


def _length_words(low, high, unit):
    def units(count):
        return f"{count} {unit}" if count == 1 else f"{count} {unit}s"

    if low is not None and low == high:
        return f"exactly {units(low)}"
    if low is not None and high is not None:
        return f"between {low} and {units(high)}"
    if low is not None:
        return f"at least {units(low)}"
    return f"at most {units(high)}"

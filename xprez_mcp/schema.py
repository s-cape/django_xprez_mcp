"""JSON Schema from type hints, and argument conversion against the same hints."""

import datetime
import decimal
import enum
import inspect
import types
import typing
import uuid
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from django.core.exceptions import ImproperlyConfigured

from .errors import InvalidArguments

EMPTY = inspect.Parameter.empty
_VARIADIC = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
# ``X | Y`` unions; Python 3.9 has none.
_UNION_TYPE = getattr(types, "UnionType", Union)

_SIMPLE = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    decimal.Decimal: {"type": "string", "format": "decimal"},
    datetime.datetime: {"type": "string", "format": "date-time"},
    datetime.date: {"type": "string", "format": "date"},
    uuid.UUID: {"type": "string", "format": "uuid"},
}


# --- parameters --------------------------------------------------------------


def wire_parameters(func):
    """Parameters that come from the client: all but ``self`` and the context."""
    params = list(inspect.signature(func).parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    if not params or params[0].kind in _VARIADIC:
        raise ImproperlyConfigured(
            f"{func.__qualname__} must take the call context as its first "
            "parameter (after self)."
        )
    wire = [param for param in params[1:] if param.kind not in _VARIADIC]
    for param in wire:
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise ImproperlyConfigured(
                f"{func.__qualname__}: positional-only parameter {param.name!r} "
                "cannot be passed by name."
            )
    return wire


def type_hints(func):
    try:
        return typing.get_type_hints(func, include_extras=True)
    except Exception:
        return dict(getattr(func, "__annotations__", {}))


def accepts_extra(func):
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in inspect.signature(func).parameters.values()
    )


# --- schema ------------------------------------------------------------------


def split_annotated(annotation):
    """``Annotated[X, "text"]`` -> (X, "text")."""
    description = None
    if get_origin(annotation) is Annotated:
        annotation, *metadata = get_args(annotation)
        for item in metadata:
            if isinstance(item, str):
                description = item
    return annotation, description


def _is_union(annotation):
    origin = get_origin(annotation)
    return origin is Union or origin is _UNION_TYPE


def annotation_schema(annotation):
    annotation, description = split_annotated(annotation)
    schema = _schema(annotation)
    if description:
        schema = {**schema, "description": description}
    return schema


def _schema(annotation):
    if annotation is EMPTY or annotation is Any:
        return {}
    if annotation is type(None):
        return {"type": "null"}
    if annotation in _SIMPLE:
        return dict(_SIMPLE[annotation])
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return {"enum": [member.value for member in annotation]}
    if hasattr(annotation, "model_json_schema"):  # pydantic model
        return annotation.model_json_schema()
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return {"enum": list(args)}
    if _is_union(annotation):
        # anyOf rather than a ``type`` array: single-type dialects reject arrays.
        return {"anyOf": [annotation_schema(arg) for arg in args]}
    if annotation in (list, set, frozenset, tuple):
        return {"type": "array"}
    if origin in (list, set, frozenset) or (
        origin is tuple and len(args) == 2 and args[1] is Ellipsis
    ):
        return {"type": "array", "items": annotation_schema(args[0])}
    if annotation is dict:
        return {"type": "object"}
    if origin is dict:
        values = annotation_schema(args[1])
        if not values:  # dict[str, Any]: an empty schema constrains nothing
            return {"type": "object"}
        return {"type": "object", "additionalProperties": values}
    raise TypeError(f"Unsupported type annotation: {annotation!r}")


def _default_value(value):
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, (str, int, float, bool)):
        return value
    return None


def signature_schema(func):
    hints = type_hints(func)
    properties, required = {}, []
    for param in wire_parameters(func):
        try:
            prop = annotation_schema(hints.get(param.name, EMPTY))
        except TypeError as error:
            raise ImproperlyConfigured(
                f"{func.__qualname__}, parameter {param.name!r}: {error}"
            ) from None
        if param.default is EMPTY:
            required.append(param.name)
        elif _default_value(param.default) is not None:
            prop["default"] = _default_value(param.default)
        properties[param.name] = prop
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    if not accepts_extra(func):
        schema["additionalProperties"] = False
    return schema


def parameter_description(func, name):
    return split_annotated(type_hints(func).get(name, EMPTY))[1]


# --- conversion --------------------------------------------------------------


def coerce_arguments(func, arguments):
    """Validate client ``arguments`` against ``func``'s hints; return kwargs."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidArguments("Arguments must be an object.")
    hints = type_hints(func)
    params = {param.name: param for param in wire_parameters(func)}
    if not accepts_extra(func):
        for key in arguments:
            if key not in params:
                raise InvalidArguments(f"Unknown argument: {key}.")
    kwargs = {}
    for key, value in arguments.items():
        if key in params:
            kwargs[key] = coerce(hints.get(key, EMPTY), value, key)
        else:
            kwargs[key] = value
    for name, param in params.items():
        if name not in kwargs and param.default is EMPTY:
            raise InvalidArguments(f"Missing required argument: {name}.")
    return kwargs


def coerce_strings(func, values):
    """Convert string values (URI parameters, prompt arguments) by type hints."""
    hints = type_hints(func)
    params = {param.name for param in wire_parameters(func)}
    return {
        key: from_string(hints.get(key, EMPTY), value, key) if key in params else value
        for key, value in values.items()
    }


def _invalid(path, expected):
    return InvalidArguments(f"{path} must be {expected}.")


def coerce(annotation, value, path):
    annotation, _ = split_annotated(annotation)
    if annotation is EMPTY or annotation is Any:
        return value
    if annotation is type(None):
        if value is None:
            return None
        raise _invalid(path, "null")
    if annotation is bool:
        if isinstance(value, bool):
            return value
        raise _invalid(path, "a boolean")
    if annotation is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise _invalid(path, "an integer")
    if annotation is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise _invalid(path, "a number")
    if annotation is str:
        if isinstance(value, str):
            return value
        raise _invalid(path, "a string")
    if annotation in (decimal.Decimal, datetime.datetime, datetime.date, uuid.UUID):
        if isinstance(value, str) or (
            annotation is decimal.Decimal
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            return from_string(annotation, str(value), path)
        raise _invalid(path, "a string")
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        try:
            return annotation(value)
        except ValueError:
            raise _invalid(
                path, f"one of {[member.value for member in annotation]}"
            ) from None
    if hasattr(annotation, "model_validate"):  # pydantic model
        try:
            return annotation.model_validate(value)
        except Exception as error:
            raise InvalidArguments(f"{path}: {error}") from None
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        for option in args:
            if value == option and type(value) is type(option):
                return value
        raise _invalid(path, f"one of {list(args)}")
    if _is_union(annotation):
        for option in sorted(args, key=lambda arg: arg is not type(None)):
            try:
                return coerce(option, value, path)
            except InvalidArguments:
                continue
        raise InvalidArguments(f"{path} has an invalid value.")
    if annotation in (list, set, frozenset, tuple) or origin in (
        list,
        set,
        frozenset,
        tuple,
    ):
        if not isinstance(value, list):
            raise _invalid(path, "an array")
        if args:
            value = [
                coerce(args[0], item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        container = origin or annotation
        return value if container is list else container(value)
    if annotation is dict or origin is dict:
        if not isinstance(value, dict):
            raise _invalid(path, "an object")
        if len(args) == 2:
            return {
                key: coerce(args[1], item, f"{path}.{key}")
                for key, item in value.items()
            }
        return value
    return value


def from_string(annotation, value, path):
    annotation, _ = split_annotated(annotation)
    if _is_union(annotation):
        for option in get_args(annotation):
            if option is type(None):
                continue
            try:
                return from_string(option, value, path)
            except InvalidArguments:
                continue
        raise InvalidArguments(f"{path} has an invalid value.")
    try:
        if annotation is bool:
            if value.lower() in ("true", "1", "yes"):
                return True
            if value.lower() in ("false", "0", "no"):
                return False
            raise ValueError
        if annotation in (int, float, uuid.UUID):
            return annotation(value)
        if annotation is decimal.Decimal:
            return decimal.Decimal(value)
        if annotation is datetime.datetime:
            return datetime.datetime.fromisoformat(value)
        if annotation is datetime.date:
            return datetime.date.fromisoformat(value)
        if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            return annotation(value)
        if get_origin(annotation) is Literal:
            if value in get_args(annotation):
                return value
            raise ValueError
    except (ValueError, decimal.InvalidOperation):
        raise InvalidArguments(f"{path} has an invalid value.") from None
    return value


# --- hand-written schemas ----------------------------------------------------

_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _matches(type_name, value):
    if type_name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _JSON_TYPES[type_name])


def validate_json(schema, value, path="arguments"):
    """The subset of JSON Schema that tool input schemas commonly use."""
    types_ = schema.get("type")
    if types_ is not None:
        types_ = types_ if isinstance(types_, list) else [types_]
        if not any(_matches(type_name, value) for type_name in types_):
            raise InvalidArguments(f"{path} must be of type {' or '.join(types_)}.")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidArguments(f"{path} must be one of {schema['enum']}.")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise InvalidArguments(f"Missing required argument: {name}.")
        for name, item in value.items():
            if name in properties:
                validate_json(properties[name], item, f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                raise InvalidArguments(f"Unknown argument: {name}.")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_json(schema["items"], item, f"{path}[{index}]")

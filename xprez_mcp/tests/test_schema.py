import datetime
import decimal
import enum
import sys
import unittest
import uuid
from typing import Annotated, Any, Literal, Optional

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from xprez_mcp.core.schema import (
    annotation_schema,
    coerce_arguments,
    coerce_strings,
    signature_schema,
    validate_json,
)
from xprez_mcp.errors import InvalidArguments


class Size(enum.Enum):
    S = "s"
    M = "m"


class AnnotationSchemaTests(SimpleTestCase):
    def test_simple_types(self):
        self.assertEqual(annotation_schema(str), {"type": "string"})
        self.assertEqual(annotation_schema(int), {"type": "integer"})
        self.assertEqual(annotation_schema(float), {"type": "number"})
        self.assertEqual(annotation_schema(bool), {"type": "boolean"})
        self.assertEqual(
            annotation_schema(datetime.date), {"type": "string", "format": "date"}
        )
        self.assertEqual(
            annotation_schema(decimal.Decimal), {"type": "string", "format": "decimal"}
        )
        self.assertEqual(annotation_schema(Any), {})

    def test_containers(self):
        self.assertEqual(
            annotation_schema(list[int]),
            {"type": "array", "items": {"type": "integer"}},
        )
        self.assertEqual(annotation_schema(list), {"type": "array"})
        self.assertEqual(
            annotation_schema(dict[str, bool]),
            {"type": "object", "additionalProperties": {"type": "boolean"}},
        )

    def test_choices(self):
        self.assertEqual(annotation_schema(Literal["a", "b"]), {"enum": ["a", "b"]})
        self.assertEqual(annotation_schema(Size), {"enum": ["s", "m"]})

    def test_unions(self):
        self.assertEqual(
            annotation_schema(Optional[int]),
            {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        )
        self.assertEqual(
            annotation_schema(Optional[list[int]]),
            {
                "anyOf": [
                    {"type": "array", "items": {"type": "integer"}},
                    {"type": "null"},
                ]
            },
        )

    @unittest.skipIf(sys.version_info < (3, 10), "X | Y needs Python 3.10")
    def test_pipe_unions(self):
        self.assertEqual(
            annotation_schema(eval("int | None")),
            {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        )

    def test_free_form_dict(self):
        self.assertEqual(annotation_schema(dict[str, Any]), {"type": "object"})
        self.assertEqual(
            annotation_schema(dict[str, int]),
            {"type": "object", "additionalProperties": {"type": "integer"}},
        )

    def test_annotated_description(self):
        self.assertEqual(
            annotation_schema(Annotated[str, "A name"]),
            {"type": "string", "description": "A name"},
        )

    def test_unsupported(self):
        with self.assertRaises(TypeError):
            annotation_schema(object)


class SignatureSchemaTests(SimpleTestCase):
    def test_method(self):
        def search(self, ctx, query: str, limit: int = 10, size: Size = Size.S):
            pass

        self.assertEqual(
            signature_schema(search),
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "size": {"enum": ["s", "m"], "default": "s"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def test_function_without_arguments(self):
        def ping(ctx):
            pass

        self.assertEqual(
            signature_schema(ping),
            {"type": "object", "properties": {}, "additionalProperties": False},
        )

    def test_context_is_required(self):
        def broken(self):
            pass

        with self.assertRaises(ImproperlyConfigured):
            signature_schema(broken)

    def test_unsupported_annotation_names_the_parameter(self):
        def tool(ctx, value: object):
            pass

        with self.assertRaisesMessage(ImproperlyConfigured, "'value'"):
            signature_schema(tool)


class CoerceArgumentsTests(SimpleTestCase):
    def tool(
        self,
        ctx,
        day: datetime.date,
        size: Size = Size.S,
        tags: Optional[list[str]] = None,
        amount: Optional[decimal.Decimal] = None,
        key: Optional[uuid.UUID] = None,
        flag: bool = False,
    ):
        pass

    def test_converts(self):
        kwargs = coerce_arguments(
            self.tool.__func__,
            {
                "day": "2026-11-13",
                "size": "m",
                "tags": ["a"],
                "amount": 1.5,
                "key": "12345678-1234-5678-1234-567812345678",
                "flag": True,
            },
        )
        self.assertEqual(kwargs["day"], datetime.date(2026, 11, 13))
        self.assertIs(kwargs["size"], Size.M)
        self.assertEqual(kwargs["tags"], ["a"])
        self.assertEqual(kwargs["amount"], decimal.Decimal("1.5"))
        self.assertIsInstance(kwargs["key"], uuid.UUID)

    def test_none_for_optional(self):
        kwargs = coerce_arguments(
            self.tool.__func__, {"day": "2026-11-13", "tags": None}
        )
        self.assertIsNone(kwargs["tags"])

    def test_errors(self):
        cases = [
            ({}, "Missing required argument: day."),
            ({"day": "2026-11-13", "extra": 1}, "Unknown argument: extra."),
            ({"day": "not a date"}, "day has an invalid value."),
            ({"day": "2026-11-13", "size": "xl"}, "size must be one of ['s', 'm']."),
            ({"day": "2026-11-13", "flag": "yes"}, "flag must be a boolean."),
            ({"day": "2026-11-13", "tags": [1]}, "tags has an invalid value."),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaisesMessage(InvalidArguments, message):
                    coerce_arguments(self.tool.__func__, arguments)

    def test_bool_is_not_int(self):
        def tool(ctx, count: int):
            pass

        with self.assertRaisesMessage(InvalidArguments, "count must be an integer."):
            coerce_arguments(tool, {"count": True})

    def test_arguments_must_be_object(self):
        def tool(ctx):
            pass

        with self.assertRaises(InvalidArguments):
            coerce_arguments(tool, ["a"])

    def test_from_strings(self):
        def resource(ctx, id: int, day: Optional[datetime.date] = None, name: str = ""):
            pass

        self.assertEqual(
            coerce_strings(resource, {"id": "7", "day": "2026-01-02", "name": "x"}),
            {"id": 7, "day": datetime.date(2026, 1, 2), "name": "x"},
        )
        with self.assertRaises(InvalidArguments):
            coerce_strings(resource, {"id": "seven"})


class ValidateJsonTests(SimpleTestCase):
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "items": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def test_valid(self):
        validate_json(self.schema, {"name": "a", "items": [1, 2]})

    def test_invalid(self):
        cases = [
            ({}, "Missing required argument: name."),
            ({"name": 1}, "arguments.name must be of type string."),
            ({"name": "a", "items": [True]}, "arguments.items[0] must be of type"),
            ({"name": "a", "other": 1}, "Unknown argument: other."),
        ]
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesMessage(InvalidArguments, message):
                    validate_json(self.schema, value)


class FromStringTests(SimpleTestCase):
    def test_datetime_forms(self):
        def tool(ctx, when: datetime.datetime):
            pass

        utc = datetime.timezone.utc
        cases = {
            # "Z" is what clients send; Python before 3.11 does not read it
            "2024-01-02T10:00:00Z": datetime.datetime(2024, 1, 2, 10, tzinfo=utc),
            "2024-01-02T10:00:00+00:00": datetime.datetime(2024, 1, 2, 10, tzinfo=utc),
            "2024-01-02T10:00:00": datetime.datetime(2024, 1, 2, 10),
            "2024-01-02": datetime.datetime(2024, 1, 2),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                kwargs = coerce_arguments(tool, {"when": value})
                self.assertEqual(kwargs, {"when": expected})
        with self.assertRaises(InvalidArguments):
            coerce_arguments(tool, {"when": "tomorrow"})

"""A small JSON Schema (draft 2020-12 subset) checker with no third-party dependency.

The lock file has no ``jsonschema`` package and the scorer must run from a clean checkout,
so the evaluation schemas in ``alibi/eval/schemas/`` are checked with this module. The
schemas stay standard JSON Schema files, so any full implementation can check them too.

Supported keywords: ``type`` (string or list), ``enum``, ``const``, ``properties``,
``required``, ``additionalProperties`` (bool or schema), ``items``, ``minItems``,
``maxItems``, ``uniqueItems``, ``minimum``, ``maximum``, ``exclusiveMinimum``,
``exclusiveMaximum``, ``minLength``, ``maxLength``, ``pattern``, ``format`` (``date-time``
only; other formats are ignored, as the standard allows), ``anyOf``, ``oneOf``, ``allOf``,
``not``, ``$ref`` to ``#/$defs/<name>`` or ``#/definitions/<name>``.

``integer`` accepts Python ``int`` only (``bool`` and ``1.0`` are rejected), so money
fields cannot be smuggled in as floats or booleans. ``number`` accepts int and float.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")


class SchemaError(ValueError):
    """The schema itself is malformed (bad $ref, unknown type name)."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _type_ok(name: str, value: Any) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return _is_int(value)
    if name == "number":
        return _is_int(value) or (isinstance(value, float) and value == value)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise SchemaError(f"unknown type name {name!r}")


def _resolve(ref: str, root: dict) -> dict:
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            name = ref[len(prefix):]
            table = root.get(prefix[2:-1], {})
            if name not in table:
                raise SchemaError(f"unresolved $ref {ref!r}")
            return table[name]
    raise SchemaError(f"only local $ref to #/$defs/ is supported, got {ref!r}")


def parse_date_time(text: str) -> datetime | None:
    """Return the datetime if ``text`` is an ISO-8601 timestamp with a zone, else None."""
    if not isinstance(text, str) or not DATE_TIME.match(text):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _errors(schema: Any, value: Any, root: dict, path: str, out: list[str]) -> None:
    if schema is True:
        return
    if schema is False:
        out.append(f"{path}: no value is allowed here")
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"schema at {path} must be an object or boolean")
    if "$ref" in schema:
        _errors(_resolve(schema["$ref"], root), value, root, path, out)
        rest = {k: v for k, v in schema.items() if k != "$ref"}
        if rest:
            _errors(rest, value, root, path, out)
        return

    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(name, value) for name in names):
            out.append(f"{path}: expected {' or '.join(names)}, got {_describe(value)}")
            return
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: must equal {schema['const']!r}")

    if isinstance(value, dict):
        props = schema.get("properties", {})
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            out.append(f"{path}: needs at least {schema['minProperties']} propert(y/ies)")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            out.append(f"{path}: allows at most {schema['maxProperties']} properties")
        for name in schema.get("required", []):
            if name not in value:
                out.append(f"{path}: missing required property {name!r}")
        for name, sub in props.items():
            if name in value:
                _errors(sub, value[name], root, f"{path}/{name}", out)
        extra = schema.get("additionalProperties", True)
        if extra is not True:
            for name in value:
                if name in props:
                    continue
                if extra is False:
                    out.append(f"{path}: unexpected property {name!r}")
                else:
                    _errors(extra, value[name], root, f"{path}/{name}", out)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: allows at most {schema['maxItems']} item(s)")
        if schema.get("uniqueItems"):
            seen = []
            for item in value:
                if item in seen:
                    out.append(f"{path}: items must be unique, {item!r} repeats")
                    break
                seen.append(item)
        if "items" in schema:
            for index, item in enumerate(value):
                _errors(schema["items"], item, root, f"{path}/{index}", out)
    if _is_int(value) or isinstance(value, float):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value} is above the maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append(f"{path}: {value} must be above {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            out.append(f"{path}: {value} must be below {schema['exclusiveMaximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: shorter than {schema['minLength']} character(s)")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: {value!r} does not match {schema['pattern']!r}")
        if schema.get("format") == "date-time" and parse_date_time(value) is None:
            out.append(f"{path}: {value!r} is not an ISO-8601 timestamp with a timezone")

    for keyword in ("allOf",):
        for index, sub in enumerate(schema.get(keyword, [])):
            _errors(sub, value, root, path, out)
    if "anyOf" in schema:
        if not any(not _collect(sub, value, root, path) for sub in schema["anyOf"]):
            out.append(f"{path}: matches none of the allowed alternatives")
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not _collect(sub, value, root, path))
        if matches != 1:
            out.append(f"{path}: must match exactly one alternative, matched {matches}")
    if "not" in schema and not _collect(schema["not"], value, root, path):
        out.append(f"{path}: matches a forbidden schema")


def _collect(schema: Any, value: Any, root: dict, path: str) -> list[str]:
    found: list[str] = []
    _errors(schema, value, root, path, found)
    return found


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def errors(schema: dict, value: Any) -> list[str]:
    """Every violation as ``<json pointer>: <message>``. Empty means valid."""
    return _collect(schema, value, schema, "$")


def validate(schema: dict, value: Any) -> None:
    """Raise ``ValueError`` listing the violations, if any."""
    found = errors(schema, value)
    if found:
        raise ValueError("\n".join(found))

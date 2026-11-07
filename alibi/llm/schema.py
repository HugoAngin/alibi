"""A small JSON Schema checker for validating model output.

The team's lock file has no `jsonschema` package, so this covers the subset the
Alibi prompts use: type (including type lists), properties, required,
additionalProperties, enum, const, items, minItems, maxItems, minimum, maximum,
minLength, maxLength, anyOf and oneOf. Anything else in a schema is ignored, so
keep schemas inside this subset. Returns a list of "path: problem" strings.
"""

from __future__ import annotations

from typing import Any

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(instance: Any, schema: dict, path: str = "$") -> list[str]:
    """Return every violation found. An empty list means the instance conforms."""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return problems

    if "const" in schema and instance != schema["const"]:
        problems.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(t, lambda v: False)(instance) for t in types):
            problems.append(f"{path}: expected type {expected!r}, got {type(instance).__name__}")
            return problems  # further keyword checks assume the type matched

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                problems.append(f"{path}: missing required property {name!r}")
        for name, value in instance.items():
            if name in props:
                problems.extend(validate(value, props[name], f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}: unexpected property {name!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                problems.extend(validate(value, schema["additionalProperties"], f"{path}.{name}"))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            problems.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            problems.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(instance):
                problems.extend(validate(item, schema["items"], f"{path}[{i}]"))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            problems.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            problems.append(f"{path}: longer than {schema['maxLength']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            problems.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            problems.append(f"{path}: above maximum {schema['maximum']}")

    if "anyOf" in schema:
        if not any(not validate(instance, s, path) for s in schema["anyOf"]):
            problems.append(f"{path}: matches none of anyOf")
    if "oneOf" in schema:
        matches = sum(1 for s in schema["oneOf"] if not validate(instance, s, path))
        if matches != 1:
            problems.append(f"{path}: matches {matches} of oneOf, expected exactly 1")

    return problems

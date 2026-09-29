"""A small JSON Schema validator for LLM replies (no dependency).

Covers what structured output needs: `type` (one or a list of string, number, integer,
boolean, array, object, null), `properties`, `required`, `additionalProperties: false`,
`items`, `enum`, `minItems`/`maxItems`, `minLength`/`maxLength`, `minimum`/`maximum`.
Other keywords (`description`, `title`, ...) are allowed and ignored.
"""

from __future__ import annotations

from typing import Any

TYPES = ("string", "number", "integer", "boolean", "array", "object", "null")
_SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum", "minItems", "maxItems",
    "minLength", "maxLength", "minimum", "maximum", "description", "title", "default", "examples",
})


def _is(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, float) and value.is_integer())
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return value is None


def check_schema(schema: Any, path: str = "schema") -> list[str]:
    """Problems with the schema itself (unknown types, malformed properties)."""
    if not isinstance(schema, dict):
        return [f"{path} must be an object"]
    problems = []
    unknown = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unknown:
        problems.append(f"{path} uses unsupported keywords: {', '.join(unknown)}")
    kinds = schema.get("type")
    for kind in kinds if isinstance(kinds, list) else [kinds] if kinds is not None else []:
        if kind not in TYPES:
            problems.append(f"{path}.type '{kind}' isn't one of {', '.join(TYPES)}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        problems.append(f"{path}.properties must be an object")
    else:
        for name, sub in properties.items():
            problems += check_schema(sub, f"{path}.properties.{name}")
    if "items" in schema:
        problems += check_schema(schema["items"], f"{path}.items")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        problems.append(f"{path}.required must be a list of property names")
    return problems


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Why `instance` doesn't match `schema` ("$.action_items[0].task: is required"); [] if it does."""
    kinds = schema.get("type")
    if kinds is not None:
        allowed = kinds if isinstance(kinds, list) else [kinds]
        if not any(_is(instance, kind) for kind in allowed):
            return [f"{path}: expected {' or '.join(allowed)}, got {type(instance).__name__ if instance is not None else 'null'}"]
    errors: list[str] = []
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
    if _is(instance, "number") and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above the maximum {schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(instance):
                errors += validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}.{name}: is required")
        for name, value in instance.items():
            if name in properties:
                errors += validate(value, properties[name], f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{name}: isn't allowed (not in the schema)")
    return errors

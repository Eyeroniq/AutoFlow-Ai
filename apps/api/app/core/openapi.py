"""OpenAPI document touch-ups.

pydantic marks a field that has a default as not required, in the schema of a model that is only ever
sent back as a response too, even though the API always includes it. Generated clients then type every
such field as possibly missing. `tighten` marks every property of a response-only schema as required
(the truth for FastAPI responses, which serialize defaults); schemas that are also accepted as request
bodies keep their optional fields, since a client may omit them there.
"""

from typing import Any


def _refs(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _refs(item)
    return found


def _closure(roots: set[str], schemas: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        stack.extend(_refs(schemas[name]))
    return seen


def tighten(spec: dict[str, Any]) -> dict[str, Any]:
    schemas = spec.get("components", {}).get("schemas", {})
    requested: set[str] = set()
    returned: set[str] = set()
    for path_item in spec.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            requested |= _refs(operation.get("requestBody", {}))
            for code, response in operation.get("responses", {}).items():
                if str(code).startswith("2") or code == "default":
                    returned |= _refs(response)
            # Query/path parameters can also point at schemas (enums).
            requested |= _refs(operation.get("parameters", []))
    response_only = _closure(returned, schemas) - _closure(requested, schemas)
    for name in response_only:
        schema = schemas[name]
        properties = schema.get("properties")
        if isinstance(properties, dict) and properties:
            schema["required"] = sorted(set(schema.get("required", [])) | set(properties))
    return spec

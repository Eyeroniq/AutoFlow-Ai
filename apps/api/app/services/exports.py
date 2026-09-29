"""An execution's final output as a downloadable file: JSON as is, or CSV.

CSV flattens any JSON into rows: every list of objects becomes one row per object (its
fields as columns, nested objects as dotted columns, lists joined with "; "), and every
other value one row of its own. The `group` column is where the row came from, e.g.
`invoice.entities.amounts`. So entity extraction output reads as one row per entity.
"""

import csv
import io
import json
from typing import Any

from flowforge_engine import variables


def _cell(value: Any) -> Any:
    if isinstance(value, list):
        return "; ".join(str(_cell(item)) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else value


def _flat(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flat(value, f"{name}."))
        else:
            out[name] = _cell(value)
    return out


def output_rows(value: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            rows += output_rows(item, f"{path}.{key}" if path else str(key))
        return rows
    if isinstance(value, list):
        if not value:
            return []
        if all(isinstance(item, dict) for item in value):
            return [{"group": path, **_flat(item)} for item in value]
        return [{"group": path, "value": _cell(value)}]
    return [{"group": path, "value": _cell(value)}]


def to_csv(final_output: dict[str, Any]) -> str:
    rows = output_rows(final_output)
    columns: list[str] = ["group", "value"]
    for row in rows:
        columns += [key for key in row if key not in columns]
    if not any("value" in row for row in rows):
        columns.remove("value")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue()


def to_json(final_output: dict[str, Any]) -> str:
    return json.dumps(final_output, indent=2, ensure_ascii=False, default=variables.stringify)

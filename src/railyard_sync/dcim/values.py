"""Small readers for the JSON NetBox and Nautobot return, shared by the loaders.

Both APIs give choice fields as ``{"value", "label"}``, nested objects as ``{"id", "name"|"display"|…}`` (Nautobot
only names them at ``?depth=1``), and lengths and masses as a number plus a unit choice. Everything here returns the
snapshot's conventions: strings for ids, slugs for enumerations, millimetres, kilograms and metres.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import DCIMError, DCIMVersionError

MM_PER = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
KG_PER = {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "oz": 0.028349523125}
M_PER = {"km": 1000.0, "m": 1.0, "cm": 0.01, "mi": 1609.344, "ft": 0.3048, "in": 0.0254}


def value(field: Any, default: str = "") -> str:
    """An enumeration's slug: choice fields come as ``{"value", "label"}``."""
    if isinstance(field, dict):
        field = field.get("value")
    if field is None:
        return default
    return str(field)


def ref_id(field: Any) -> str | None:
    """The id of a nested object (``{"id": 7, …}``) or a bare id, as a string."""
    if isinstance(field, dict):
        field = field.get("id")
    if field is None or field == "":
        return None
    return str(field)


def name_of(field: Any) -> str:
    """The name of a nested object (role, tenant, platform, manufacturer…), or ""."""
    if isinstance(field, dict):
        return str(field.get("name") or field.get("display") or "")
    return "" if field is None else str(field)


def tag_names(item: dict) -> list[str]:
    return [str(t.get("name") if isinstance(t, dict) else t) for t in item.get("tags") or []]


def number(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def integer(raw: Any, default: int) -> int:
    parsed = number(raw)
    return default if parsed is None else int(parsed)


def convert(raw: Any, unit: Any, table: dict[str, float], default_unit: str) -> float | None:
    parsed = number(raw)
    if parsed is None:
        return None
    factor = table.get(value(unit, default_unit).lower())
    if factor is None:
        raise DCIMError(f"Unknown unit {value(unit)!r} (expected one of {', '.join(table)}).")
    return round(parsed * factor, 6)


def mm(raw: Any, unit: Any) -> float | None:
    return convert(raw, unit, MM_PER, "mm")


def kg(raw: Any, unit: Any) -> float | None:
    return convert(raw, unit, KG_PER, "kg")


def metres(raw: Any, unit: Any) -> float | None:
    return convert(raw, unit, M_PER, "m")


def parse_version(text: str, product: str) -> tuple[int, int, int]:
    """``"4.6.2"`` / ``"v2.4.0-beta.1"`` -> ``(4, 6, 2)``; an unreadable version is a :class:`DCIMVersionError`."""
    match = re.match(r"\s*v?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        raise DCIMVersionError(f"Could not read the {product} version from /api/status/ ({text!r}).")
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)

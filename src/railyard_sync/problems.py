"""Railyard's validation problems (``/api/validate`` and a refused save's ``problems``) in words.

A problem names its objects by id (``rackId``, ``placementId``); :class:`ProblemNamer` looks them up in the
document that was checked, so a message reads "rack 'A01' (nb-rack-100), device 'sw1' (nb-dev-1): …"
rather than ids alone.
"""

from __future__ import annotations

from typing import Any

ERROR, WARNING = "error", "warning"


class ProblemNamer:
    """Names for the racks and devices of a Project JSON document, by id."""

    def __init__(self, document: dict | None) -> None:
        self.racks: dict[str, str] = {}
        self.devices: dict[str, tuple[str, str]] = {}  # placement id -> (label, rack id)
        for rack in (document or {}).get("racks") or []:
            if not isinstance(rack, dict):
                continue
            rack_id = str(rack.get("id") or "")
            self.racks[rack_id] = str(rack.get("name") or "")
            for placement in rack.get("placements") or []:
                if isinstance(placement, dict):
                    self.devices[str(placement.get("id") or "")] = (str(placement.get("label") or ""), rack_id)

    def where(self, problem: dict) -> str:
        rack_id = str(problem.get("rackId") or "")
        placement_id = str(problem.get("placementId") or "")
        parts = []
        if placement_id:
            label, owner = self.devices.get(placement_id, ("", ""))
            rack_id = rack_id or owner
            parts.append(f"device {label!r} ({placement_id})" if label else f"device {placement_id}")
        if rack_id:
            name = self.racks.get(rack_id, "")
            parts.append(f"rack {name!r} ({rack_id})" if name else f"rack {rack_id}")
        return ", in ".join(parts)

    def describe(self, problem: dict) -> str:
        """``rack 'A01' (nb-rack-100): the rack name "A01" is already used in this space [rack.name-duplicate]``."""
        where = self.where(problem)
        message = str(problem.get("message") or "").strip() or "(no message)"
        code = str(problem.get("code") or "")
        text = f"{where}: {message}" if where else message
        return f"{text} [{code}]" if code else text


def severity(problem: dict) -> str:
    return WARNING if str(problem.get("severity") or ERROR) == WARNING else ERROR


def identity(problem: dict) -> tuple[str, str, str, str]:
    """What tells one finding from another: an estate that already had it did not get it from the import."""
    return (
        str(problem.get("code") or ""),
        str(problem.get("rackId") or ""),
        str(problem.get("placementId") or ""),
        str(problem.get("message") or ""),
    )


def listing(problems: list[dict], namer: ProblemNamer, *, limit: int = 20) -> list[str]:
    """Up to ``limit`` problems as lines, then how many more there are."""
    lines = [namer.describe(p) for p in problems[:limit]]
    if len(problems) > limit:
        lines.append(f"… and {len(problems) - limit} more")
    return lines


def as_problems(value: Any) -> list[dict]:
    return [p for p in value or [] if isinstance(p, dict)] if isinstance(value, list) else []

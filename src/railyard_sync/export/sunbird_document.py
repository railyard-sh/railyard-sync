"""Railyard's ``sunbird-sync`` document: the racked devices of an estate, with their serials, for Sunbird dcTrack.

Railyard generates it server-side, as a deliverable: ``POST /api/projects/{id}/deliverables/sunbird-sync``
(``RailyardClient.sunbird_sync_document``), scoped like the NetBox sync document (``options.scope``,
``options.rackIds``, ``changeRequestId``, ``versionId``)::

    {"format": "railyard-sunbird-sync", "version": 1,
     "project": {"id", "name", "revision", "changeRequestId"?, "versionId"?,
                 "scope"?: {"containerId"?, "rackIds", "cablesLeftOut", "powerLinksLeftOut"}},
     "cabinets": [{"name", "location": ["London", "LON1", "Hall A", "Row 1"], "uHeight", "startingUnit",
                   "descendingUnits", "railyard": {"kind": "rack", "id"}}],
     "devices": [{"cabinet", "name", "uPosition", "uFromBottom", "uHeight", "face": "front"|"rear"|"full",
                  "make", "model", "partNumber", "serial", "railyard": {"kind": "device", "id", "rackId"}}],
     "skipped": [{"cabinet", "name", "reason", "railyard"}],
     "warnings": [...]}

``devices`` lists every racked device in the scope, those without a serial too. ``uPosition`` is the device's
lowest-numbered U in the rack's own numbering; ``uFromBottom`` the 1-based unit of its physically bottom U, counted
from the cabinet's bottom (what dcTrack's ``cmbUPosition`` is). ``face: "full"`` is a full-depth device. A cabinet's
name is unique within its location path; a device names its cabinet, and ``railyard.rackId`` is the cabinet's
``railyard.id``, which is how a device is tied to its cabinet when two locations both have an ``R01``.

**Everything that knows the document's shape is in this module** (:func:`parse_document`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FORMAT = "railyard-sunbird-sync"
SUPPORTED_VERSIONS = (1,)
FACES = ("front", "rear", "full")

#: Upper bound on the rows read from one list, so a malformed document can't make the export read without end.
MAX_ROWS = 200_000


class SunbirdDocumentError(ValueError):
    """The document is not a Railyard Sunbird sync document this version can read."""


@dataclass(frozen=True)
class Cabinet:
    """A Railyard rack, as dcTrack will be asked for it: its name and where it sits."""

    name: str
    location: tuple[str, ...]  # the Railyard space path, outermost first
    rack_id: str = ""
    u_height: int | None = None
    starting_unit: int | None = None
    descending_units: bool = False

    @property
    def where(self) -> str:
        """``London / LON1 / Hall A / Row 1``, for messages."""
        return " / ".join(self.location)


@dataclass(frozen=True)
class Device:
    """A racked Railyard device: where it sits and the serial Railyard records for it (``""`` when none)."""

    cabinet: Cabinet  # with no location when the document does not list it (a warning says so)
    cabinet_name: str
    name: str
    u_position: int | None
    u_from_bottom: int | None
    u_height: int | None
    face: str  # front | rear | full
    serial: str
    make: str = ""
    model: str = ""
    part_number: str = ""
    device_id: str = ""  # the placement id


@dataclass(frozen=True)
class Skipped:
    """A device the document itself left out (a zero-U PDU, say), with Railyard's reason."""

    cabinet: str
    name: str
    reason: str
    device_id: str = ""


@dataclass
class SunbirdDocument:
    project_id: str
    project_name: str
    revision: Any = None
    change_request_id: str = ""
    version_id: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    cabinets: list[Cabinet] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---- value readers -----------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None or isinstance(value, dict | list):
        return ""
    return str(value).strip()


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in ("true", "1", "yes")


def _identity(row: dict) -> dict:
    ident = row.get("railyard")
    return ident if isinstance(ident, dict) else {}


def _rows(document: dict, key: str) -> list[dict]:
    value = document.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
        raise SunbirdDocumentError(f"The Sunbird sync document's {key!r} must be a list of objects.")
    if len(value) > MAX_ROWS:
        raise SunbirdDocumentError(f"The Sunbird sync document has {len(value)} {key}; at most {MAX_ROWS}.")
    return value


# ---- the document ------------------------------------------------------------------------------


def parse_document(document: Any) -> SunbirdDocument:
    """Check the envelope and read the cabinets, devices, skipped devices and warnings."""
    if not isinstance(document, dict):
        raise SunbirdDocumentError("The Sunbird sync document must be a JSON object.")
    if document.get("format") != FORMAT:
        raise SunbirdDocumentError(
            f"Not a Railyard Sunbird sync document: format is {document.get('format')!r}, not {FORMAT!r}."
        )
    version = document.get("version")
    if isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        raise SunbirdDocumentError(
            f"Sunbird sync document version {version!r} is not supported by this railyard-sync (it reads version "
            f"{', '.join(map(str, SUPPORTED_VERSIONS))}): upgrade railyard-sync."
        )
    project = document.get("project")
    if not isinstance(project, dict) or not _text(project.get("id")):
        raise SunbirdDocumentError("The Sunbird sync document names no project id.")
    scope = project.get("scope") if isinstance(project.get("scope"), dict) else {}
    doc = SunbirdDocument(
        project_id=_text(project.get("id")),
        project_name=_text(project.get("name")) or _text(project.get("id")),
        revision=project.get("revision"),
        change_request_id=_text(project.get("changeRequestId")),
        version_id=_text(project.get("versionId")),
        scope=dict(scope),
        warnings=[_text(w) for w in _rows_or_texts(document.get("warnings")) if _text(w)],
    )

    by_id: dict[str, Cabinet] = {}
    by_name: dict[str, list[Cabinet]] = {}
    for row in _rows(document, "cabinets"):
        name = _text(row.get("name"))
        if not name:
            raise SunbirdDocumentError("A cabinet in the Sunbird sync document has no name.")
        location = row.get("location") or []
        if not isinstance(location, list):
            raise SunbirdDocumentError(f"Cabinet {name!r}: 'location' must be a list of space names.")
        cabinet = Cabinet(
            name=name,
            location=tuple(_text(p) for p in location if _text(p)),
            rack_id=_text(_identity(row).get("id")),
            u_height=_int(row.get("uHeight")),
            starting_unit=_int(row.get("startingUnit")),
            descending_units=_bool(row.get("descendingUnits")),
        )
        doc.cabinets.append(cabinet)
        if cabinet.rack_id:
            by_id[cabinet.rack_id] = cabinet
        by_name.setdefault(name.casefold(), []).append(cabinet)

    unlisted: dict[tuple[str, str], Cabinet] = {}
    for row in _rows(document, "devices"):
        name = _text(row.get("name"))
        ident = _identity(row)
        cabinet_name = _text(row.get("cabinet"))
        face = _text(row.get("face")).lower()
        if face not in FACES:
            raise SunbirdDocumentError(
                f"Device {name or ident.get('id')!r} in {cabinet_name!r}: face {row.get('face')!r} is not one of "
                f"{', '.join(FACES)}."
            )
        rack_id = _text(ident.get("rackId"))
        cabinet = by_id.get(rack_id) if rack_id else None
        if cabinet is None:
            named = by_name.get(cabinet_name.casefold(), [])
            cabinet = named[0] if len(named) == 1 else None
        if cabinet is None:
            # A cabinet the document does not list, or lists in more than one place: known only by its name, so
            # only --location can place it.
            key = (cabinet_name.casefold(), rack_id)
            if key not in unlisted:
                unlisted[key] = Cabinet(name=cabinet_name, location=(), rack_id=rack_id)
                doc.warnings.append(
                    f"Devices name cabinet {cabinet_name!r}, which the document "
                    f"{'lists more than once' if by_name.get(cabinet_name.casefold()) else 'does not list'}: it is "
                    "looked for by name in the --location location only."
                )
            cabinet = unlisted[key]
        doc.devices.append(
            Device(
                cabinet=cabinet,
                cabinet_name=cabinet.name,
                name=name,
                u_position=_int(row.get("uPosition")),
                u_from_bottom=_int(row.get("uFromBottom")),
                u_height=_int(row.get("uHeight")),
                face=face,
                serial=_text(row.get("serial")),
                make=_text(row.get("make")),
                model=_text(row.get("model")),
                part_number=_text(row.get("partNumber")),
                device_id=_text(ident.get("id")),
            )
        )

    for row in _rows(document, "skipped"):
        doc.skipped.append(
            Skipped(
                cabinet=_text(row.get("cabinet")),
                name=_text(row.get("name")),
                reason=_text(row.get("reason")),
                device_id=_text(_identity(row).get("id")),
            )
        )
    return doc


def _rows_or_texts(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

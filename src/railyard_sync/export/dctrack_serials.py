"""Railyard's device serials -> Sunbird dcTrack: match each device to an item that already exists in dcTrack, by
cabinet and U position (never by name: the two systems name devices differently), then write the serials over
dcTrack's REST API (``--apply``) or into a File Import CSV for dcTrack's Import wizard.

Matching, for each Railyard device in the ``sunbird-sync`` document (:mod:`.sunbird_document`):

1. **Location.** ``location_map`` maps a Railyard space name to a dcTrack location code; the deepest space in the
   cabinet's path with a mapping wins, else ``location`` (``--location``). Neither: the device is ``unmapped``.
2. **Cabinet.** The dcTrack cabinet in that location with the Railyard cabinet's name, trimmed, case-insensitively
   (dcTrack stores names in upper case). None: ``cabinet-not-found``.
3. **Item.** Among the cabinet's items with a U position, those whose bottom U is the device's U (``uFromBottom``, or
   the rack's own ``uPosition`` with ``match_u="own"``) and whose rails suit its face (front: Front or Both; rear:
   Back or Both; full: any). One: matched; none: ``no-item-at-position``; several: ``ambiguous``. A matched item
   whose height differs from the device's is a ``conflict`` (``height-mismatch``): a serial on the wrong item is
   worse than none. Two Railyard devices matching the same item are both ``ambiguous``.

Then, for a matched device with a serial: dcTrack's serial equal (trimmed, case-sensitive) -> ``unchanged``; blank
-> ``update``; different -> ``conflict`` (``serial-differs``), or ``update`` with ``overwrite``. A device without a
serial is ``no-serial`` and never touches dcTrack, and a dcTrack serial is never cleared.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..log import count, seconds
from .dctrack_rest import (
    BULK_BATCH,
    DcCabinet,
    DcItem,
    DcTrackClient,
    DcTrackError,
    Location,
    RowResult,
    import_object,
)
from .sunbird_document import Cabinet, SunbirdDocument, parse_document

log = logging.getLogger(__name__)

MATCH_BOTTOM, MATCH_OWN = "bottom", "own"
MATCH_U = (MATCH_BOTTOM, MATCH_OWN)

# Outcomes, in report order.
UPDATE = "update"
UNCHANGED = "unchanged"
CONFLICT = "conflict"
AMBIGUOUS = "ambiguous"
UNMAPPED = "unmapped"
LOCATION_NOT_FOUND = "location-not-found"
CABINET_NOT_FOUND = "cabinet-not-found"
NO_POSITION = "no-position"
NO_ITEM = "no-item-at-position"
NO_SERIAL = "no-serial"
OUTCOMES = (
    UPDATE,
    UNCHANGED,
    CONFLICT,
    AMBIGUOUS,
    UNMAPPED,
    LOCATION_NOT_FOUND,
    CABINET_NOT_FOUND,
    NO_POSITION,
    NO_ITEM,
    NO_SERIAL,
)
#: Devices with a serial that could not be matched: reported, not written.
PROBLEMS = (UNMAPPED, LOCATION_NOT_FOUND, CABINET_NOT_FOUND, NO_POSITION, NO_ITEM)
#: Outcomes that make a run fail (exit 1): a serial was not written because it would be unsafe to.
FAILING = (CONFLICT, AMBIGUOUS)

# Reasons for a conflict or an ambiguity.
SERIAL_DIFFERS = "serial-differs"
SERIAL_UNREAD = "serial-unread"
HEIGHT_MISMATCH = "height-mismatch"
SEVERAL_ITEMS = "several-items"
SAME_ITEM = "same-item"
SEVERAL_CABINETS = "several-cabinets"

#: The dcTrack rails (``radioRailsUsed``) an item may use to be the device on a Railyard face.
FACE_RAILS = {"front": ("Front", "Both"), "rear": ("Back", "Both"), "full": ("Front", "Back", "Both")}

#: The File Import columns written (dcTrack's own headings; a customer's import template may differ).
IMPORT_COLUMNS = ("# Operation", "Object", "Location", "Name", "Serial Number")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class PlannedDevice:
    """One Railyard device and what the export does (or did) with its serial."""

    outcome: str
    cabinet: str  # the Railyard cabinet name
    u: int | None  # the U matched on
    name: str  # the Railyard device name
    serial: str  # Railyard's serial
    face: str
    reason: str = ""
    note: str = ""
    device_id: str = ""  # the Railyard placement id
    where: str = ""  # the Railyard space path
    location: str = ""  # the dcTrack location code
    item_id: str = ""
    item_name: str = ""
    item_class: str = ""
    item_serial: str = ""  # dcTrack's serial, as read before any write
    applied: bool | None = None  # with --apply: whether dcTrack took the update
    error: str = ""  # dcTrack's words when it refused the update

    def line(self) -> str:
        """``R01 U12 SRV-01 → dcTrack SRV01: (blank) → ABC123``, then the outcome and why for anything but an
        update."""
        text = f"{self.cabinet} U{self.u if self.u else '?'} {self.name or '(unnamed)'}"
        if self.item_name or self.item_id:
            text += f" → dcTrack {self.item_name or '#' + self.item_id}"
        if self.item_id and self.outcome in (UPDATE, UNCHANGED, CONFLICT) and self.reason != HEIGHT_MISMATCH:
            text += f": {self.item_serial or '(blank)'} → {self.serial}"
        else:
            text += f": serial {self.serial}"
        if self.outcome == UPDATE and self.item_serial:
            text += " (replaces dcTrack's serial: --overwrite)"
        elif self.outcome != UPDATE:
            label = f"{self.outcome}, {self.reason}" if self.reason else self.outcome
            text += f" [{label}{': ' + self.note if self.note else ''}]"
        if self.error:
            text += f" — dcTrack refused it: {self.error}"
        return text


@dataclass
class SerialSyncResult:
    """What ``export sunbird`` did (or, for a dry run, would do)."""

    dry_run: bool
    project_id: str
    project_name: str
    dctrack_url: str
    match_u: str
    overwrite: bool
    change_request_id: str = ""
    version_id: str = ""
    devices: list[PlannedDevice] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # the document's own skipped devices
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # updates dcTrack refused
    updated: int = 0  # updates dcTrack took (0 for a dry run)
    import_file: str = ""
    import_rows: int = 0

    def of(self, *outcomes: str) -> list[PlannedDevice]:
        return [d for d in self.devices if d.outcome in outcomes]

    @property
    def counts(self) -> dict[str, int]:
        return {outcome: len(self.of(outcome)) for outcome in OUTCOMES}

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def failed(self) -> bool:
        """Errors, conflicts or ambiguities: the run's exit code is 1."""
        return bool(self.errors or self.of(*FAILING))

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "counts": self.counts, "ok": self.ok}


# ---- matching ----------------------------------------------------------------------------------------------------


def parse_location_map(entries: list[str]) -> dict[str, str]:
    """``["Hall A=LON1-HA"]`` -> ``{"hall a": "LON1-HA"}``. Raises ``ValueError`` for an entry without ``=``."""
    out: dict[str, str] = {}
    for entry in entries or []:
        name, sep, code = entry.rpartition("=")
        if not sep or not name.strip() or not code.strip():
            raise ValueError(f"--location-map {entry!r} must be 'Railyard space name=dcTrack location code'")
        out[name.strip().casefold()] = code.strip()
    return out


def resolve_location(cabinet: Cabinet, location_map: dict[str, str], default: str) -> str:
    """The dcTrack location code for a cabinet: the deepest space in its path with a mapping, else ``default``."""
    for part in reversed(cabinet.location):
        code = location_map.get(part.strip().casefold())
        if code:
            return code
    return default.strip()


class _Reads:
    """dcTrack reads, each made once per run."""

    def __init__(self, client: DcTrackClient) -> None:
        self.client = client
        self._locations: dict[str, Location] | None = None
        self._cabinets: dict[str, dict[str, list[DcCabinet]]] = {}
        self._items: dict[str, list[DcItem]] = {}

    def location(self, code: str) -> Location | None:
        if self._locations is None:
            self._locations = {}
            for loc in self.client.locations():
                self._locations.setdefault(loc.code.casefold(), loc)
        return self._locations.get(code.casefold())

    def cabinets(self, location: Location, name: str) -> list[DcCabinet]:
        if location.code not in self._cabinets:
            by_name: dict[str, list[DcCabinet]] = {}
            for cabinet in self.client.cabinets(location.code):
                by_name.setdefault(cabinet.name.strip().casefold(), []).append(cabinet)
            self._cabinets[location.code] = by_name
        return self._cabinets[location.code].get(name.strip().casefold(), [])

    def items(self, cabinet: DcCabinet) -> list[DcItem]:
        if cabinet.id not in self._items:
            self._items[cabinet.id] = self.client.cabinet_items(cabinet.id)
        return self._items[cabinet.id]


def _settle(row: PlannedDevice, outcome: str, reason: str = "", note: str = "") -> None:
    """Give a device with a serial its outcome (a device without one stays ``no-serial``)."""
    if row.serial:
        row.outcome, row.reason, row.note = outcome, reason, note


def plan_serials(
    doc: SunbirdDocument,
    client: DcTrackClient,
    *,
    location: str = "",
    location_map: dict[str, str] | None = None,
    match_u: str = MATCH_BOTTOM,
    overwrite: bool = False,
) -> tuple[list[PlannedDevice], list[str]]:
    """Match every device in the document to a dcTrack item and decide what happens to its serial. Only reads
    dcTrack, and only for cabinets holding a device with a serial."""
    if match_u not in MATCH_U:
        raise ValueError(f"match_u must be one of {', '.join(MATCH_U)}")
    location_map = location_map or {}
    reads = _Reads(client)
    warnings: list[str] = []
    missing_codes: dict[str, int] = {}
    with_serials = {d.cabinet for d in doc.devices if d.serial}
    rows: list[PlannedDevice] = []
    places: dict[str, tuple[Location, DcCabinet]] = {}  # item id -> where it was found, for the serial reads

    for device in doc.devices:
        u = device.u_from_bottom if match_u == MATCH_BOTTOM else device.u_position
        row = PlannedDevice(
            outcome="" if device.serial else NO_SERIAL,
            cabinet=device.cabinet_name,
            u=u,
            name=device.name,
            serial=device.serial,
            face=device.face,
            device_id=device.device_id,
            where=device.cabinet.where,
        )
        rows.append(row)
        if device.cabinet not in with_serials:
            continue  # nothing in this cabinet to write: dcTrack is not asked about it
        code = resolve_location(device.cabinet, location_map, location)
        if not code:
            where = f" ({device.cabinet.where})" if device.cabinet.where else ""
            _settle(row, UNMAPPED, note=f"no --location-map entry for its spaces{where}, and no --location")
            continue
        loc = reads.location(code)
        if loc is None:
            missing_codes[code] = missing_codes.get(code, 0) + (1 if device.serial else 0)
            _settle(row, LOCATION_NOT_FOUND, note=f"dcTrack has no location with code {code!r}")
            continue
        row.location = loc.code
        found = reads.cabinets(loc, device.cabinet_name)
        if not found:
            note = f"no cabinet named {device.cabinet_name!r} in dcTrack location {loc.code}"
            _settle(row, CABINET_NOT_FOUND, note=note)
            continue
        if len(found) > 1:
            _settle(row, AMBIGUOUS, SEVERAL_CABINETS, f"{len(found)} cabinets of that name in {loc.code}")
            continue
        cabinet = found[0]
        if u is None or u <= 0:
            numbering = "from the bottom" if match_u == MATCH_BOTTOM else "in the rack's own numbering"
            _settle(row, NO_POSITION, note=f"the document gives no U {numbering}")
            continue
        rails = FACE_RAILS[device.face]
        candidates = [i for i in reads.items(cabinet) if i.u_position == u and i.rails in rails]
        if not candidates:
            _settle(row, NO_ITEM, note=f"no dcTrack item at U{u} ({device.face}) in {cabinet.name}")
            continue
        if len(candidates) > 1:
            names = ", ".join(sorted(i.name or "#" + i.id for i in candidates))
            _settle(row, AMBIGUOUS, SEVERAL_ITEMS, f"{len(candidates)} dcTrack items at U{u} ({device.face}): {names}")
            continue
        item = candidates[0]
        row.item_id, row.item_name, row.item_class = item.id, item.name, item.item_class
        places[item.id] = (loc, cabinet)
        if device.u_height and item.height and device.u_height != item.height:
            note = f"{device.u_height}U in Railyard, {item.height}U in dcTrack: not written"
            _settle(row, CONFLICT, HEIGHT_MISMATCH, note)

    # Two Railyard devices on one dcTrack item: neither can be trusted.
    by_item: dict[str, list[PlannedDevice]] = {}
    for row in rows:
        if row.item_id:
            by_item.setdefault(row.item_id, []).append(row)
    for item_rows in by_item.values():
        if len(item_rows) > 1:
            names = ", ".join(sorted(r.name for r in item_rows))
            for row in item_rows:
                _settle(row, AMBIGUOUS, SAME_ITEM, f"{len(item_rows)} Railyard devices match this item: {names}")

    # Serials, for the devices matched cleanly: one read per cabinet.
    pending = [r for r in rows if r.serial and not r.outcome]
    by_cabinet: dict[tuple[str, str], list[PlannedDevice]] = {}
    for row in pending:
        loc, cabinet = places[row.item_id]
        by_cabinet.setdefault((loc.code, cabinet.name), []).append(row)
    for (code, cabinet_name), cabinet_rows in by_cabinet.items():
        serials = client.serials(code, cabinet_name, [r.item_id for r in cabinet_rows])
        for row in cabinet_rows:
            current = serials.get(row.item_id)
            if current is None:
                _settle(row, CONFLICT, SERIAL_UNREAD, "dcTrack did not return the item's serial: not written")
                continue
            row.item_serial = current
            if current == row.serial:
                _settle(row, UNCHANGED)
            elif not current or overwrite:
                _settle(row, UPDATE)
            else:
                _settle(row, CONFLICT, SERIAL_DIFFERS, "kept; --overwrite replaces it")

    for code, n in sorted(missing_codes.items()):
        warnings.append(
            f"dcTrack has no location with code {code!r} (from --location or --location-map): "
            f"{count(n, 'device')} with a serial skipped."
        )
    return rows, warnings


# ---- writing -------------------------------------------------------------------------------------------------------


def apply_updates(client: DcTrackClient, rows: list[PlannedDevice], *, batch_size: int = BULK_BATCH) -> list[str]:
    """Write the ``update`` rows' serials through dcTrack's bulk endpoint, ``batch_size`` at a time. Marks each row
    ``applied`` (or its ``error``) and returns one error line per row dcTrack refused."""
    updates = [r for r in rows if r.outcome == UPDATE]
    errors: list[str] = []
    for start in range(0, len(updates), max(1, batch_size)):
        batch = updates[start : start + max(1, batch_size)]
        started = time.monotonic()
        try:
            results = client.bulk_update([(r.item_id, {"tiSerialNumber": r.serial}) for r in batch])
        except DcTrackError as e:  # the whole batch: every row in it failed for the same reason
            results = [RowResult(r.item_id, False, e.status, str(e)) for r in batch]
        for row, result in zip(batch, results, strict=True):
            row.applied = result.ok
            if result.ok:
                continue
            row.error = result.message or (f"HTTP {result.status}" if result.status else "no reason given")
            errors.append(row.line())
        log.info(
            "Wrote %s of %s in %s",
            count(sum(1 for r in batch if r.applied), "serial"),
            count(len(batch), "update"),
            seconds(time.monotonic() - started),
        )
    return errors


def safe_cell(text: str) -> str:
    """A CSV cell a spreadsheet will not run as a formula: one starting with ``=``, ``+``, ``-``, ``@`` (or a tab
    or carriage return) gets a leading ``'``."""
    return "'" + text if text.startswith(_FORMULA_START) else text


def import_rows(rows: list[PlannedDevice]) -> list[list[str]]:
    """The File Import rows for the ``update`` devices: ``EDIT``, the item's object, location, name and serial."""
    return [
        [safe_cell(v) for v in ("EDIT", import_object(r.item_class), r.location, r.item_name, r.serial)]
        for r in rows
        if r.outcome == UPDATE
    ]


def write_import_file(path: str, rows: list[PlannedDevice]) -> int:
    """Write the File Import CSV (UTF-8) for the ``update`` rows; returns how many rows it holds."""
    body = import_rows(rows)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(IMPORT_COLUMNS)
        writer.writerows(body)
    return len(body)


# ---- the run -------------------------------------------------------------------------------------------------------


def sync_serials_to_dctrack(
    document: dict,
    client: DcTrackClient,
    *,
    location: str = "",
    location_map: dict[str, str] | None = None,
    match_u: str = MATCH_BOTTOM,
    overwrite: bool = False,
    dry_run: bool = True,
    import_file: str | None = None,
    batch_size: int = BULK_BATCH,
) -> SerialSyncResult:
    """Match the document's devices to dcTrack items and plan their serials; write the File Import CSV with
    ``import_file``; unless ``dry_run``, write the updates over the REST API.

    Raises ``SunbirdDocumentError`` for a document it can't read and ``DcTrackError`` when dcTrack can't be read. An
    update dcTrack refuses is in ``SerialSyncResult.errors``."""
    doc = parse_document(document)
    with_serial = sum(1 for d in doc.devices if d.serial)
    log.info(
        "Matching %s (%s with a serial) in %s against dcTrack %s%s…",
        count(len(doc.devices), "device"),
        f"{with_serial:,}",
        count(len(doc.cabinets), "cabinet"),
        client.url,
        "" if not dry_run else " as a dry run",
    )
    started = time.monotonic()
    rows, warnings = plan_serials(
        doc, client, location=location, location_map=location_map, match_u=match_u, overwrite=overwrite
    )
    result = SerialSyncResult(
        dry_run=dry_run,
        project_id=doc.project_id,
        project_name=doc.project_name,
        dctrack_url=client.url,
        match_u=match_u,
        overwrite=overwrite,
        change_request_id=doc.change_request_id,
        version_id=doc.version_id,
        devices=rows,
        skipped=[f"{s.cabinet} {s.name}: {s.reason}".strip() for s in doc.skipped],
        warnings=[*doc.warnings, *warnings],
    )
    counts = result.counts
    log.info(
        "Matched in %s: %s",
        seconds(time.monotonic() - started),
        ", ".join(f"{n} {outcome}" for outcome, n in counts.items() if n) or "no devices",
    )
    if import_file:
        result.import_rows = write_import_file(import_file, rows)
        result.import_file = import_file
        escaped = sum(1 for r in result.of(UPDATE) if r.serial.startswith(_FORMULA_START))
        if escaped:
            result.warnings.append(
                f"{count(escaped, 'serial')} in {import_file} start with =, +, - or @ and carry a leading ' so a "
                "spreadsheet does not run them as formulas: remove it in dcTrack if the import keeps it."
            )
        log.info("Wrote %s to %s", count(result.import_rows, "import row"), import_file)
    if not dry_run and result.of(UPDATE):
        log.info("Writing %s to dcTrack…", count(len(result.of(UPDATE)), "serial"))
        result.errors = apply_updates(client, rows, batch_size=batch_size)
        result.updated = sum(1 for r in rows if r.applied)
    return result

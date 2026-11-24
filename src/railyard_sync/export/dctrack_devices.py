"""Railyard's device placement -> Sunbird dcTrack (``export sunbird --devices``): the serials export, plus creating
the devices dcTrack does not have and moving the Planned ones it has elsewhere. Nothing is ever deleted, renamed or
moved out of an Installed state.

For each device in the ``sunbird-sync`` document, racked (``devices``) and side-mounted (``zeroUDevices``):

1. **Location and cabinet** as the serials export finds them (:mod:`.dctrack_serials`). A cabinet dcTrack does not
   have is created (Planned) with ``cabinet_model`` (``--cabinet-model``), each one taking a dcTrack cabinet licence;
   without it the cabinet's devices are ``cabinet-not-found``. Locations are never created.
2. **The item at the position** (cabinet, bottom U and rails, as the serials export matches). Found: the serial rules
   apply, and a name that differs from Railyard's is reported, never changed. A 0U device has no U to match (dcTrack's
   cabinet list gives no side), so it is looked for by name only.
3. **Else the item by name**: the item in the location named as the Railyard device (upper-cased). In the same
   cabinet (0U): matched. Elsewhere and Planned: a ``move``. Elsewhere and anything else: a conflict
   (``installed-item-elsewhere``), never moved.
4. **Else a create** (Planned), once the target units are known to be free (``position-occupied`` otherwise) and the
   name is free in the location (``name-taken`` otherwise), on a model from ``model_map`` (``--model-map``), else the
   Models Library entry with Railyard's make and model, else a placeholder (``Railyard`` / ``Generic 2U Device``,
   created once and reused; ``placeholders=False`` makes it ``no-model`` instead).

Items in a matched dcTrack cabinet that no Railyard device accounts for are listed as ``only-in-dctrack``, untouched.
Writes (``dry_run=False``), in order: makes, placeholder models, cabinets (then re-read), item creates (bulk POST),
moves (bulk PUT), serial updates (bulk PUT). A refused write is reported with dcTrack's words; the rest carry on.
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..log import count, seconds
from .dctrack_rest import (
    BULK_BATCH,
    CABINET_CLASS,
    PLANNED,
    RAILS_FOR_FACE,
    DcCabinet,
    DcItem,
    DcModel,
    DcTrackClient,
    DcTrackError,
    Location,
    RowResult,
    is_planned,
    kind_class,
    placeholder_model,
    zero_u_fields,
)
from .dctrack_serials import (
    AMBIGUOUS,
    CABINET_NOT_FOUND,
    CONFLICT,
    CREATE,
    DEVICE_OUTCOMES,
    FACE_RAILS,
    HEIGHT_MISMATCH,
    INSTALLED_ELSEWHERE,
    LOCATION_NOT_FOUND,
    MATCH_BOTTOM,
    MATCH_U,
    MOVE,
    NAME_TAKEN,
    NO_MODEL,
    NO_POSITION,
    NO_SERIAL,
    POSITION_OCCUPIED,
    SAME_ITEM,
    SERIAL_DIFFERS,
    SERIAL_UNREAD,
    SEVERAL_CABINETS,
    SEVERAL_ITEMS,
    UNCHANGED,
    UNMAPPED,
    UPDATE,
    PlannedDevice,
    SerialSyncResult,
    _Reads,
    apply_updates,
    resolve_location,
)
from .sunbird_document import Cabinet, Device, SunbirdDocument, parse_document

log = logging.getLogger(__name__)

DEFAULT_PLACEHOLDER_MAKE = "Railyard"
MODEL_MAP_COLUMNS = ("railyard_make", "railyard_model", "dctrack_make", "dctrack_model")
FROM_MAP, FROM_LIBRARY, FROM_PLACEHOLDER = "map", "library", "placeholder"


class PlacementRefused(ValueError):
    """The placement export can't start (a ``--cabinet-model`` dcTrack does not have, an unreadable model map)."""


@dataclass
class PlannedPlacement(PlannedDevice):
    """A device and what ``--devices`` does with it: its serial (``outcome``, as the serials export) and its
    placement: ``create`` (``outcome`` is then ``create`` too) or ``move`` (the serial rules then apply as well)."""

    side: str = ""  # left | right for a 0U device
    kind: str = ""
    placement: str = ""  # "" | create | move
    found_by: str = ""  # position | name
    name_differs: bool = False  # dcTrack names the item otherwise (reported, never renamed)
    item_status: str = ""
    target_cabinet: str = ""  # the dcTrack cabinet the device belongs in
    new_cabinet: bool = False  # ... which this export creates
    rails: str = ""  # the radioRailsUsed it takes (racked)
    height: int = 0
    create_name: str = ""
    from_cabinet: str = ""  # where a moved item is now
    from_u: int | None = None
    model_make: str = ""
    model_name: str = ""
    model_source: str = ""  # map | library | placeholder
    placed: bool | None = None  # with --apply: whether dcTrack took the create or move

    def where_in_cabinet(self) -> str:
        return f"0U {self.side}" if self.side else super().where_in_cabinet()

    def line(self) -> str:
        head = f"{self.cabinet} {self.where_in_cabinet()} {self.name or '(unnamed)'}"
        if self.placement == CREATE:
            text = f"{head}: create {self.create_name} as {self.model_make} / {self.model_name}"
            text += " (placeholder)" if self.model_source == FROM_PLACEHOLDER else ""
            text += f", serial {self.serial}" if self.serial else ""
            text += f", in new cabinet {self.target_cabinet}" if self.new_cabinet else ""
        elif self.placement == MOVE:
            moved_from = f"{self.from_cabinet} U{self.from_u}" if self.from_u else self.from_cabinet
            text = f"{head} → dcTrack {self.item_name}: move from {moved_from} to {self.target_cabinet}"
            if self.outcome in (UPDATE, CONFLICT):
                text += f", serial {self.item_serial or '(blank)'} → {self.serial}"
            if self.outcome == CONFLICT:
                text += f" [{self.outcome}, {self.reason}: {self.note}]"
        else:
            return super().line()
        if self.error:
            text += f" — dcTrack refused it: {self.error}"
        return text


@dataclass
class PlacementResult(SerialSyncResult):
    """What ``export sunbird --devices`` did (or would do)."""

    makes: list[str] = field(default_factory=list)  # makes to create
    models: list[str] = field(default_factory=list)  # "MAKE / MODEL" placeholders to create
    cabinets: list[str] = field(default_factory=list)  # cabinets to create
    only_in_dctrack: list[str] = field(default_factory=list)  # dcTrack items with no Railyard device, untouched
    created: int = 0  # items created (with --apply)
    moved: int = 0
    cabinets_created: int = 0
    models_created: int = 0

    @property
    def counts(self) -> dict[str, int]:
        counts = {outcome: len(self.of(outcome)) for outcome in DEVICE_OUTCOMES}
        counts[MOVE] = sum(1 for d in self.devices if getattr(d, "placement", "") == MOVE)
        return counts

    @property
    def placeholders(self) -> int:
        """Devices created on placeholder models (to swap later with dcTrack's UPDATE MODEL import)."""
        return sum(1 for d in self.of(CREATE) if getattr(d, "model_source", "") == FROM_PLACEHOLDER)

    def placements(self, kind: str) -> list[PlannedDevice]:
        return [d for d in self.devices if getattr(d, "placement", "") == kind]

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "placeholders": self.placeholders}


# ---- inputs ----------------------------------------------------------------------------------------------------


def read_model_map(path: str) -> dict[tuple[str, str], tuple[str, str]]:
    """``--model-map``: a CSV with the columns ``railyard_make,railyard_model,dctrack_make,dctrack_model``, as
    ``{(railyard make, model) casefolded: (dcTrack make, model)}``. Raises ``PlacementRefused`` for a bad file."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            header = [(h or "").strip().lower() for h in reader.fieldnames or []]
            missing = [c for c in MODEL_MAP_COLUMNS if c not in header]
            if missing:
                raise PlacementRefused(
                    f"the model map {path} must have the columns {','.join(MODEL_MAP_COLUMNS)} (missing "
                    f"{', '.join(missing)})"
                )
            out: dict[tuple[str, str], tuple[str, str]] = {}
            for n, raw in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
                values = [row.get(c, "") for c in MODEL_MAP_COLUMNS]
                if not any(values):
                    continue
                if not all(values):
                    raise PlacementRefused(f"the model map {path}, line {n}: every column needs a value")
                out[(values[0].casefold(), values[1].casefold())] = (values[2], values[3])
            return out
    except OSError as e:
        raise PlacementRefused(f"cannot read the model map {path}: {e.strerror or e}") from None


def parse_cabinet_model(value: str) -> tuple[str, str]:
    """``--cabinet-model "MAKE/MODEL"`` -> ``(make, model)`` (split at the first ``/``)."""
    make, sep, model = (value or "").partition("/")
    if not sep or not make.strip() or not model.strip():
        raise ValueError(f"--cabinet-model {value!r} must be 'MAKE/MODEL', a cabinet model in dcTrack's Models Library")
    return make.strip(), model.strip()


# ---- dcTrack reads ---------------------------------------------------------------------------------------------


class _PlacementReads(_Reads):
    """The serials export's reads, plus each location's items by name, models and makes; each read once."""

    def __init__(self, client: DcTrackClient) -> None:
        super().__init__(client)
        self._named: dict[str, dict[str, list[DcItem]]] = {}
        self._models: dict[tuple[str, str], DcModel | None] = {}
        self._makes: set[str] | None = None

    def named(self, location: Location) -> dict[str, list[DcItem]]:
        """The location's items by upper-cased name."""
        if location.code not in self._named:
            by_name: dict[str, list[DcItem]] = {}
            for item in self.client.location_items(location.code):
                by_name.setdefault(item.name.upper(), []).append(item)
            self._named[location.code] = by_name
        return self._named[location.code]

    def model(self, make: str, model: str) -> DcModel | None:
        key = (make.strip().casefold(), model.strip().casefold())
        if key not in self._models:
            self._models[key] = self.client.find_model(make, model)
        return self._models[key]

    def has_make(self, make: str) -> bool:
        if self._makes is None:
            self._makes = {n.casefold() for name, aliases in self.client.makes() for n in (name, *aliases)}
        return make.strip().casefold() in self._makes

    def forget_cabinets(self, location: str) -> None:
        self._cabinets.pop(location, None)


# ---- planning --------------------------------------------------------------------------------------------------


def _set(row: PlannedPlacement, outcome: str, reason: str = "", note: str = "") -> None:
    row.outcome, row.reason, row.note = outcome, reason, note


def _rails_overlap(a: str, b: str) -> bool:
    return a == "Both" or b == "Both" or a == b


@dataclass
class _Plan:
    rows: list[PlannedPlacement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    makes: dict[str, str] = field(default_factory=dict)  # casefolded -> make to create
    models: dict[tuple[str, str], dict] = field(default_factory=dict)  # placeholder models to create
    cabinets: dict[tuple[str, str], dict] = field(default_factory=dict)  # (location, name) -> cabinet to create
    only_in_dctrack: list[str] = field(default_factory=list)


class _Planner:
    def __init__(
        self,
        client: DcTrackClient,
        *,
        location: str,
        location_map: dict[str, str],
        match_u: str,
        overwrite: bool,
        cabinet_model: tuple[str, str] | None,
        model_map: dict[tuple[str, str], tuple[str, str]],
        placeholder_make: str,
        placeholders: bool,
    ) -> None:
        if match_u not in MATCH_U:
            raise ValueError(f"match_u must be one of {', '.join(MATCH_U)}")
        self.client = client
        self.reads = _PlacementReads(client)
        self.location, self.location_map, self.match_u, self.overwrite = location, location_map, match_u, overwrite
        self.model_map = model_map
        self.placeholder_make = placeholder_make.strip() or DEFAULT_PLACEHOLDER_MAKE
        self.placeholders = placeholders
        self.plan = _Plan()
        self.cabinet_model: DcModel | None = None
        if cabinet_model:
            found = self.reads.model(*cabinet_model)
            if found is None:
                raise PlacementRefused(
                    f"--cabinet-model {cabinet_model[0]}/{cabinet_model[1]} is not in dcTrack's Models Library: "
                    "name an existing cabinet model (make/model)"
                )
            if found.item_class and found.item_class.split("/", 1)[0].strip().casefold() != CABINET_CLASS.casefold():
                raise PlacementRefused(
                    f"--cabinet-model {found.make}/{found.model} is a {found.item_class} model, not a cabinet model"
                )
            self.cabinet_model = found
        self.claimed: set[str] = set()  # dcTrack item ids a Railyard device accounts for
        self.new_names: set[tuple[str, str]] = set()  # (location, name) of planned creates
        #: occupied units per cabinet: (bottom, top, rails, what, item id)
        self.ranges: dict[tuple[str, str], list[tuple[int, int, str, str, str]]] = {}
        self.matched_cabinets: dict[str, DcCabinet] = {}
        self.missing_codes: dict[str, int] = {}

    # -- the passes --------------------------------------------------------------------------------

    def run(self, doc: SunbirdDocument) -> _Plan:
        pending: list[tuple[PlannedPlacement, Device, Location, DcCabinet | None]] = []
        for device in [*doc.devices, *doc.zero_u_devices]:
            found = self._place(device)
            if found is not None:
                pending.append(found)
        self._same_items()
        for row, device, loc, cabinet in pending:
            if not row.outcome and not row.item_id:
                self._by_name(row, device, loc, cabinet)
        self._serials()
        for cabinet in self.matched_cabinets.values():
            for item in self.reads.items(cabinet):
                if item.id not in self.claimed:
                    where = f"U{item.u_position}" if item.u_position else "0U"
                    what = f" ({item.item_class})" if item.item_class else ""
                    self.plan.only_in_dctrack.append(f"{cabinet.name} {where} {item.name or '#' + item.id}{what}")
        for code, n in sorted(self.missing_codes.items()):
            self.plan.warnings.append(
                f"dcTrack has no location with code {code!r} (from --location or --location-map): "
                f"{count(n, 'device')} skipped; locations are never created."
            )
        return self.plan

    def _place(self, device: Device) -> tuple[PlannedPlacement, Device, Location, DcCabinet | None] | None:
        """Location, cabinet and the item at the position; what is left is looked for by name."""
        zero_u = device.zero_u
        u = None if zero_u else device.u_from_bottom if self.match_u == MATCH_BOTTOM else device.u_position
        row = PlannedPlacement(
            outcome="",
            cabinet=device.cabinet_name,
            u=u,
            name=device.name,
            serial=device.serial,
            face=device.face,
            device_id=device.device_id,
            where=device.cabinet.where,
            side=device.side,
            kind=device.kind,
            rails="" if zero_u else RAILS_FOR_FACE[device.face],
            height=0 if zero_u else max(device.u_height or 1, 1),
        )
        self.plan.rows.append(row)
        code = resolve_location(device.cabinet, self.location_map, self.location)
        if not code:
            where = f" ({device.cabinet.where})" if device.cabinet.where else ""
            _set(row, UNMAPPED, note=f"no --location-map entry for its spaces{where}, and no --location")
            return None
        loc = self.reads.location(code)
        if loc is None:
            self.missing_codes[code] = self.missing_codes.get(code, 0) + 1
            _set(row, LOCATION_NOT_FOUND, note=f"dcTrack has no location with code {code!r}")
            return None
        row.location = loc.code
        found = self.reads.cabinets(loc, device.cabinet_name)
        if len(found) > 1:
            _set(row, AMBIGUOUS, SEVERAL_CABINETS, f"{len(found)} cabinets of that name in {loc.code}")
            return None
        cabinet: DcCabinet | None = None
        if found:
            cabinet = found[0]
            row.target_cabinet = cabinet.name
            self.matched_cabinets[cabinet.id] = cabinet
        elif self.cabinet_model is None:
            note = (
                f"no cabinet named {device.cabinet_name!r} in dcTrack location {loc.code} (--cabinet-model creates it)"
            )
            _set(row, CABINET_NOT_FOUND, note=note)
            return None
        else:
            row.target_cabinet, row.new_cabinet = self._new_cabinet(device.cabinet, loc), True
        if not zero_u and (u is None or u <= 0):
            numbering = "from the bottom" if self.match_u == MATCH_BOTTOM else "in the rack's own numbering"
            _set(row, NO_POSITION, note=f"the document gives no U {numbering}")
            return None
        if cabinet is not None and not zero_u:
            rails = FACE_RAILS[device.face]
            candidates = [i for i in self.reads.items(cabinet) if i.u_position == u and i.rails in rails]
            if len(candidates) > 1:
                self.claimed.update(i.id for i in candidates)  # accounted for, if not resolved: not only-in-dctrack
                names = ", ".join(sorted(i.name or "#" + i.id for i in candidates))
                _set(row, AMBIGUOUS, SEVERAL_ITEMS, f"{len(candidates)} dcTrack items at U{u} ({device.face}): {names}")
            elif candidates:
                self._matched(row, device, candidates[0], "position")
        return row, device, loc, cabinet

    def _new_cabinet(self, cab: Cabinet, loc: Location) -> str:
        name = cab.name.strip().upper()
        key = (loc.code, name)
        if key not in self.plan.cabinets:
            assert self.cabinet_model is not None
            body: dict[str, Any] = {
                "tiName": name,
                "cmbLocation": loc.code,
                "cmbMake": self.cabinet_model.make,
                "cmbModel": self.cabinet_model.model,
                "cmbStatus": PLANNED,
            }
            if cab.row:
                body["cmbRowLabel"] = cab.row
                if cab.row_position > 0:
                    body["cmbRowPosition"] = cab.row_position
            self.plan.cabinets[key] = body
            self.ranges[(loc.code, name.casefold())] = []
        return name

    def _matched(self, row: PlannedPlacement, device: Device, item: DcItem, found_by: str) -> None:
        row.item_id, row.item_name, row.item_class, row.item_status = item.id, item.name, item.item_class, item.status
        row.found_by = found_by
        row.name_differs = bool(device.name) and item.name.upper() != device.name.strip().upper()
        if not device.zero_u and device.u_height and item.height and device.u_height != item.height:
            note = f"{device.u_height}U in Railyard, {item.height}U in dcTrack: not written"
            _set(row, CONFLICT, HEIGHT_MISMATCH, note)

    def _same_items(self) -> None:
        by_item: dict[str, list[PlannedPlacement]] = {}
        for row in self.plan.rows:
            if row.item_id:
                by_item.setdefault(row.item_id, []).append(row)
                self.claimed.add(row.item_id)
        for rows in by_item.values():
            if len(rows) > 1:
                names = ", ".join(sorted(r.name for r in rows))
                for row in rows:
                    _set(row, AMBIGUOUS, SAME_ITEM, f"{len(rows)} Railyard devices match this item: {names}")

    def _occupied(self, row: PlannedPlacement, cabinet: DcCabinet | None, exclude: str = "") -> str:
        """What already holds the units the device needs, or ``""``; reserves them when free."""
        key = (row.location, row.target_cabinet.casefold())
        if key not in self.ranges:
            self.ranges[key] = [
                (i.u_position, i.u_position + (i.height or 1) - 1, i.rails, i.name or "#" + i.id, i.id)
                for i in (self.reads.items(cabinet) if cabinet is not None else [])
                if i.u_position
            ]
        assert row.u is not None
        low, high = row.u, row.u + row.height - 1
        for other_low, other_high, rails, label, item_id in self.ranges[key]:
            if item_id == exclude or not (other_low <= high and low <= other_high and _rails_overlap(rails, row.rails)):
                continue
            span = f"U{other_low}" if other_low == other_high else f"U{other_low}–U{other_high}"
            return f"{label} ({span}, {rails})"
        self.ranges[key].append((low, high, row.rails, row.create_name or row.item_name or row.name, exclude))
        return ""

    def _by_name(self, row: PlannedPlacement, device: Device, loc: Location, cabinet: DcCabinet | None) -> None:
        key = device.name.strip().upper()
        hits = self.reads.named(loc).get(key, []) if key else []
        if hits:
            item = hits[0]
            is_cabinet = item.item_class.casefold() == CABINET_CLASS.casefold()
            if is_cabinet or item.id in self.claimed:
                what = "a cabinet" if is_cabinet else "matched to another device"
                _set(row, CONFLICT, NAME_TAKEN, f"dcTrack's {item.name} in {loc.code} is {what}")
                return
            self.claimed.add(item.id)
            self._matched(row, device, item, "name")
            if row.outcome:
                return
            here = item.cabinet.casefold() == row.target_cabinet.casefold()
            if here and (device.zero_u or (item.u_position == row.u and item.rails in FACE_RAILS[device.face])):
                return  # in place: only the serial rules apply
            row.from_cabinet, row.from_u = item.cabinet, item.u_position or None
            if not is_planned(item.status):
                where = f"{item.cabinet} U{item.u_position}" if item.u_position else item.cabinet or "no cabinet"
                note = f"{item.status or 'not Planned'} in {where}: never moved"
                _set(row, CONFLICT, INSTALLED_ELSEWHERE, note)
                return
            if not device.zero_u:
                holder = self._occupied(row, cabinet, exclude=item.id)
                if holder:
                    _set(row, CONFLICT, POSITION_OCCUPIED, f"U{row.u} is taken by {holder}")
                    return
            row.placement = MOVE
            return

        if key:
            name = key
        elif device.zero_u:
            name = f"{row.target_cabinet}-0U-{row.side.upper()}"
        else:
            name = f"{row.target_cabinet}-U{row.u}"
        row.create_name = name
        if name in self.reads.named(loc) or (loc.code, name) in self.new_names:
            _set(row, CONFLICT, NAME_TAKEN, f"dcTrack location {loc.code} already has an item named {name}")
            return
        if not device.zero_u:
            holder = self._occupied(row, cabinet)
            if holder:
                _set(row, CONFLICT, POSITION_OCCUPIED, f"U{row.u} is taken by {holder}")
                return
        model, source, note = self._model(device, row.height)
        if model is None:
            _set(row, NO_MODEL, note=note)
            return
        row.model_make, row.model_name, row.model_source = model.make, model.model, source
        row.item_class = model.item_class or kind_class(device.kind)
        row.placement = CREATE
        self.new_names.add((loc.code, name))
        _set(row, CREATE)

    def _model(self, device: Device, height: int) -> tuple[DcModel | None, str, str]:
        """The dcTrack model a created device takes: mapped, else the library's, else a placeholder."""
        key = (device.make.strip().casefold(), device.model.strip().casefold())
        if key in self.model_map:
            make, model = self.model_map[key]
            found = self.reads.model(make, model)
            if found is None:
                return None, "", f"--model-map gives {make} / {model}, which is not in dcTrack's Models Library"
            return found, FROM_MAP, ""
        if all(key):
            found = self.reads.model(device.make, device.model)
            if found is not None:
                return found, FROM_LIBRARY, ""
        if not self.placeholders:
            named = f"{device.make} / {device.model}" if all(key) else "no make and model in Railyard"
            return None, "", f"{named}: not in dcTrack's Models Library (--no-placeholders)"
        item_class = kind_class(device.kind)
        name = f"Generic {height}U {item_class}"
        found = self.reads.model(self.placeholder_make, name)
        if found is None:
            model_key = (self.placeholder_make.casefold(), name.casefold())
            if model_key not in self.plan.models:
                self.plan.models[model_key] = placeholder_model(self.placeholder_make, name, item_class, height)
                if not self.reads.has_make(self.placeholder_make):
                    self.plan.makes[self.placeholder_make.casefold()] = self.placeholder_make
            found = DcModel(self.placeholder_make, name, item_class)
        return found, FROM_PLACEHOLDER, ""

    def _serials(self) -> None:
        """The serial rules, for items matched in place or moved: one read per cabinet they are in now."""
        groups: dict[tuple[str, str], list[PlannedPlacement]] = {}
        for row in self.plan.rows:
            if row.item_id and not row.outcome:
                cabinet = row.from_cabinet if row.placement == MOVE else row.target_cabinet
                groups.setdefault((row.location, cabinet), []).append(row)
        for (code, cabinet), rows in groups.items():
            with_serial = [r for r in rows if r.serial]
            serials = self.client.serials(code, cabinet, [r.item_id for r in with_serial]) if with_serial else {}
            for row in rows:
                if not row.serial:
                    row.outcome = NO_SERIAL
                    continue
                current = serials.get(row.item_id)
                if current is None:
                    _set(row, CONFLICT, SERIAL_UNREAD, "dcTrack did not return the item's serial: not written")
                    continue
                row.item_serial = current
                if current == row.serial:
                    _set(row, UNCHANGED)
                elif not current or self.overwrite:
                    _set(row, UPDATE)
                else:
                    _set(row, CONFLICT, SERIAL_DIFFERS, "kept; --overwrite replaces it")


# ---- writing ---------------------------------------------------------------------------------------------------


def _create_body(row: PlannedPlacement) -> dict:
    body: dict[str, Any] = {
        "tiName": row.create_name,
        "cmbLocation": row.location,
        "cmbCabinet": row.target_cabinet,
        "cmbMake": row.model_make,
        "cmbModel": row.model_name,
        "cmbStatus": PLANNED,
    }
    if row.side:
        body.update(zero_u_fields(row.side, row.item_class))
    else:
        body.update({"cmbUPosition": row.u, "radioRailsUsed": row.rails})
    if row.serial:
        body["tiSerialNumber"] = row.serial
    return body


def _move_body(row: PlannedPlacement) -> dict:
    body: dict[str, Any] = {"cmbCabinet": row.target_cabinet}
    if row.side:
        body.update(zero_u_fields(row.side, row.item_class))
    else:
        body.update({"cmbUPosition": row.u, "radioRailsUsed": row.rails})
    return body


def _fail(row: PlannedPlacement, message: str, errors: list[str]) -> None:
    row.placed, row.error = False, message
    errors.append(row.line())


def _batches(rows: list[PlannedPlacement], size: int) -> list[list[PlannedPlacement]]:
    size = max(1, size)
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _bulk(write: Any, batch: list[PlannedPlacement], bodies: list[Any]) -> list[RowResult]:
    try:
        return write(bodies)
    except DcTrackError as e:  # the whole batch: every row in it failed for the same reason
        return [RowResult(r.item_id, False, e.status, str(e)) for r in batch]


def apply_placements(
    client: DcTrackClient, plan: _Plan, result: PlacementResult, reads: _PlacementReads, *, batch_size: int
) -> list[str]:
    """Write the plan in dcTrack's order; returns one error line per refused write."""
    errors: list[str] = []
    failed_makes: set[str] = set()
    for key, make in plan.makes.items():
        try:
            client.create_make(make)
        except DcTrackError as e:
            failed_makes.add(key)
            errors.append(f"make {make}: dcTrack refused it: {e}")
    failed_models: set[tuple[str, str]] = set()
    for key, body in plan.models.items():
        if body["make"].casefold() in failed_makes:
            failed_models.add(key)
            continue
        try:
            client.create_model(body)
            result.models_created += 1
        except DcTrackError as e:
            failed_models.add(key)
            errors.append(f"model {body['make']} / {body['model']}: dcTrack refused it: {e}")
    failed_cabinets: set[tuple[str, str]] = set()
    for key, body in plan.cabinets.items():
        try:
            client.create_item(body)
        except DcTrackError as e:
            failed_cabinets.add(key)
            errors.append(f"cabinet {body['tiName']} in {body['cmbLocation']}: dcTrack refused it: {e}")
    for code in {code for code, _ in plan.cabinets}:  # re-read: the items go into the cabinets dcTrack now lists
        reads.forget_cabinets(code)
    for key in plan.cabinets:
        if key in failed_cabinets:
            continue
        location = reads.location(key[0])
        if location is None or not reads.cabinets(location, key[1]):
            failed_cabinets.add(key)
            errors.append(f"cabinet {key[1]} in {key[0]}: created, but dcTrack does not list it")
        else:
            result.cabinets_created += 1

    def blocked(row: PlannedPlacement) -> str:
        if row.new_cabinet and (row.location, row.target_cabinet) in failed_cabinets:
            return f"not written: cabinet {row.target_cabinet} was not created"
        if row.placement == CREATE and (row.model_make.casefold(), row.model_name.casefold()) in failed_models:
            return f"not created: model {row.model_make} / {row.model_name} was not created"
        return ""

    for kind, write, body_of in (
        (CREATE, client.bulk_create, _create_body),
        (MOVE, client.bulk_update, lambda r: (r.item_id, _move_body(r))),
    ):
        ready = []
        for row in result.devices:
            if getattr(row, "placement", "") != kind or not isinstance(row, PlannedPlacement):
                continue
            reason = blocked(row)
            if reason:
                _fail(row, reason, errors)
            else:
                ready.append(row)
        for batch in _batches(ready, batch_size):
            started = time.monotonic()
            for row, outcome in zip(batch, _bulk(write, batch, [body_of(r) for r in batch]), strict=True):
                if outcome.ok:
                    row.placed = True
                    if kind == CREATE and outcome.new_id:
                        row.item_id = outcome.new_id
                else:
                    _fail(row, outcome.message or (f"HTTP {outcome.status}" if outcome.status else "no reason"), errors)
            done = sum(1 for r in batch if r.placed)
            verb = "Created" if kind == CREATE else "Moved"
            log.info("%s %d of %d in %s", verb, done, len(batch), seconds(time.monotonic() - started))
    serial_rows = [r for r in result.devices if r.outcome == UPDATE]
    if serial_rows:
        errors += apply_updates(client, serial_rows, batch_size=batch_size)
    return errors


# ---- the run ---------------------------------------------------------------------------------------------------


def sync_placements_to_dctrack(
    document: dict,
    client: DcTrackClient,
    *,
    location: str = "",
    location_map: dict[str, str] | None = None,
    match_u: str = MATCH_BOTTOM,
    overwrite: bool = False,
    dry_run: bool = True,
    cabinet_model: tuple[str, str] | None = None,
    model_map: dict[tuple[str, str], tuple[str, str]] | None = None,
    placeholder_make: str = DEFAULT_PLACEHOLDER_MAKE,
    placeholders: bool = True,
    batch_size: int = BULK_BATCH,
) -> PlacementResult:
    """Reconcile the document's devices with dcTrack: create, move (Planned items only) and write serials; unless
    ``dry_run``, write it. Raises ``SunbirdDocumentError`` for a document it can't read, ``PlacementRefused`` for a
    ``cabinet_model`` dcTrack does not have and ``DcTrackError`` when dcTrack can't be read; a write dcTrack refuses is
    in ``PlacementResult.errors``."""
    doc = parse_document(document)
    log.info(
        "Placing %s and %s in %s against dcTrack %s%s…",
        count(len(doc.devices), "racked device"),
        count(len(doc.zero_u_devices), "0U device"),
        count(len(doc.cabinets), "cabinet"),
        client.url,
        " as a dry run" if dry_run else "",
    )
    started = time.monotonic()
    planner = _Planner(
        client,
        location=location,
        location_map=location_map or {},
        match_u=match_u,
        overwrite=overwrite,
        cabinet_model=cabinet_model,
        model_map=model_map or {},
        placeholder_make=placeholder_make,
        placeholders=placeholders,
    )
    plan = planner.run(doc)
    zero_u_ids = {d.device_id for d in doc.zero_u_devices if d.device_id}
    result = PlacementResult(
        dry_run=dry_run,
        project_id=doc.project_id,
        project_name=doc.project_name,
        dctrack_url=client.url,
        match_u=match_u,
        overwrite=overwrite,
        change_request_id=doc.change_request_id,
        version_id=doc.version_id,
        devices=list(plan.rows),
        # the 0U devices the document also lists as skipped are placed here
        skipped=[f"{s.cabinet} {s.name}: {s.reason}".strip() for s in doc.skipped if s.device_id not in zero_u_ids],
        warnings=[*doc.warnings, *plan.warnings],
        makes=list(plan.makes.values()),
        models=[f"{b['make']} / {b['model']}" for b in plan.models.values()],
        cabinets=[
            f"{b['tiName']} in {b['cmbLocation']} as {b['cmbMake']} / {b['cmbModel']}"
            + (f", row {b['cmbRowLabel']}" if b.get("cmbRowLabel") else "")
            + (f" position {b['cmbRowPosition']}" if b.get("cmbRowPosition") else "")
            for b in plan.cabinets.values()
        ],
        only_in_dctrack=plan.only_in_dctrack,
    )
    counts = result.counts
    log.info(
        "Planned in %s: %s",
        seconds(time.monotonic() - started),
        ", ".join(f"{n} {outcome}" for outcome, n in counts.items() if n) or "no devices",
    )
    if not dry_run:
        result.errors = apply_placements(client, plan, result, planner.reads, batch_size=batch_size)
        result.created = sum(1 for r in result.placements(CREATE) if getattr(r, "placed", False))
        result.moved = sum(1 for r in result.placements(MOVE) if getattr(r, "placed", False))
        result.updated = sum(1 for r in result.devices if r.applied)
    return result

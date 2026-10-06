"""Typed-ish access over the Railyard Project JSON document.

The wire format is the contract in ``railyard/schema/project.schema.json`` (Go mirror
``backend/internal/model/model.go``). This wraps the raw ``dict`` with the same lookups the Go export
uses — ``resolve_dc``, ``device_type_by_key`` — plus small helpers for defaults. Field names are the
JSON (camelCase) tags exactly as they appear on the wire.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .export.mappings import DEFAULT_STARTING_U, DEFAULT_U_HEIGHT

# A Project is just the decoded JSON; these aliases document intent at call sites.
Doc = dict[str, Any]
Rack = dict[str, Any]
Placement = dict[str, Any]
DeviceType = dict[str, Any]
DataCentre = dict[str, Any]


class Project:
    """Indexes a decoded Project document and answers the lookups the source adapter needs."""

    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        self._dc_by_id = {d["id"]: d for d in doc.get("dataCentres", []) if "id" in d}
        self._loc_by_id = {loc["id"]: loc for loc in doc.get("locations", []) if "id" in loc}
        self._row_by_id = {r["id"]: r for r in doc.get("rows", []) if "id" in r}
        self._catalogue_by_key = {c["key"]: c for c in doc.get("catalogue", []) if "key" in c}

    # -- metadata -----------------------------------------------------------

    @property
    def id(self) -> str:
        return self.doc.get("id", "")

    @property
    def name(self) -> str:
        return self.doc.get("name", "")

    @property
    def organisation(self) -> str:
        return self.doc.get("organisation", "")

    # -- collections --------------------------------------------------------

    @property
    def racks(self) -> list[Rack]:
        return self.doc.get("racks", []) or []

    @property
    def cables(self) -> list[dict]:
        return self.doc.get("cables", []) or []

    @property
    def power_links(self) -> list[dict]:
        return self.doc.get("powerLinks", []) or []

    def placements(self, rack: Rack) -> list[Placement]:
        return rack.get("placements", []) or []

    def iter_placements(self) -> Iterator[tuple[Rack, Placement]]:
        """(rack, placement) pairs in project order — the deterministic order the exporters use."""
        for rack in self.racks:
            for pl in self.placements(rack):
                yield rack, pl

    # -- lookups (ported from model.go) ------------------------------------

    def device_type_by_key(self, key: str) -> DeviceType | None:
        """Catalogue entry with this exact key, or None. Port of ``DeviceTypeByKey`` (model.go:439)."""
        return self._catalogue_by_key.get(key)

    def resolve_dc(self, rack: Rack) -> DataCentre | None:
        """The effective data centre for a rack: its own ``dcId``, else its row's ``dcId``.
        Port of ``ResolveDC`` (model.go:450-462)."""
        dc_id = rack.get("dcId")
        if dc_id and dc_id in self._dc_by_id:
            return self._dc_by_id[dc_id]
        row_id = rack.get("rowId")
        if row_id:
            row = self._row_by_id.get(row_id)
            if row and row.get("dcId"):
                return self._dc_by_id.get(row["dcId"])
        return None

    # -- per-entity defaults ------------------------------------------------

    @staticmethod
    def rack_u_height(rack: Rack) -> int:
        return int(rack.get("uHeight") or DEFAULT_U_HEIGHT)

    @staticmethod
    def rack_width_mm(rack: Rack) -> int:
        return int(rack.get("widthMm") or 0)

    @staticmethod
    def rack_descending(rack: Rack) -> bool:
        return bool(rack.get("descendingUnits", False))

    @staticmethod
    def starting_unit(rack: Rack) -> int:
        return int(rack.get("startingUnit") or DEFAULT_STARTING_U)

    @staticmethod
    def is_zero_u(placement: Placement) -> bool:
        """Port of ``Placement.IsZeroU`` (model.go:213)."""
        return placement.get("mount") == "zeroU"

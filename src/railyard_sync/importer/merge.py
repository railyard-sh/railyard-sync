"""Re-import: merge a freshly built project into the estate an earlier import created.

:func:`merge` takes the project as Railyard holds it now (``existing``, possibly edited by people in
Railyard since the last import) and the project :func:`railyard_sync.importer.build_project` just built
from the DCIM (``imported``), and returns the document to save plus a :class:`MergeDiff`. It implements
the re-import policy in ``CLAUDE.md``:

- **Identity.** Objects are matched by id (``key`` for catalogue entries and rack types). Ids that start
  with ``<prefix>-`` (``nb-`` for NetBox) were created by an import; anything else was designed in
  Railyard and is never changed by a re-import.
- **Field ownership.** On an object both sides have, each field follows :data:`FIELD_OWNERSHIP`:

  ``owned``     the DCIM's value replaces Railyard's; a field the import leaves out is removed.
  ``if-set``    the DCIM's value replaces Railyard's when the import has one; otherwise Railyard's is kept
                (a rack's power capacity when NetBox has no feeds, a role NetBox does not set).
  ``union``     list fields (tags): Railyard's values in their order, then the DCIM's new ones. A tag
                removed in the DCIM stays until it is removed in Railyard.
  ``derived``   re-derived from the space tree after the merge, as the editor does (``dcId``, ``rowId``,
                a data centre's ``locationId``, a row's ``rackIds``).
  ``railyard``  Railyard's. The import only seeds it on an object that does not have it yet (a
                device's colour, power draw, naming mode, a rack's facing, a space's layout…).

  Any field not listed is ``railyard``. A device's ``label`` is ``owned`` while the device is named
  manually (``namingMode`` absent or ``"manual"``, as the importer creates it) and ``railyard`` once
  someone switches it to automatic naming in Railyard. A space's ``parentId`` and a rack's
  ``containerId`` are ``owned`` with one exception: a Railyard-designed space placed *below* the
  DCIM's parent (racks gathered into a Railyard row inside the imported location, a site filed under a
  Railyard region) is kept, because the DCIM's placement still holds.
- **Placements** (devices) are matched across racks: a device the DCIM moved to another rack moves,
  keeping its Railyard fields. Ports and power inlets are matched by id inside the device, with the
  same rules: Railyard-added ports stay, ports gone from the DCIM are stale.
- **Catalogue and rack types.** Prefixed entries (``nb-dt-…``, ``nb-rt-…``) are the DCIM's and are
  updated. Catalogue copies the import references (keyed by their library slug) are added when the
  estate lacks them and never changed when it has them: an entry with that key may be Railyard's own.
- **Order.** Every collection keeps the existing order; new objects are appended in the import's order,
  so a PUT never reorders a collection (a reorder forces a full replacement on the server).
- **Stale objects.** A prefixed object the import no longer has is kept and listed as stale. With
  ``allow_deletes`` it is removed, together with the imported cables and power links that end on it,
  **unless something designed in Railyard still depends on it**: a Railyard device in a rack gone from
  the DCIM, a Railyard cable or power link ending on a device or port gone from the DCIM, a topology node
  or edge, a meet-me room, a pod pattern naming a device type. Those objects are kept and reported
  (:attr:`MergeDiff.retained`), so a Railyard cable is never broken or silently dropped. Retention
  propagates: a kept device keeps its rack, the rack its space.
- **Reference repair.** When a device moves rack, the ``rackId`` on the cable and power-link ends that
  name it (Railyard's included) is updated to match, and noted. This repairs a reference, it is not a
  design change.
- **Conflicts** the server would refuse are reported in :attr:`MergeDiff.conflicts`, not resolved: a
  Railyard-designed (or stale) device that now overlaps an imported one, and two racks with the same name
  in one space. The CLI does not save while there are conflicts.
- ``meta.railyardSync`` is replaced by the import's; the rest of ``meta`` and every collection the
  importer does not write (topologies, pod patterns, naming, meet-me rooms…) are kept as they are.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

OWNED = "owned"
IF_SET = "if-set"
UNION = "union"
DERIVED = "derived"
RAILYARD = "railyard"

# Per entity, how each field of an object both sides have is merged (see the module docstring). Fields
# not listed are RAILYARD. "id"/"key" are the identity and never change.
FIELD_OWNERSHIP: dict[str, dict[str, str]] = {
    "containers": {
        "name": OWNED,
        "parentId": OWNED,  # except a Railyard space below the DCIM's parent, which is kept
        "status": OWNED,
        "facility": OWNED,
        "type": RAILYARD,
        "layout": RAILYARD,
        "exportSite": RAILYARD,
        "rackOrder": RAILYARD,
    },
    "locations": {"name": DERIVED, "facility": DERIVED, "status": DERIVED},
    "dataCentres": {
        "name": DERIVED,
        "status": DERIVED,
        "locationId": DERIVED,
        "naming": RAILYARD,
        "aisleMm": RAILYARD,
        "rackGapMm": RAILYARD,
        "trayHeightMm": RAILYARD,
    },
    "rows": {"name": DERIVED, "dcId": DERIVED, "rackIds": DERIVED},
    "rackTypes": {
        "manufacturer": OWNED,
        "model": OWNED,
        "uHeight": OWNED,
        "widthMm": OWNED,
        "depthMm": OWNED,
        "formFactor": OWNED,
    },
    "racks": {
        "name": OWNED,
        "uHeight": OWNED,
        "startingUnit": OWNED,
        "descendingUnits": OWNED,
        "containerId": OWNED,  # except a Railyard space below the DCIM's location, which is kept
        "rackTypeKey": OWNED,
        "status": OWNED,
        "widthMm": IF_SET,
        "depthMm": IF_SET,
        "maxLoadKg": IF_SET,
        "powerCapacityW": IF_SET,
        "role": IF_SET,
        "tags": UNION,
        "dcId": DERIVED,
        "rowId": DERIVED,
        "notes": RAILYARD,  # the DCIM's comments seed it; Railyard's notes are never overwritten
        "indexInRow": RAILYARD,
        "facing": RAILYARD,
        "kind": RAILYARD,
    },
    "placements": {
        "startU": OWNED,
        "heightU": OWNED,
        "face": OWNED,
        "mount": OWNED,
        "deviceTypeRef": OWNED,
        "serial": OWNED,
        "label": OWNED,  # while named manually; RAILYARD once Railyard names it
        "ports": OWNED,  # merged port by port (FIELD_OWNERSHIP["ports"])
        "powerInlets": OWNED,  # merged inlet by inlet (FIELD_OWNERSHIP["powerInlets"])
        "role": IF_SET,
        "tags": UNION,
        "side": RAILYARD,  # 0U side: the DCIM has none, the importer only alternates it
        "notes": RAILYARD,
        "namingMode": RAILYARD,
        "nameSequence": RAILYARD,
        "powerW": RAILYARD,
        "colour": RAILYARD,
    },
    "ports": {
        "name": OWNED,
        "side": OWNED,
        "kind": OWNED,
        "origin": OWNED,
        "connector": OWNED,
        "module": OWNED,
        "templateIndex": OWNED,
        "peerId": OWNED,
        "peerPosition": OWNED,
        "optic": RAILYARD,
    },
    "powerInlets": {"name": OWNED, "connector": OWNED, "origin": OWNED},
    "catalogue": {
        "manufacturer": OWNED,
        "model": OWNED,
        "uHeight": OWNED,
        "fullDepth": OWNED,
        "partNumber": OWNED,
        "weightKg": OWNED,
        "airflow": OWNED,
        "ports": OWNED,
        "powerInlets": OWNED,
        "outlets": OWNED,
        "source": OWNED,
        "powerW": IF_SET,
    },
    "cables": {"a": OWNED, "b": OWNED, "media": OWNED, "label": OWNED, "colour": IF_SET, "kind": RAILYARD},
    "powerLinks": {"device": OWNED, "pdu": OWNED},
}

# The kinds a diff reports, in display order, with their labels.
KINDS: dict[str, str] = {
    "containers": "spaces",
    "locations": "locations",
    "dataCentres": "data centres",
    "rows": "rows",
    "rackTypes": "rack types",
    "racks": "racks",
    "placements": "devices",
    "ports": "ports",
    "powerInlets": "power inlets",
    "catalogue": "device types",
    "cables": "cables",
    "powerLinks": "power links",
}
_SINGULAR = {kind: label.removesuffix("s") for kind, label in KINDS.items()}


@dataclass
class KindDiff:
    """What a merge did to one kind of object. Lists hold ids (keys for catalogue entries and rack
    types), in the merged document's order; only imported objects are listed."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # gone from the DCIM, kept
    removed: list[str] = field(default_factory=list)  # gone from the DCIM, removed (allow_deletes)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "added": list(self.added),
            "updated": list(self.updated),
            "unchanged": list(self.unchanged),
            "stale": list(self.stale),
            "removed": list(self.removed),
        }


@dataclass
class Conflict:
    """Something the server would refuse, which the merge reports rather than resolves."""

    kind: str  # "overlap" / "rack_name"
    message: str
    ids: tuple[str, ...] = ()


@dataclass
class Retained:
    """A stale object ``allow_deletes`` could not remove, because a Railyard object depends on it."""

    kind: str
    id: str
    reason: str


@dataclass
class MergeDiff:
    kinds: dict[str, KindDiff] = field(default_factory=lambda: {k: KindDiff() for k in KINDS})
    conflicts: list[Conflict] = field(default_factory=list)
    retained: list[Retained] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __getitem__(self, kind: str) -> KindDiff:
        return self.kinds[kind]

    @property
    def changed(self) -> bool:
        """Whether the merge changed any imported object (the meta record aside)."""
        return any(d.changed for d in self.kinds.values())

    @property
    def stale_count(self) -> int:
        return sum(len(d.stale) for d in self.kinds.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kinds": {k: d.to_dict() for k, d in self.kinds.items()},
            "conflicts": [{"kind": c.kind, "message": c.message, "ids": list(c.ids)} for c in self.conflicts],
            "retained": [{"kind": r.kind, "id": r.id, "reason": r.reason} for r in self.retained],
            "notes": list(self.notes),
        }

    def summary(self, *, list_limit: int = 10) -> str:
        """A short human-readable account: counts per kind, then stale and removed ids, objects kept
        for Railyard's sake, conflicts and notes."""
        lines: list[str] = []
        for kind, label in KINDS.items():
            d = self.kinds[kind]
            parts = [
                f"{len(ids)} {word}"
                for word, ids in (
                    ("added", d.added),
                    ("updated", d.updated),
                    ("stale", d.stale),
                    ("removed", d.removed),
                    ("unchanged", d.unchanged),
                )
                if ids
            ]
            if parts:
                lines.append(f"  {label}: {', '.join(parts)}")
        if not lines:
            lines.append("  nothing imported")
        out = ["Re-import changes:", *lines]
        for title, attr in (("Stale (gone from the source, kept)", "stale"), ("Removed", "removed")):
            listed = [(KINDS[k], getattr(d, attr)) for k, d in self.kinds.items() if getattr(d, attr)]
            if listed:
                out.append(f"{title}:")
                for label, ids in listed:
                    shown = ", ".join(ids[:list_limit]) + (
                        f" and {len(ids) - list_limit} more" if len(ids) > list_limit else ""
                    )
                    out.append(f"  {label}: {shown}")
        if self.retained:
            out.append("Kept although gone from the source (Railyard objects depend on them):")
            out.extend(f"  {_SINGULAR.get(r.kind, r.kind)} {r.id}: {r.reason}" for r in self.retained)
        if self.conflicts:
            out.append("Conflicts (resolve them in Railyard before saving):")
            out.extend(f"  - {c.message}" for c in self.conflicts)
        if self.notes:
            out.append("Notes:")
            out.extend(f"  - {n}" for n in self.notes)
        return "\n".join(out)


@dataclass
class MergeResult:
    project: dict[str, Any]
    diff: MergeDiff


def merge(
    existing: dict[str, Any], imported: dict[str, Any], *, prefix: str = "nb", allow_deletes: bool = False
) -> MergeResult:
    """Merge ``imported`` (a freshly built project) into ``existing`` (the estate as Railyard holds it).
    Neither argument is modified. See the module docstring for the policy."""
    return _Merger(existing, imported, prefix, allow_deletes).run()


# ---- field-level merging ----------------------------------------------------


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _union(existing: list, imported: list) -> list:
    out = list(existing)
    out.extend(v for v in imported if v not in out)
    return out


def _merge_fields(existing: dict, imported: dict, rules: dict[str, str], *, skip: Iterable[str] = ()) -> dict:
    """``existing`` with ``imported``'s fields applied by ``rules``. Keys keep existing's order; keys new to
    the object are appended in imported's order. Values taken from ``imported`` are deep copies."""
    out = dict(existing)
    skipped = set(skip) | {"id", "key"}
    for key in list(existing) + [k for k in imported if k not in existing]:
        if key in skipped:
            continue
        rule = rules.get(key, RAILYARD)
        present = key in imported
        if rule == OWNED:
            if present:
                out[key] = copy.deepcopy(imported[key])
            else:
                out.pop(key, None)
        elif rule == IF_SET:
            if present and not _empty(imported[key]):
                out[key] = copy.deepcopy(imported[key])
        elif rule == UNION:
            if present:
                out[key] = _union(existing.get(key) or [], imported[key] or [])
        elif rule == DERIVED:
            if present:
                out[key] = copy.deepcopy(imported[key])  # provisional: re-derived after the merge
        elif key not in existing and present:  # RAILYARD: seeded on first sight only
            out[key] = copy.deepcopy(imported[key])
    return out


def _merge_components(existing: list | None, imported: list | None, rules: dict[str, str]) -> list | None:
    """Merge a device's ports (or power inlets) by id: existing order, new ones appended. Components
    only Railyard has (its own, or stale ones) stay here; stale ones may be removed later."""
    if imported is None:
        return existing
    if existing is None:
        return copy.deepcopy(imported)
    by_id = {c.get("id"): c for c in imported}
    out = [_merge_fields(c, by_id[c.get("id")], rules) if c.get("id") in by_id else c for c in existing]
    seen = {c.get("id") for c in existing}
    out.extend(copy.deepcopy(c) for c in imported if c.get("id") not in seen)
    return out


# ---- the merge ----------------------------------------------------------------


Ref = tuple[str, str]  # (kind, id)


class _Merger:
    def __init__(self, existing: dict, imported: dict, prefix: str, allow_deletes: bool) -> None:
        self.existing = existing
        self.imported = imported
        self.marker = f"{prefix}-"
        self.allow_deletes = allow_deletes
        self.doc: dict[str, Any] = copy.deepcopy(existing)
        self.diff = MergeDiff()
        self.imported_parent: dict[Any, Any] = {}

    def ours(self, ident: Any) -> bool:
        return isinstance(ident, str) and ident.startswith(self.marker)

    def run(self) -> MergeResult:
        if self.doc.get("containers") is None and self.imported.get("containers") is not None:
            _ensure_containers(self.doc)
        before = copy.deepcopy(self.doc)  # existing, migrated to containers when needed: the diff's baseline

        self._merge_containers()
        for kind, key in (("rackTypes", "key"), ("catalogue", "key")):
            self._merge_keyed(kind, key)
        for kind in ("locations", "dataCentres", "rows"):
            self._merge_simple(kind)
        self._merge_racks()
        self._keep_railyard_nesting()
        for kind in ("cables", "powerLinks"):
            self._merge_simple(kind)
        if self.allow_deletes:
            self._remove_stale()
        self._repair_end_racks()
        if self.doc.get("containers") is not None:
            _project_container_layout(self.doc)
        self._merge_meta()
        self._compute_diff(before)
        self._find_conflicts()
        return MergeResult(self.doc, self.diff)

    # -- collections ----------------------------------------------------------

    def _merge_list(self, kind: str, existing: list | None, imported: list | None, key: str = "id") -> list | None:
        if existing is None and imported is None:
            return None
        rules = FIELD_OWNERSHIP[kind]
        by_key = {item.get(key): item for item in imported or []}
        out = []
        for item in existing or []:
            k = item.get(key)
            # Only the import's own objects are updated; an unprefixed key both sides have (a catalogue
            # copy) is Railyard's and stays as it is.
            out.append(_merge_fields(item, by_key[k], rules) if k in by_key and self.ours(k) else item)
        seen = {item.get(key) for item in existing or []}
        out.extend(copy.deepcopy(item) for item in imported or [] if item.get(key) not in seen)
        return out

    def _set(self, kind: str, value: list | None) -> None:
        if value is not None:
            self.doc[kind] = value

    def _merge_simple(self, kind: str) -> None:
        self._set(kind, self._merge_list(kind, self.doc.get(kind), self.imported.get(kind)))

    def _merge_keyed(self, kind: str, key: str) -> None:
        self._set(kind, self._merge_list(kind, self.doc.get(kind), self.imported.get(kind), key))

    def _merge_containers(self) -> None:
        self._merge_simple("containers")
        # The import's own parentId, for the nesting rule (applied once racks are merged too).
        self.imported_parent = {c.get("id"): c.get("parentId") for c in self.imported.get("containers") or []}

    def _merge_racks(self) -> None:
        imported_racks = self.imported.get("racks") or []
        if not imported_racks and self.doc.get("racks") is None:
            return
        rack_rules = FIELD_OWNERSHIP["racks"]
        existing_pl = {
            pl.get("id"): (rack.get("id"), pl) for rack, pl in _iter_placements(self.doc) if self.ours(pl.get("id"))
        }
        imported_rack_of = {pl.get("id"): rack.get("id") for rack, pl in _iter_placements(self.imported)}
        merged_pl: dict[str, dict] = {}
        for rack, pl in _iter_placements(self.imported):
            pid = pl.get("id")
            merged_pl[pid] = self._merge_placement(existing_pl[pid][1], pl) if pid in existing_pl else copy.deepcopy(pl)
            if pid in existing_pl and existing_pl[pid][0] != rack.get("id"):
                self.diff.notes.append(
                    f"device {_label(pl)} ({pid}) moved from rack {existing_pl[pid][0]} to {rack.get('id')}"
                )
        imported_by_id = {r.get("id"): r for r in imported_racks}
        placed: set[str] = set()

        def fill(rack_id: str, current: list[dict]) -> list[dict]:
            out = []
            for pl in current:
                pid = pl.get("id")
                if pid in merged_pl:
                    if imported_rack_of[pid] == rack_id:
                        out.append(merged_pl[pid])
                        placed.add(pid)
                    # else: the import puts it in another rack, where it is appended below
                else:
                    out.append(pl)  # Railyard's, or stale
            for pl in imported_by_id.get(rack_id, {}).get("placements") or []:
                if pl.get("id") not in placed:
                    out.append(merged_pl[pl.get("id")])
                    placed.add(pl.get("id"))
            return out

        racks = []
        for rack in self.doc.get("racks") or []:
            rid = rack.get("id")
            if rid in imported_by_id and self.ours(rid):
                merged = _merge_fields(rack, imported_by_id[rid], rack_rules, skip=("placements",))
            else:
                merged = rack
            merged["placements"] = fill(rid, rack.get("placements") or [])
            racks.append(merged)
        seen = {r.get("id") for r in self.doc.get("racks") or []}
        for rack in imported_racks:
            if rack.get("id") in seen:
                continue
            new = {k: copy.deepcopy(v) for k, v in rack.items() if k != "placements"}
            new["placements"] = fill(rack.get("id"), [])
            racks.append(new)
        self.doc["racks"] = racks

    def _merge_placement(self, existing: dict, imported: dict) -> dict:
        rules = dict(FIELD_OWNERSHIP["placements"])
        if existing.get("namingMode") not in (None, "", "manual"):
            rules["label"] = RAILYARD  # Railyard names this device now
        out = _merge_fields(existing, imported, rules, skip=("ports", "powerInlets"))
        for key in ("ports", "powerInlets"):
            merged = _merge_components(existing.get(key), imported.get(key), FIELD_OWNERSHIP[key])
            if merged is not None:
                out[key] = merged
        return out

    def _keep_railyard_nesting(self) -> None:
        """Undo the import's parentId/containerId where Railyard placed the object in a space of its own
        below the DCIM's parent (see the module docstring)."""
        containers = {c.get("id"): c for c in self.doc.get("containers") or []}
        existing_containers = {c.get("id"): c for c in self.existing.get("containers") or []}
        existing_racks = {r.get("id"): r for r in self.existing.get("racks") or []}

        def nearest_ours(start: str | None, avoid: str | None) -> tuple[bool, str | None]:
            """(ok, id): the nearest imported space at or above ``start``; ok is False on a cycle."""
            seen: set[str] = set()
            node = start
            while node and node in containers and not self.ours(node):
                if node in seen:
                    return False, None
                seen.add(node)
                node = containers[node].get("parentId")
            if avoid is not None and node == avoid:
                return False, None
            return True, node if node in containers else None

        def keep(old: str | None, new: str | None, avoid: str | None) -> bool:
            if not old or old == new or self.ours(old) or old not in containers:
                return False
            ok, anchor = nearest_ours(old, avoid)
            return ok and anchor == (new or None)

        for cid, container in containers.items():
            if not self.ours(cid) or cid not in self.imported_parent or cid not in existing_containers:
                continue
            old = existing_containers[cid].get("parentId")
            if keep(old, self.imported_parent[cid], cid):
                container["parentId"] = old
        imported_container = {r.get("id"): r.get("containerId") for r in self.imported.get("racks") or []}
        for rack in self.doc.get("racks") or []:
            rid = rack.get("id")
            if not self.ours(rid) or rid not in imported_container or rid not in existing_racks:
                continue
            old = existing_racks[rid].get("containerId")
            if keep(old, imported_container[rid], None):
                rack["containerId"] = old

    # -- stale objects --------------------------------------------------------

    def _imported_ids(self) -> dict[str, set[str]]:
        imp = self.imported
        ids: dict[str, set[str]] = {
            "containers": {c.get("id") for c in imp.get("containers") or []},
            "racks": {r.get("id") for r in imp.get("racks") or []},
            "placements": {pl.get("id") for _, pl in _iter_placements(imp)},
            "ports": {p.get("id") for _, pl in _iter_placements(imp) for p in pl.get("ports") or []},
            "powerInlets": {p.get("id") for _, pl in _iter_placements(imp) for p in pl.get("powerInlets") or []},
            "rackTypes": {t.get("key") for t in imp.get("rackTypes") or []},
            "catalogue": {t.get("key") for t in imp.get("catalogue") or []},
            "cables": {c.get("id") for c in imp.get("cables") or []},
            "powerLinks": {c.get("id") for c in imp.get("powerLinks") or []},
        }
        for kind in ("locations", "dataCentres", "rows"):
            ids[kind] = {c.get("id") for c in imp.get(kind) or []}
        return ids

    def _objects(self) -> Iterator[tuple[Ref, str, list[tuple[Ref, str]]]]:
        """Every object in the merged document that can depend on another: (ref, description, the refs
        it depends on with why)."""
        doc = self.doc
        for c in doc.get("containers") or []:
            name = f"space {c.get('name')!r}"
            yield ("containers", c.get("id")), name, [(("containers", c.get("parentId")), f"it holds {name}")]
        pod_text = json.dumps(doc.get("podPatterns") or [])
        for rack in doc.get("racks") or []:
            rname = f"rack {rack.get('name')!r}"
            yield (
                ("racks", rack.get("id")),
                rname,
                [
                    (("containers", rack.get("containerId")), f"it holds {rname}"),
                    (("rackTypes", rack.get("rackTypeKey")), f"{rname} uses it"),
                ],
            )
            for pl in rack.get("placements") or []:
                dname = f"device {_label(pl)!r}"
                yield (
                    ("placements", pl.get("id")),
                    dname,
                    [
                        (("racks", rack.get("id")), f"it holds {dname}"),
                        (("catalogue", pl.get("deviceTypeRef")), f"{dname} is of this type"),
                    ],
                )
                for kind in ("ports", "powerInlets"):
                    for comp in pl.get(kind) or []:
                        cname = f"{'port' if kind == 'ports' else 'power inlet'} {comp.get('name')!r} of {dname}"
                        yield (kind, comp.get("id")), cname, [(("placements", pl.get("id")), f"it has {cname}")]
        for cable in doc.get("cables") or []:
            cname = f"cable {cable.get('label') or cable.get('id')!r}"
            deps = []
            for end in (cable.get("a") or {}, cable.get("b") or {}):
                deps.append((("placements", end.get("placementId")), f"{cname} ends on it"))
                deps.append((("ports", end.get("portId")), f"{cname} ends on it"))
            yield ("cables", cable.get("id")), cname, deps
        for link in doc.get("powerLinks") or []:
            lname = f"power link {link.get('id')!r}"
            device, pdu = link.get("device") or {}, link.get("pdu") or {}
            yield (
                ("powerLinks", link.get("id")),
                lname,
                [
                    (("placements", device.get("placementId")), f"{lname} powers it"),
                    (("powerInlets", device.get("inletId")), f"{lname} plugs into it"),
                    (("placements", pdu.get("placementId")), f"{lname} is fed from it"),
                ],
            )
        for topo in doc.get("topologies") or []:
            tname = f"topology {topo.get('name')!r}"
            deps = [(("placements", n.get("placementId")), f"{tname} shows it") for n in topo.get("nodes") or []]
            deps += [(("cables", e.get("cableId")), f"{tname} shows it") for e in topo.get("edges") or []]
            yield ("topologies", topo.get("id")), tname, deps
        for room in doc.get("meetMeRooms") or []:
            mname = f"meet-me room {room.get('name')!r}"
            yield ("meetMeRooms", room.get("id")), mname, [(("containers", room.get("dcId")), f"{mname} is on it")]
        # A pod pattern names device and rack types inside its templates; any mention keeps them.
        for kind in ("catalogue", "rackTypes"):
            for entry in doc.get(kind) or []:
                if self.ours(entry.get("key")) and json.dumps(entry.get("key")) in pod_text:
                    yield ("podPatterns", "*"), "a pod pattern", [((kind, entry.get("key")), "a pod pattern uses it")]

    def _remove_stale(self) -> None:
        imported = self._imported_ids()
        objects = list(self._objects())
        catalogue_keys = {("catalogue", e.get("key")) for e in self.doc.get("catalogue") or []}
        rack_type_keys = {("rackTypes", e.get("key")) for e in self.doc.get("rackTypes") or []}
        candidates: set[Ref] = set()
        followers: set[Ref] = set()
        for ref, _, deps in objects:
            kind, ident = ref
            if kind in imported and self.ours(ident) and ident not in imported[kind]:
                if kind in ("ports", "powerInlets") and deps[0][0][1] not in imported["placements"]:
                    followers.add(ref)  # a stale device's own ports stay with it, or go with it
                    continue
                candidates.add(ref)
        for ref in catalogue_keys | rack_type_keys:
            if self.ours(ref[1]) and ref[1] not in imported[ref[0]]:
                candidates.add(ref)
        # Fixpoint: whatever survives keeps what it depends on.
        reasons: dict[Ref, str] = {}
        changed = True
        while changed:
            changed = False
            for ref, _, deps in objects:
                if ref in candidates or ref in followers:
                    continue
                for dep, why in deps:
                    if dep in candidates:
                        candidates.discard(dep)
                        reasons[dep] = why
                        changed = True
        for (kind, ident), why in reasons.items():
            self.diff.retained.append(Retained(kind, ident, why))
        gone = {kind: {i for k, i in candidates if k == kind} for kind in KINDS}
        doc = self.doc
        if doc.get("containers") is not None:
            doc["containers"] = [c for c in doc["containers"] if c.get("id") not in gone["containers"]]
        for kind in ("rackTypes", "catalogue"):
            if doc.get(kind) is not None:
                doc[kind] = [e for e in doc[kind] if e.get("key") not in gone[kind]]
        for kind in ("cables", "powerLinks"):
            if doc.get(kind) is not None:
                doc[kind] = [e for e in doc[kind] if e.get("id") not in gone[kind]]
        if doc.get("racks") is not None:
            racks = []
            for rack in doc["racks"]:
                if rack.get("id") in gone["racks"]:
                    continue
                kept = []
                for pl in rack.get("placements") or []:
                    if pl.get("id") in gone["placements"]:
                        continue
                    for kind in ("ports", "powerInlets"):
                        if pl.get(kind) is not None:
                            pl[kind] = [c for c in pl[kind] if c.get("id") not in gone[kind]]
                    kept.append(pl)
                rack["placements"] = kept
                racks.append(rack)
            doc["racks"] = racks

    # -- references and meta --------------------------------------------------

    def _repair_end_racks(self) -> None:
        rack_of = {pl.get("id"): rack.get("id") for rack, pl in _iter_placements(self.doc)}
        imported_cables = {c.get("id") for c in self.imported.get("cables") or []}
        imported_links = {c.get("id") for c in self.imported.get("powerLinks") or []}
        for kind, sides, fresh in (
            ("cables", ("a", "b"), imported_cables),
            ("powerLinks", ("device", "pdu"), imported_links),
        ):
            for item in self.doc.get(kind) or []:
                for side in sides:
                    end = item.get(side)
                    if not isinstance(end, dict):
                        continue
                    want = rack_of.get(end.get("placementId"))
                    if want and end.get("rackId") != want:
                        if item.get("id") not in fresh:
                            self.diff.notes.append(
                                f"{KINDS[kind][:-1]} {item.get('id')}: end {side} now names rack {want}, where its "
                                f"device moved"
                            )
                        end["rackId"] = want

    def _merge_meta(self) -> None:
        sync = (self.imported.get("meta") or {}).get("railyardSync")
        if sync is None:
            return
        meta = dict(self.doc.get("meta") or {})
        meta["railyardSync"] = copy.deepcopy(sync)
        self.doc["meta"] = meta

    # -- diff and conflicts ---------------------------------------------------

    def _compute_diff(self, before: dict) -> None:
        imported = self._imported_ids()

        def index(doc: dict) -> dict[str, dict[str, Any]]:
            out: dict[str, dict[str, Any]] = {k: {} for k in KINDS}
            for kind in ("containers", "locations", "dataCentres", "rows", "cables", "powerLinks"):
                for item in doc.get(kind) or []:
                    out[kind][item.get("id")] = item
            for kind in ("rackTypes", "catalogue"):
                for item in doc.get(kind) or []:
                    out[kind][item.get("key")] = item
            for rack in doc.get("racks") or []:
                out["racks"][rack.get("id")] = {k: v for k, v in rack.items() if k != "placements"}
                for pl in rack.get("placements") or []:
                    out["placements"][pl.get("id")] = (rack.get("id"), pl)
                    for kind in ("ports", "powerInlets"):
                        for comp in pl.get(kind) or []:
                            out[kind][comp.get("id")] = comp
            return out

        old, new = index(before), index(self.doc)
        for kind in KINDS:
            d = self.diff.kinds[kind]
            for ident, item in new[kind].items():
                if ident not in old[kind]:
                    if self.ours(ident) or ident in imported[kind]:
                        d.added.append(ident)
                elif not self.ours(ident):
                    continue
                elif ident not in imported[kind]:
                    d.stale.append(ident)
                elif item != old[kind][ident]:
                    d.updated.append(ident)
                else:
                    d.unchanged.append(ident)
            d.removed.extend(i for i in old[kind] if i not in new[kind] and self.ours(i))

    def _find_conflicts(self) -> None:
        imported_pl = {pl.get("id") for _, pl in _iter_placements(self.imported)}
        imported_racks = {r.get("id") for r in self.imported.get("racks") or []}

        def origin(ident: str, current: set[str]) -> str:
            if ident in current:
                return "imported"
            return "stale imported" if self.ours(ident) else "Railyard-designed"

        for rack in self.doc.get("racks") or []:
            placed = [pl for pl in rack.get("placements") or [] if _u_interval(pl)]
            for i, a in enumerate(placed):
                for b in placed[i + 1 :]:
                    # Only a clash between an imported device and one the import does not have is the
                    # merge's to report: two imported ones are the DCIM's layout (the builder reports
                    # those), two others were already there before this import.
                    if (a.get("id") in imported_pl) == (b.get("id") in imported_pl):
                        continue
                    lo_a, hi_a = _u_interval(a)
                    lo_b, hi_b = _u_interval(b)
                    if hi_a < lo_b or hi_b < lo_a or not _faces_collide(a.get("face"), b.get("face")):
                        continue
                    lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
                    units = f"U{lo}" if lo == hi else f"U{lo}-U{hi}"
                    self.diff.conflicts.append(
                        Conflict(
                            "overlap",
                            f"rack {rack.get('name')!r}: {origin(a.get('id'), imported_pl)} device {_label(a)!r} "
                            f"overlaps {origin(b.get('id'), imported_pl)} device {_label(b)!r} at {units}",
                            (a.get("id"), b.get("id")),
                        )
                    )
        # Railyard names devices uniquely across an estate, ignoring case and surrounding spaces, and refuses
        # a save that would give an imported device a name another device already has. The builder keeps
        # imported names apart; a clash with a device designed in Railyard, or one kept from an earlier
        # import, is the merge's to report.
        by_name: dict[str, list[dict]] = {}
        for _, pl in _iter_placements(self.doc):
            label = str(pl.get("label") or "").strip()
            if label:
                by_name.setdefault(label.casefold(), []).append(pl)
        for group in by_name.values():
            fresh = [pl for pl in group if pl.get("id") in imported_pl]
            if len(group) < 2 or not fresh or len(fresh) == len(group):
                continue
            others = [pl for pl in group if pl.get("id") not in imported_pl]
            stale = all(self.ours(pl.get("id")) for pl in others)
            hint = (
                "it was deleted or renamed in the DCIM; re-run with --allow-deletes to drop it, "
                "or rename it in Railyard"
                if stale
                else "rename the Railyard device (or the DCIM's) so the names differ"
            )
            self.diff.conflicts.append(
                Conflict(
                    "device_name",
                    f"imported device {_label(fresh[0])!r} ({fresh[0].get('id')}) has the same name as "
                    + ", ".join(f"{origin(pl.get('id'), imported_pl)} device {pl.get('id')}" for pl in others)
                    + f"; Railyard needs device names to be unique in an estate: {hint}",
                    tuple(pl.get("id") for pl in group),
                )
            )
        by_space: dict[tuple[Any, str], list[dict]] = {}
        for rack in self.doc.get("racks") or []:
            by_space.setdefault((rack.get("containerId"), str(rack.get("name", "")).casefold()), []).append(rack)
        for group in by_space.values():
            fresh = [r for r in group if r.get("id") in imported_racks]
            if len(group) < 2 or not fresh or len(fresh) == len(group):
                continue
            self.diff.conflicts.append(
                Conflict(
                    "rack_name",
                    f"racks {', '.join(repr(r.get('id')) for r in group)} share the name {group[0].get('name')!r} "
                    "in one space",
                    tuple(r.get("id") for r in group),
                )
            )


# ---- helpers --------------------------------------------------------------------


def _iter_placements(doc: dict) -> Iterator[tuple[dict, dict]]:
    for rack in doc.get("racks") or []:
        for pl in rack.get("placements") or []:
            yield rack, pl


def _label(pl: dict) -> str:
    return str(pl.get("label") or pl.get("id") or "")


def _u_interval(pl: dict) -> tuple[int, int] | None:
    """The units a placement occupies, or None for a 0U side mount or an empty interval."""
    if pl.get("mount") == "zeroU":
        return None
    try:
        start, height = int(pl.get("startU") or 0), int(pl.get("heightU") or 0)
    except (TypeError, ValueError):
        return None
    if height <= 0:
        return None
    return start, start + height - 1


def _faces_collide(a: Any, b: Any) -> bool:
    """Front and rear never collide; full depth collides with everything (model.laterColliders)."""
    return a == "full" or b == "full" or a == b


def _ensure_containers(doc: dict) -> None:
    """Give a legacy document (locations / data centres / rows, no ``containers``) its space tree, as the
    server's ``EnsureContainers`` does, so imported spaces can join it."""
    location_ids = {loc.get("id") for loc in doc.get("locations") or []}
    dc_ids = {dc.get("id") for dc in doc.get("dataCentres") or []}
    row_ids = {row.get("id") for row in doc.get("rows") or []}
    containers: list[dict] = []
    for loc in doc.get("locations") or []:
        c = {"id": loc.get("id"), "name": loc.get("name", ""), "type": "Location", "layout": "group"}
        _put(c, "status", loc.get("status"))
        _put(c, "facility", loc.get("facility"))
        containers.append(c)
    for dc in doc.get("dataCentres") or []:
        c = {"id": dc.get("id"), "name": dc.get("name", ""), "type": "Data centre", "layout": "floor"}
        _put(c, "parentId", dc.get("locationId") if dc.get("locationId") in location_ids else None)
        _put(c, "status", dc.get("status"))
        c["exportSite"] = True
        containers.append(c)
    for row in doc.get("rows") or []:
        c = {"id": row.get("id"), "name": row.get("name", ""), "type": "Row", "layout": "row"}
        _put(c, "parentId", row.get("dcId") if row.get("dcId") in dc_ids else None)
        containers.append(c)
    doc["containers"] = containers
    ids = {c["id"] for c in containers}
    for rack in doc.get("racks") or []:
        if rack.get("containerId"):
            continue
        parent = rack.get("rowId") if rack.get("rowId") in row_ids else rack.get("dcId")
        _put(rack, "containerId", parent if parent in ids else None)


def _put(obj: dict, key: str, value: Any) -> None:
    """Set an omitempty field: a falsy value removes it, as Go's encoder would leave it out."""
    if value:
        obj[key] = value
    else:
        obj.pop(key, None)


def _project_container_layout(doc: dict) -> None:
    """Re-derive the physical records from the space tree, in place. A port of the server's
    ``ProjectContainerLayout`` (backend/internal/model/container_layout.go), except that records keep the
    document's existing order (new ones appended in tree order) so a save never reorders a collection."""
    containers = doc.get("containers") or []
    by_id = {c.get("id"): c for c in containers}

    def ancestors(ident: Any) -> list[dict]:
        path, seen = [], set()
        node = by_id.get(ident)
        while node is not None and node.get("id") not in seen:
            path.append(node)
            seen.add(node.get("id"))
            node = by_id.get(node.get("parentId"))
        return path

    def ordered(existing: list[dict], derived: dict[Any, dict], tree_order: list[Any]) -> list[dict]:
        out = [derived[r.get("id")] for r in existing if r.get("id") in derived]
        listed = {r.get("id") for r in out}
        out.extend(derived[i] for i in tree_order if i not in listed)
        return out

    locations = []
    location_ids = set()
    for loc in doc.get("locations") or []:
        c = by_id.get(loc.get("id"))
        if c is None:
            continue
        loc["name"] = c.get("name", "")
        _put(loc, "status", c.get("status"))
        _put(loc, "facility", c.get("facility"))
        locations.append(loc)
        location_ids.add(loc.get("id"))
    if doc.get("locations") is not None or locations:
        doc["locations"] = locations

    dc_by_id = {dc.get("id"): dc for dc in doc.get("dataCentres") or []}
    derived_dcs: dict[Any, dict] = {}
    floors = [c.get("id") for c in containers if c.get("layout") == "floor"]
    for cid in floors:
        c = by_id[cid]
        dc = dc_by_id.get(cid, {"id": cid})
        dc["id"], dc["name"] = cid, c.get("name", "")
        _put(dc, "status", c.get("status"))
        _put(dc, "locationId", next((a.get("id") for a in ancestors(cid) if a.get("id") in location_ids), None))
        derived_dcs[cid] = dc
    if doc.get("dataCentres") is not None or derived_dcs:
        doc["dataCentres"] = ordered(doc.get("dataCentres") or [], derived_dcs, floors)

    for rack in doc.get("racks") or []:
        dc_id = row_id = None
        if rack.get("containerId"):
            for a in ancestors(rack.get("containerId")):
                if a.get("layout") == "floor":
                    dc_id = a.get("id")
                    break
                if a.get("layout") == "row" and row_id is None:
                    row_id = a.get("id")
        _put(rack, "rowId", row_id)
        _put(rack, "dcId", dc_id)

    row_by_id = {row.get("id"): row for row in doc.get("rows") or []}
    derived_rows: dict[Any, dict] = {}
    row_order = [c.get("id") for c in containers if c.get("layout") == "row"]
    for cid in row_order:
        members = [r.get("id") for r in doc.get("racks") or [] if r.get("rowId") == cid]
        row = row_by_id.get(cid, {"id": cid})
        rack_ids, listed = [], set()
        for ident in row.get("rackIds") or []:
            if ident in members and ident not in listed:
                rack_ids.append(ident)
            listed.add(ident)
        rack_ids.extend(i for i in members if i not in listed)
        row["id"], row["name"] = cid, by_id[cid].get("name", "")
        _put(row, "dcId", next((a.get("id") for a in ancestors(cid) if a.get("layout") == "floor"), None))
        row["rackIds"] = rack_ids
        derived_rows[cid] = row
    if doc.get("rows") is not None or derived_rows:
        doc["rows"] = ordered(doc.get("rows") or [], derived_rows, row_order)


__all__ = [
    "FIELD_OWNERSHIP",
    "Conflict",
    "KindDiff",
    "MergeDiff",
    "MergeResult",
    "Retained",
    "merge",
]

"""Limit a sync to what changed in Railyard since an earlier commit: a sync document's ``changes`` block.

A NetBox or Nautobot sync document asked for with ``since`` (the id of a saved version of the same project) is still
complete, and also carries ``changes``::

    {"since": {"versionId": "…", "number": 12, "revision": 370},
     "added": 11, "changed": 12, "removed": 10,
     "objects": {"racks": {"added":   [{"key": {"site": "LON1", "name": "R05"}, "railyard": {…}}],
                           "changed": [{"key": {…}, "previousKey": {…}, "fields": [{"column", "from", "to"}], …}],
                           "removed": [<the earlier document's whole row>, …]}, …}}

``key`` names the columns an object's row is found under in ``objects``; ``previousKey`` the key it had in the earlier
version, when a change renamed it; ``removed`` holds the earlier document's rows. A change that only follows a rename
(a device in a renamed rack) is not listed.

A sync limited to the changes (``sync_to_netbox`` / ``sync_to_nautobot`` with ``only_changes``) creates and updates
only the objects listed as added or changed, and deletes (with ``allow_deletes``) only those listed as removed, instead
of every owned object the document lacks. Objects nobody changed in Railyard are not compared, so an edit made in the
DCIM meanwhile to one of them is left alone. A rename the DCIM has not seen is applied in place where the target can
rename (devices, and NetBox locations and racks); a renamed component or cable is replaced.

All knowledge of the block's shape lives here. The rows are read by the document adapters' own readers
(``row_id``), so a listed object is the model the full sync would have built from the same row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sync_document import SyncDocumentError

#: Sections whose renamed objects a target can rename in place (devices by their Railyard id; locations and racks on a
#: target that sets ``renames_places``). Other renamed objects are replaced: the old one deleted, the new one created.
_RENAMED_IN_PLACE = {"device", "location", "rack"}

#: The columns that name another object, by the kind of object they name (NetBox's, then Nautobot's names).
_NAMING_COLUMNS = {
    "device": {"device", "side_a_device", "side_b_device", "device__name"},
    "location": {"location", "parent", "location__name", "parent__name"},
}

#: A Nautobot cable row holds its ends as ``a`` and ``b`` objects; its key names them as NetBox's columns do.
_CABLE_ENDS = {"side_a_": "a", "side_b_": "b"}


@dataclass
class ChangeSet:
    """The ``changes`` block as model identities: what a sync limited to it may create, update and delete."""

    since: dict[str, Any]
    added: int
    changed: int
    removed_count: int
    #: (model type, unique id) of each object listed as added or changed.
    wanted: set[tuple[str, str]] = field(default_factory=set)
    #: (model type, earlier unique id, unique id, section, the row as it was) of each changed object a change renamed.
    renamed: list[tuple[str, str, str, str, dict]] = field(default_factory=list)
    #: (section, row) as the earlier document had them: the removed rows, and renamed objects to replace.
    earlier: list[tuple[str, dict]] = field(default_factory=list)
    #: Names a change renamed, by kind ("device", "location"): earlier name -> name.
    names: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Listed changes that name no row of the document (a newer Railyard, or a section this package does not read).
    unmatched: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """The earlier version in words: ``commit 12`` (or its id)."""
        number = self.since.get("number")
        return f"commit {number}" if number else f"version {self.since.get('versionId') or '(unnamed)'}"


def raw_changes(document: dict) -> dict | None:
    """The document's ``changes`` block, checked for shape; ``None`` when the document has none."""
    block = document.get("changes")
    if block is None:
        return None
    if not isinstance(block, dict) or not isinstance(block.get("objects", {}), dict):
        raise SyncDocumentError("The sync document's 'changes' must be a JSON object with an 'objects' object.")
    for kind, section in block.get("objects", {}).items():
        if not isinstance(section, dict):
            raise SyncDocumentError(f"The sync document's changes.objects.{kind} must be a JSON object.")
        for part in ("added", "changed", "removed"):
            entries = section.get(part) or []
            if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
                raise SyncDocumentError(f"The sync document's changes.objects.{kind}.{part} must be a list of objects.")
    return block


def change_set(source) -> ChangeSet:
    """The source adapter's document's ``changes`` as model identities (see :class:`ChangeSet`).

    ``source`` is a loaded :class:`~railyard_sync.export.sync_document.SyncDocumentAdapter` or
    :class:`~railyard_sync.export.nautobot_document.NautobotSyncDocumentAdapter`. Raises ``SyncDocumentError`` when
    the document has no ``changes`` (it was not asked for with ``since``)."""
    block = source.document.changes
    if block is None:
        raise SyncDocumentError(
            "The sync document lists no changes: it was not requested with 'since', or this Railyard does not "
            "support it. Run the sync without limiting it to the changes."
        )
    out = ChangeSet(
        since=dict(block.get("since") or {}),
        added=_count(block.get("added")),
        changed=_count(block.get("changed")),
        removed_count=_count(block.get("removed")),
    )
    objects = source.document.objects
    for kind, section in (block.get("objects") or {}).items():
        rows = objects.get(kind)
        if rows is None:
            listed = sum(len(section.get(p) or []) for p in ("added", "changed", "removed"))
            if listed:
                out.unmatched.append(f"{listed} change(s) to {kind}, which this railyard-sync does not sync")
            continue
        for part in ("added", "changed"):
            for entry in section.get(part) or []:
                key = entry.get("key") if isinstance(entry.get("key"), dict) else {}
                row = _find(rows, key)
                if row is None:
                    out.unmatched.append(f"{kind} {_words(key)} ({part}): not found in the document")
                    continue
                ident = source.row_id(kind, row)
                if ident is None:
                    continue  # a row that is no object of its own (a design tag: created as the objects need it)
                out.wanted.add(ident)
                previous = entry.get("previousKey")
                if part != "changed" or not isinstance(previous, dict) or not previous:
                    continue
                old_row = _with_key(row, previous)
                old = source.row_id(kind, old_row)
                if old is None or old == ident:
                    continue
                if ident[0] in _RENAMED_IN_PLACE:
                    out.renamed.append((ident[0], old[1], ident[1], kind, old_row))
                    if ident[0] in _NAMING_COLUMNS and previous.get("name") and key.get("name"):
                        out.names.setdefault(ident[0], {})[str(previous["name"])] = str(key["name"])
                else:
                    out.earlier.append((kind, old_row))
        out.earlier += [(kind, row) for row in section.get("removed") or []]
    return out


def narrow(source, target, changes: ChangeSet) -> None:
    """Limit ``source`` to the objects ``changes`` lists as added or changed, and re-key the target's owned objects
    that a listed change renamed, so the diff renames them in place (where the target can) rather than creating
    second ones. Call it after ``target.load()`` and before ``target.reconcile()``."""
    if not hasattr(target, "place_renames"):
        target.place_renames = []
    for model_type, old, new, kind, old_row in changes.renamed:
        if model_type == "device":
            continue  # the device's Railyard id renames it (OwnershipMixin.detect_renames)
        if not getattr(target, "renames_places", False):
            # Replaced: the new one is created and the old one deleted, so what is in it moves to the new one, though
            # the changes do not list it (they list no change that only follows a rename).
            changes.earlier.append((kind, old_row))
            new_name = source.get(model_type, new).name
            fields = ("parent", "location") if model_type == "location" else ("rack",)
            for type_name in ("location", "rack", "device"):
                for model in source.get_all(type_name):
                    if any(getattr(model, f, None) == new_name for f in fields):
                        changes.wanted.add((type_name, model.get_unique_id()))
            continue
        owned = target.get_or_none(model_type, old)
        if owned is None or owned.nb_id is None or target.get_or_none(model_type, new) is not None:
            continue
        new_name = source.get(model_type, new).name
        existing = target.place_named(model_type, owned.site, new_name)
        if existing is not None and existing != owned.nb_id:
            continue  # the new name is taken in the DCIM: reconcile uses that object as it is
        target.rekey_place(model_type, owned.site, owned.name, new_name)
        target.place_renames.append((model_type, owned.site, owned.name, new_name, owned.nb_id))
        target.report.renamed.append(f"{model_type} {owned.site}/{owned.name} → {new_name}")
    for model_type in source.top_level:
        for model in list(source.get_all(model_type)):
            if (model_type, model.get_unique_id()) not in changes.wanted:
                source.remove(model)


def removed(source, target, changes: ChangeSet) -> set[tuple[str, str]]:
    """The owned objects the changes remove (the earlier rows' objects), as the target holds them after
    ``reconcile``: an earlier row names a renamed device or location by its earlier name, and the target holds what
    hangs off it under the new name when it renamed it in place, under the earlier one otherwise."""
    out: set[tuple[str, str]] = set()
    devices = changes.names.get("device", {})
    places = changes.names.get("location", {})
    # A row may name a device or location by its earlier name or (a key that left it unchanged) its new one; the
    # target holds the objects under either. Only an owned object is ever matched, so trying each is safe.
    maps: list[dict[str, dict[str, str]]] = []
    for device_names in ({}, devices):
        for place_names in ({}, places, {new: old for old, new in places.items()}):
            maps.append({"device": device_names, "location": place_names})
    for kind, row in changes.earlier:
        for names in maps:
            ident = source.row_id(kind, _renamed(row, names))
            if ident is not None and target.get_or_none(ident[0], ident[1]) is not None:
                out.add(ident)
                break
    return out


def _flat(row: dict) -> dict:
    """A row with a Nautobot cable's ``a``/``b`` ends also as NetBox's ``side_a_device``… columns."""
    flat = dict(row)
    for prefix, end in _CABLE_ENDS.items():
        if isinstance(row.get(end), dict):
            for field_name in ("device", "type", "name"):
                flat[prefix + field_name] = row[end].get(field_name)
    return flat


def _with_key(row: dict, key: dict) -> dict:
    """``row`` with ``key``'s values in its columns (into a Nautobot cable's ``a``/``b`` ends where they live)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in row.items()}
    for column, value in key.items():
        prefix = column[:7]
        if prefix in _CABLE_ENDS and isinstance(out.get(_CABLE_ENDS[prefix]), dict):
            out[_CABLE_ENDS[prefix]][column[7:]] = value
        else:
            out[column] = value
    return out


def _renamed(row: dict, names: dict[str, dict[str, str]]) -> dict:
    """``row`` with the devices and locations it names renamed as ``names`` says."""
    key = {}
    flat = _flat(row)
    for kind, columns in _NAMING_COLUMNS.items():
        for column in columns:
            if column in flat and flat[column] in names.get(kind, {}):
                key[column] = names[kind][flat[column]]
    if "device" in names:
        for prefix in _CABLE_ENDS:
            column = prefix + "device"
            if flat.get(column) in names["device"]:
                key[column] = names["device"][flat[column]]
    return _with_key(row, key) if key else row


def _find(rows: list[dict], key: dict) -> dict | None:
    """The row whose columns hold ``key``'s values (the first, as the document's readers keep the first)."""
    if not key:
        return None
    for row in rows:
        flat = _flat(row)
        if all(_cell(flat.get(column)) == _cell(value) for column, value in key.items()):
            return row
    return None


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


def _words(key: dict) -> str:
    return "[" + ", ".join(f"{k}={v}" for k, v in key.items()) + "]"


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

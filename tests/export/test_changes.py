"""A sync limited to a document's ``changes`` (``only_changes``; ``railyard-sync export … --since``): only what
Railyard lists as added or changed since a commit is created or updated, only what it lists as removed is deleted,
and renames it lists are applied in place where the DCIM allows it.

The documents are the cabled fixture changed as a later commit would change it, with the ``changes`` block Railyard
serves beside the objects when asked with ``since`` (see ``railyard_sync/export/changes.py`` for its shape)."""

import copy

import pytest
import test_nautobot_export as nbt
from fake_nautobot_rest import FakeNautobot
from fake_netbox_rest import FakeNetBox
from test_netbox_export import document, drop_device, rename_device, sync

from railyard_sync.export.changes import change_set
from railyard_sync.export.sync_document import SyncDocumentAdapter, SyncDocumentError

SINCE = {"versionId": "ver_12", "number": 12, "revision": 370}


def with_changes(doc: dict, **sections) -> dict:
    """``doc`` with a ``changes`` block: ``sections`` maps a section to ``{"added": …, "changed": …, "removed": …}``."""
    doc = copy.deepcopy(doc)
    objects = {kind: {"added": [], "changed": [], "removed": [], **parts} for kind, parts in sections.items()}
    counts = {part: sum(len(s[part]) for s in objects.values()) for part in ("added", "changed", "removed")}
    doc["changes"] = {"since": SINCE, **counts, "objects": objects}
    return doc


def changed(key: dict, previous: dict | None = None, **fields) -> dict:
    entry = {"key": key, "fields": [{"column": c, "from": f, "to": t} for c, (f, t) in fields.items()]}
    if previous:
        entry["previousKey"] = previous
    entry["railyard"] = {"kind": "x"}
    return entry


def row(doc: dict, kind: str, **match) -> dict:
    return next(r for r in doc["objects"][kind] if all(r.get(k) == v for k, v in match.items()))


def rename_rack(doc: dict, old: str, new: str) -> dict:
    doc = copy.deepcopy(doc)
    for r in doc["objects"]["racks"]:
        if r["name"] == old:
            r["name"] = new
    for r in doc["objects"]["devices"]:
        if r["rack"] == old:
            r["rack"] = new
    return doc


# ---- NetBox ------------------------------------------------------------------------------------------------


def test_only_the_objects_listed_are_compared_so_an_edit_made_in_netbox_meanwhile_is_kept():
    nb = FakeNetBox()
    assert sync(nb).ok
    panel = nb.one("dcim/devices", name="PP-1")
    nb.objects["dcim/devices"][panel["id"]]["comments"] = "edited in NetBox"

    head = document()
    row(head, "devices", name="SRV-1")["serial"] = "SN-0002"
    head = with_changes(head, devices={"changed": [changed({"name": "SRV-1"}, serial=("SN-0001", "SN-0002"))]})

    full = sync(nb, head, dry_run=True)
    assert any(line.startswith("update: device [name=PP-1]") for line in full.changes)  # would undo the edit

    result = sync(nb, head, only_changes=True)
    assert result.ok, result.errors
    assert result.since == {**SINCE, "added": 0, "changed": 1, "removed": 0}
    assert (result.created, result.updated, result.deleted) == (0, 1, 0)
    assert nb.one("dcim/devices", name="SRV-1")["serial"] == "SN-0002"
    assert nb.objects["dcim/devices"][panel["id"]]["comments"] == "edited in NetBox"


def test_only_rows_listed_as_removed_are_deleted_and_only_with_allow_deletes():
    nb = FakeNetBox()
    assert sync(nb).ok
    base = document()
    l2 = row(base, "cables", label="L2")
    head = copy.deepcopy(base)
    head["objects"]["cables"] = [c for c in head["objects"]["cables"] if c["label"] != "L2"]
    head = drop_device(head, "PDU-1")  # gone from the document, but not listed: a full sync's business, not this one's
    head = with_changes(head, cables={"removed": [l2]})

    kept = sync(nb, head, only_changes=True)
    assert kept.ok and kept.stale == ["cable [PP-1__dcim.rearport__1__SRV-1__dcim.interface__iface1]"], kept.stale
    assert nb.all("dcim/cables", label="L2")

    result = sync(nb, head, only_changes=True, allow_deletes=True)
    assert result.ok, result.errors
    assert result.deleted == 1 and not nb.all("dcim/cables", label="L2")
    assert nb.all("dcim/devices", name="PDU-1")  # not listed as removed: kept


def test_a_renamed_rack_is_renamed_in_place_and_keeps_its_devices():
    """Railyard does not list the devices of a renamed rack (only the rename changes them): the rack is renamed in
    NetBox, so they stay in it."""
    nb = FakeNetBox()
    assert sync(nb).ok
    rack = nb.one("dcim/racks", name="R1")
    head = rename_rack(document(), "R1", "R1-NEW")
    head = with_changes(
        head,
        racks={
            "changed": [
                changed({"site": "DC1", "name": "R1-NEW"}, {"site": "DC1", "name": "R1"}, name=("R1", "R1-NEW"))
            ]
        },
    )
    preview = sync(nb, head, only_changes=True, dry_run=True)
    assert preview.renamed == ["rack DC1/R1 → R1-NEW"] and preview.diff["create"] == 0

    result = sync(nb, head, only_changes=True, allow_deletes=True)
    assert result.ok, result.errors
    assert (result.created, result.deleted) == (0, 0)
    assert nb.objects["dcim/racks"][rack["id"]]["name"] == "R1-NEW"
    assert {d["rack"] for d in nb.all("dcim/devices")} == {rack["id"]}
    assert sync(nb, head, dry_run=True, allow_deletes=True).diff["create"] == 0  # a full sync agrees


def test_rows_removed_from_a_renamed_device_name_it_as_it_was():
    """The earlier document's rows name SW-1; NetBox holds its interface under SW-1A once the device is renamed."""
    nb = FakeNetBox()
    assert sync(nb).ok
    base = document()
    eth1 = row(base, "interfaces", device="SW-1", name="Eth1")
    l1 = row(base, "cables", label="L1")
    head = rename_device(base, "SW-1", "SW-1A")
    head["objects"]["interfaces"] = [i for i in head["objects"]["interfaces"] if i["name"] != "Eth1"]
    head["objects"]["cables"] = [c for c in head["objects"]["cables"] if c["label"] != "L1"]
    head = with_changes(
        head,
        devices={"changed": [changed({"name": "SW-1A"}, {"name": "SW-1"}, name=("SW-1", "SW-1A"))]},
        interfaces={"removed": [eth1]},
        cables={"removed": [l1]},
    )
    result = sync(nb, head, only_changes=True, allow_deletes=True)
    assert result.ok, result.errors
    assert result.renamed == ["device SW-1 → SW-1A"]
    switch = nb.one("dcim/devices", name="SW-1A")
    assert not nb.all("dcim/interfaces", device=switch["id"], name="Eth1")
    assert not nb.all("dcim/cables", label="L1")
    assert result.deleted == 2


def test_a_document_without_changes_is_refused():
    with pytest.raises(SyncDocumentError, match="lists no changes"):
        sync(FakeNetBox(), document(), only_changes=True)


def test_changes_this_railyard_sync_cannot_find_are_warnings():
    head = with_changes(
        document(),
        devices={"added": [{"key": {"name": "NOT-THERE"}, "railyard": {"kind": "device"}}]},
        **{"console-ports": {"added": [{"key": {"device": "SW-1", "name": "con0"}, "railyard": {}}]}},
    )
    source = SyncDocumentAdapter(head, name="railyard")
    source.load()
    found = change_set(source)
    assert found.unmatched == [
        "devices [name=NOT-THERE] (added): not found in the document",
        "1 change(s) to console-ports, which this railyard-sync does not sync",
    ]
    result = sync(FakeNetBox(), head, only_changes=True, dry_run=True)
    assert "Not applied: devices [name=NOT-THERE] (added): not found in the document" in result.warnings


def test_a_malformed_changes_block_is_refused():
    doc = document()
    doc["changes"] = {"objects": {"racks": {"added": "R1"}}}
    with pytest.raises(SyncDocumentError, match="changes.objects.racks.added"):
        SyncDocumentAdapter(doc, name="railyard")


# ---- Nautobot ----------------------------------------------------------------------------------------------


def test_nautobot_a_renamed_rack_is_replaced_and_its_devices_move_to_it():
    """Nautobot's target does not rename racks in place: the new rack is created, the devices Railyard did not list
    move to it, and the old rack is deleted (with allow_deletes)."""
    nb = FakeNautobot()
    assert nbt.sync(nb).ok
    head = nbt.document()
    for r in head["objects"]["racks"]:
        r["name"] = "R1-NEW"
    for r in head["objects"]["devices"]:
        r["rack__name"] = "R1-NEW"
    head = with_changes(head, racks={"changed": [changed({"name": "R1-NEW"}, {"name": "R1"}, name=("R1", "R1-NEW"))]})
    result = nbt.sync(nb, head, only_changes=True, allow_deletes=True)
    assert result.ok, result.errors
    assert [r["name"] for r in nbt.owned(nb, "dcim/racks")] == ["R1-NEW"]
    new = nbt.owned(nb, "dcim/racks")[0]["id"]
    assert {d["rack"] for d in nbt.owned(nb, "dcim/devices")} == {new}
    again = nbt.sync(nb, head, dry_run=True, allow_deletes=True)
    assert (again.diff["create"], again.diff["update"], again.diff["delete"]) == (0, 0, 0)

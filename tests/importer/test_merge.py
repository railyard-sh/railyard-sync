"""merge(existing, imported): the re-import policy in CLAUDE.md — DCIM-owned fields refresh, Railyard's
work is kept, stale objects are reported (removed only with allow_deletes, and never from under a
Railyard object), collection order is stable, and clashes are reported rather than resolved."""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from railyard_sync.importer import merge
from railyard_sync.importer.merge import FIELD_OWNERSHIP, KINDS

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"


def built() -> dict:
    """A project as the builder makes it from one NetBox site: a site (floor) with one location (group),
    two racks, a switch, a server, a 0U PDU, a custom and a catalogue-copied device type, a data cable and
    a power link."""
    return {
        "schemaVersion": "1",
        "id": "ry-0123456789abcdef0123",
        "name": "LDN1 baseline",
        "containers": [
            {"id": "nb-site-1", "name": "LDN1", "type": "Site", "layout": "floor", "status": "Active"}
            | {"exportSite": True},
            {"id": "nb-loc-10", "name": "Hall 1", "type": "Location", "parentId": "nb-site-1", "layout": "group"},
        ],
        "locations": [{"id": "nb-loc-10", "name": "Hall 1"}],
        "dataCentres": [{"id": "nb-site-1", "name": "LDN1", "status": "Active"}],
        "rows": [],
        "rackTypes": [{"key": "nb-rt-apc-ar3100", "manufacturer": "APC", "model": "AR3100", "uHeight": 42}],
        "racks": [
            {
                "id": "nb-rack-100",
                "name": "A01",
                "uHeight": 42,
                "widthMm": 600,
                "containerId": "nb-loc-10",
                "dcId": "nb-site-1",
                "rackTypeKey": "nb-rt-apc-ar3100",
                "indexInRow": 0,
                "status": "Active",
                "tags": ["prod"],
                "placements": [
                    {
                        "id": "nb-dev-1000",
                        "startU": 40,
                        "heightU": 1,
                        "face": "front",
                        "label": "ldn1-sw1",
                        "namingMode": "manual",
                        "deviceTypeRef": "nb-dt-acme-sw",
                        "role": "leaf",
                        "ports": [
                            {"id": "nb-if-1", "name": "Eth1", "side": "front", "kind": "interface", "origin": "custom"},
                            {"id": "nb-if-2", "name": "Eth2", "side": "front", "kind": "interface", "origin": "custom"},
                        ],
                        "powerInlets": [],
                    },
                    {
                        "id": "nb-dev-1001",
                        "startU": 1,
                        "heightU": 2,
                        "face": "full",
                        "label": "ldn1-srv1",
                        "namingMode": "manual",
                        "serial": "SN1",
                        "deviceTypeRef": "dell-r650",
                        "ports": [],
                        "powerInlets": [{"id": "nb-pp-1", "name": "PSU1", "origin": "custom"}],
                    },
                    {
                        "id": "nb-dev-1002",
                        "startU": 0,
                        "heightU": 0,
                        "face": "front",
                        "mount": "zeroU",
                        "side": "left",
                        "label": "ldn1-pdu1",
                        "namingMode": "manual",
                        "deviceTypeRef": "nb-dt-acme-pdu",
                        "ports": [],
                        "powerInlets": [],
                    },
                ],
            },
            {
                "id": "nb-rack-101",
                "name": "A02",
                "uHeight": 42,
                "widthMm": 600,
                "containerId": "nb-loc-10",
                "dcId": "nb-site-1",
                "indexInRow": 0,
                "status": "Active",
                "placements": [
                    {
                        "id": "nb-dev-1003",
                        "startU": 40,
                        "heightU": 1,
                        "face": "front",
                        "label": "ldn1-sw2",
                        "namingMode": "manual",
                        "deviceTypeRef": "nb-dt-acme-sw",
                        "ports": [
                            {"id": "nb-if-3", "name": "Eth1", "side": "front", "kind": "interface", "origin": "custom"}
                        ],
                        "powerInlets": [],
                    }
                ],
            },
        ],
        "catalogue": [
            {"key": "nb-dt-acme-sw", "manufacturer": "Acme", "model": "SW-48", "uHeight": 1, "fullDepth": False},
            {
                "key": "dell-r650",
                "manufacturer": "Dell",
                "model": "PowerEdge R650",
                "uHeight": 1,
                "fullDepth": True,
                "source": {"kind": "catalogue", "ref": "dell-r650"},
            },
            {
                "key": "nb-dt-acme-pdu",
                "manufacturer": "Acme",
                "model": "PDU-8",
                "uHeight": 0,
                "fullDepth": False,
                "outlets": {"count": 8, "type": "C13"},
            },
        ],
        "cables": [
            {
                "id": "nb-cable-1",
                "a": {"rackId": "nb-rack-100", "placementId": "nb-dev-1000", "portId": "nb-if-1"},
                "b": {"rackId": "nb-rack-101", "placementId": "nb-dev-1003", "portId": "nb-if-3"},
                "media": "Cat6a",
                "label": "C1",
                "kind": "patch",
            }
        ],
        "powerLinks": [
            {
                "id": "nb-power-5",
                "device": {"rackId": "nb-rack-100", "placementId": "nb-dev-1001", "inletId": "nb-pp-1"},
                "pdu": {"rackId": "nb-rack-100", "placementId": "nb-dev-1002", "outlet": 1},
            }
        ],
        "meta": {
            "railyardSync": {
                "source": {"kind": "netbox", "url": "https://netbox.example.com", "sites": ["ldn1"]},
                "importedAt": "2026-10-01T00:00:00Z",
            }
        },
    }


def rack(doc: dict, rack_id: str) -> dict:
    return next(r for r in doc["racks"] if r["id"] == rack_id)


def device(doc: dict, pid: str) -> dict:
    return next(pl for r in doc["racks"] for pl in r["placements"] if pl["id"] == pid)


def rack_of(doc: dict, pid: str) -> str:
    return next(r["id"] for r in doc["racks"] for pl in r["placements"] if pl["id"] == pid)


def ids(items: list[dict], key: str = "id") -> list[str]:
    return [i[key] for i in items]


def edited() -> dict:
    """The built estate after a week of work in Railyard: Railyard fields set on imported objects, and
    Railyard's own device, cable, rack, row, topology and meet-me room alongside them."""
    doc = built()
    sw = device(doc, "nb-dev-1000")
    sw["colour"] = "#ff0000"
    sw["powerW"] = 350
    sw["notes"] = "replace in Q3"
    sw["tags"] = ["core"]
    a01 = rack(doc, "nb-rack-100")
    a01["facing"] = "down"
    a01["notes"] = "cold aisle"
    a01["powerCapacityW"] = 8000
    a01["placements"].append(
        {"id": "pl_fw", "startU": 30, "heightU": 2, "face": "front", "label": "fw-planned", "deviceTypeRef": "fw"}
    )
    doc["racks"].append(
        {
            "id": "rack_new",
            "name": "A03",
            "uHeight": 42,
            "containerId": "nb-loc-10",
            "dcId": "nb-site-1",
            "indexInRow": 0,
            "placements": [],
        }
    )
    doc["cables"].append(
        {
            "id": "cab_fw",
            "a": {"rackId": "nb-rack-100", "placementId": "pl_fw"},
            "b": {"rackId": "nb-rack-100", "placementId": "nb-dev-1000", "portId": "nb-if-2"},
        }
    )
    doc["dataCentres"][0]["aisleMm"] = 1200
    doc["topologies"] = [
        {
            "id": "topo1",
            "name": "Fabric",
            "layers": [],
            "nodes": [{"id": "n1", "placementId": "nb-dev-1000", "layerId": "l", "x": 0, "y": 0}],
            "edges": [{"id": "e1", "cableId": "nb-cable-1", "layerId": "l", "points": []}],
        }
    ]
    doc["meta"]["owner"] = "network team"
    return doc


# ---- idempotence ------------------------------------------------------------------


def test_merging_an_import_into_itself_changes_nothing():
    doc = built()
    result = merge(doc, built())
    assert result.project == doc
    assert not result.diff.changed
    assert result.diff.stale_count == 0
    assert result.diff.conflicts == []
    assert result.diff["racks"].unchanged == ["nb-rack-100", "nb-rack-101"]
    assert result.diff["placements"].unchanged == ["nb-dev-1000", "nb-dev-1001", "nb-dev-1002", "nb-dev-1003"]


def test_reimporting_the_same_source_into_an_edited_estate_changes_nothing():
    existing = edited()
    result = merge(existing, built())
    assert result.project == existing
    assert not result.diff.changed
    assert result.diff.conflicts == []


def test_merge_is_idempotent_after_a_change():
    first = merge(edited(), _renamed(built())).project
    second = merge(first, _renamed(built()))
    assert second.project == first
    assert not second.diff.changed


def test_a_railyard_only_estate_merged_with_itself_is_untouched():
    estate = json.loads((FIXTURES / "example-estate.json").read_text())
    result = merge(estate, copy.deepcopy(estate))
    assert result.project == estate
    assert all(not d.added and not d.updated and not d.stale for d in result.diff.kinds.values())


def test_inputs_are_not_modified():
    existing, imported = edited(), _renamed(built())
    snapshot = copy.deepcopy((existing, imported))
    merge(existing, imported, allow_deletes=True)
    assert (existing, imported) == snapshot


def _renamed(doc: dict) -> dict:
    device(doc, "nb-dev-1000")["label"] = "ldn1-leaf1"
    return doc


# ---- importing into an estate designed in Railyard --------------------------------------


def test_import_into_a_railyard_estate_appends_and_leaves_railyard_objects_alone():
    estate = json.loads((FIXTURES / "example-estate.json").read_text())
    result = merge(estate, built())
    doc = result.project
    for kind in ("containers", "dataCentres", "racks", "catalogue"):
        key = "key" if kind == "catalogue" else "id"
        before = estate[kind]
        assert doc[kind][: len(before)] == before, kind  # untouched, in place
    assert ids(doc["racks"])[-2:] == ["nb-rack-100", "nb-rack-101"]
    assert result.diff["racks"].added == ["nb-rack-100", "nb-rack-101"]
    assert "dell-r650" in result.diff["catalogue"].added  # a catalogue copy is reported as added too
    assert doc["meta"]["railyardSync"]["source"]["kind"] == "netbox"
    assert ids(doc["dataCentres"])[-1] == "nb-site-1"
    assert key


def test_legacy_estate_without_spaces_gets_its_space_tree():
    legacy = json.loads((FIXTURES / "example-project.json").read_text())
    assert "containers" not in legacy
    doc = merge(legacy, built()).project
    containers = {c["id"]: c for c in doc["containers"]}
    for loc in legacy["locations"]:
        assert containers[loc["id"]]["layout"] == "group"
    for dc in legacy["dataCentres"]:
        assert containers[dc["id"]]["layout"] == "floor"
    for legacy_rack in legacy["racks"]:
        merged = rack(doc, legacy_rack["id"])
        assert merged["containerId"] in (legacy_rack.get("rowId"), legacy_rack.get("dcId"))
        assert (merged.get("rowId"), merged.get("dcId")) == (legacy_rack.get("rowId"), legacy_rack.get("dcId"))
        assert merged["placements"] == legacy_rack["placements"]


# ---- Railyard's edits survive; the DCIM's fields refresh -------------------------------------------


def test_railyard_fields_survive_a_reimport_that_changes_the_dcims():
    imported = built()
    sw = device(imported, "nb-dev-1000")
    sw["label"] = "ldn1-leaf1"  # renamed in NetBox
    sw["startU"] = 38  # moved down two units
    sw["tags"] = ["prod"]
    rack(imported, "nb-rack-100")["name"] = "A01-new"
    rack(imported, "nb-rack-100")["tags"] = ["prod", "pci"]

    result = merge(edited(), imported)
    doc = result.project
    sw = device(doc, "nb-dev-1000")
    assert (sw["label"], sw["startU"]) == ("ldn1-leaf1", 38)
    assert (sw["colour"], sw["powerW"], sw["notes"]) == ("#ff0000", 350, "replace in Q3")
    assert sw["tags"] == ["core", "prod"]  # Railyard's first, the DCIM's new ones after
    a01 = rack(doc, "nb-rack-100")
    assert a01["name"] == "A01-new"
    assert (a01["facing"], a01["notes"], a01["powerCapacityW"]) == ("down", "cold aisle", 8000)
    assert a01["tags"] == ["prod", "pci"]
    assert device(doc, "pl_fw") == device(edited(), "pl_fw")
    assert ids(a01["placements"]) == ["nb-dev-1000", "nb-dev-1001", "nb-dev-1002", "pl_fw"]
    assert rack(doc, "rack_new") == rack(edited(), "rack_new")
    assert next(c for c in doc["cables"] if c["id"] == "cab_fw") == edited()["cables"][1]
    assert doc["topologies"] == edited()["topologies"]
    assert doc["dataCentres"][0]["aisleMm"] == 1200
    assert doc["meta"]["owner"] == "network team"
    assert result.diff["placements"].updated == ["nb-dev-1000"]
    assert result.diff["racks"].updated == ["nb-rack-100"]
    assert "pl_fw" not in result.diff["placements"].unchanged  # Railyard's objects are not listed


def test_owned_fields_the_dcim_cleared_are_removed_but_if_set_fields_are_kept():
    imported = built()
    srv = device(imported, "nb-dev-1001")
    del srv["serial"]  # serial cleared in NetBox: owned, so it goes
    existing = edited()
    device(existing, "nb-dev-1001")["role"] = "compute"  # NetBox has no role: Railyard's is kept
    doc = merge(existing, imported).project
    assert "serial" not in device(doc, "nb-dev-1001")
    assert device(doc, "nb-dev-1001")["role"] == "compute"
    assert rack(doc, "nb-rack-100")["powerCapacityW"] == 8000  # no feeds in NetBox: Railyard's budget stays
    rack(imported, "nb-rack-100")["powerCapacityW"] = 7360  # now NetBox has feeds
    assert rack(merge(existing, imported).project, "nb-rack-100")["powerCapacityW"] == 7360


def test_a_device_railyard_names_keeps_its_label():
    existing = built()
    device(existing, "nb-dev-1000").update(namingMode="auto", label="LDN1-A01-LEAF-01")
    imported = _renamed(built())
    sw = device(merge(existing, imported).project, "nb-dev-1000")
    assert (sw["label"], sw["namingMode"]) == ("LDN1-A01-LEAF-01", "auto")


def test_catalogue_copies_are_added_but_never_changed():
    existing = built()
    copy_entry = next(e for e in existing["catalogue"] if e["key"] == "dell-r650")
    copy_entry["powerW"] = 450  # edited in Railyard
    imported = built()
    next(e for e in imported["catalogue"] if e["key"] == "nb-dt-acme-sw")["model"] = "SW-48X"
    next(e for e in imported["catalogue"] if e["key"] == "dell-r650")["model"] = "something else"
    result = merge(existing, imported)
    catalogue = {e["key"]: e for e in result.project["catalogue"]}
    assert catalogue["dell-r650"] == copy_entry
    assert catalogue["nb-dt-acme-sw"]["model"] == "SW-48X"
    assert result.diff["catalogue"].updated == ["nb-dt-acme-sw"]


def test_ports_merge_by_id_and_keep_railyard_additions():
    existing = built()
    sw = device(existing, "nb-dev-1000")
    sw["ports"][0]["optic"] = {"type": "SFP-10G-SR"}
    sw["ports"].append({"id": "port_mgmt", "name": "mgmt0", "side": "rear", "kind": "interface", "origin": "custom"})
    imported = built()
    ports = device(imported, "nb-dev-1000")["ports"]
    ports[0]["name"] = "Ethernet1/1"
    ports.append({"id": "nb-if-9", "name": "Eth9", "side": "front", "kind": "interface", "origin": "custom"})
    result = merge(existing, imported)
    merged = device(result.project, "nb-dev-1000")["ports"]
    assert ids(merged) == ["nb-if-1", "nb-if-2", "port_mgmt", "nb-if-9"]
    assert merged[0]["name"] == "Ethernet1/1" and merged[0]["optic"] == {"type": "SFP-10G-SR"}
    assert result.diff["ports"].added == ["nb-if-9"]
    assert result.diff["ports"].updated == ["nb-if-1"]


# ---- moves, renames, deletes --------------------------------------------------------------------


def test_a_device_moved_in_the_dcim_moves_with_its_railyard_fields_and_its_cable_ends():
    imported = built()
    sw = rack(imported, "nb-rack-100")["placements"].pop(0)
    sw["startU"] = 20
    rack(imported, "nb-rack-101")["placements"].append(sw)
    imported["cables"][0]["a"]["rackId"] = "nb-rack-101"

    result = merge(edited(), imported)
    doc = result.project
    assert rack_of(doc, "nb-dev-1000") == "nb-rack-101"
    assert ids(rack(doc, "nb-rack-101")["placements"]) == ["nb-dev-1003", "nb-dev-1000"]
    assert device(doc, "nb-dev-1000")["colour"] == "#ff0000"
    railyard_cable = next(c for c in doc["cables"] if c["id"] == "cab_fw")
    assert railyard_cable["b"]["rackId"] == "nb-rack-101"  # the reference follows the device
    assert any("cab_fw" in n for n in result.diff.notes)
    assert any("moved from rack nb-rack-100 to nb-rack-101" in n for n in result.diff.notes)
    assert "nb-dev-1000" in result.diff["placements"].updated


def test_a_device_moved_into_a_railyard_rack_goes_back_where_the_dcim_has_it():
    existing = edited()
    sw = rack(existing, "nb-rack-100")["placements"].pop(0)
    rack(existing, "rack_new")["placements"].append(sw)
    doc = merge(existing, built()).project
    assert rack_of(doc, "nb-dev-1000") == "nb-rack-100"
    assert rack(doc, "rack_new")["placements"] == []
    assert ids(rack(doc, "nb-rack-100")["placements"])[-1] == "nb-dev-1000"  # appended, not reordered


def test_a_rack_moved_to_another_location_follows_and_its_projections_are_rederived():
    imported = built()
    imported["containers"].append(
        {"id": "nb-loc-11", "name": "Hall 2", "type": "Location", "parentId": "nb-site-1", "layout": "group"}
    )
    imported["locations"].append({"id": "nb-loc-11", "name": "Hall 2"})
    rack(imported, "nb-rack-101")["containerId"] = "nb-loc-11"
    result = merge(edited(), imported)
    assert rack(result.project, "nb-rack-101")["containerId"] == "nb-loc-11"
    assert result.diff["containers"].added == ["nb-loc-11"]
    assert result.diff["locations"].added == ["nb-loc-11"]


def test_racks_gathered_into_a_railyard_row_stay_there():
    existing = edited()
    existing["containers"].append(
        {"id": "row_a", "name": "Row A", "type": "Row", "parentId": "nb-loc-10", "layout": "row"}
    )
    existing["rows"] = [
        {"id": "row_a", "name": "Row A", "dcId": "nb-site-1", "rackIds": ["nb-rack-101", "nb-rack-100"]}
    ]
    for rack_id in ("nb-rack-100", "nb-rack-101"):
        rack(existing, rack_id).update(containerId="row_a", rowId="row_a")
    result = merge(existing, built())
    doc = result.project
    assert rack(doc, "nb-rack-100")["containerId"] == "row_a"
    assert rack(doc, "nb-rack-100")["rowId"] == "row_a"
    assert doc["rows"] == existing["rows"]  # Railyard's rack order in the row is kept
    assert not result.diff.changed

    # NetBox moves the rack to another location: the Railyard row is no longer below it, so it follows.
    imported = built()
    imported["containers"].append(
        {"id": "nb-loc-11", "name": "Hall 2", "type": "Location", "parentId": "nb-site-1", "layout": "group"}
    )
    imported["locations"].append({"id": "nb-loc-11", "name": "Hall 2"})
    rack(imported, "nb-rack-100")["containerId"] = "nb-loc-11"
    doc = merge(existing, imported).project
    assert rack(doc, "nb-rack-100")["containerId"] == "nb-loc-11"
    assert "rowId" not in rack(doc, "nb-rack-100")
    assert next(r for r in doc["rows"] if r["id"] == "row_a")["rackIds"] == ["nb-rack-101"]


def test_a_site_filed_under_a_railyard_region_stays_there():
    existing = built()
    existing["containers"].insert(0, {"id": "grp_emea", "name": "EMEA", "type": "Region", "layout": "group"})
    existing["locations"].insert(0, {"id": "grp_emea", "name": "EMEA"})
    existing["containers"][1]["parentId"] = "grp_emea"
    existing["dataCentres"][0]["locationId"] = "grp_emea"
    result = merge(existing, built())
    assert result.project == existing
    assert not result.diff.changed


def test_renames_in_the_dcim_reach_spaces_and_their_records():
    imported = built()
    imported["containers"][1]["name"] = "Hall One"
    imported["locations"][0]["name"] = "Hall One"
    imported["containers"][0]["name"] = "London 1"
    imported["dataCentres"][0]["name"] = "London 1"
    result = merge(edited(), imported)
    doc = result.project
    assert doc["locations"][0]["name"] == "Hall One"
    assert doc["dataCentres"][0] == {"id": "nb-site-1", "name": "London 1", "status": "Active", "aisleMm": 1200}
    assert result.diff["containers"].updated == ["nb-site-1", "nb-loc-10"]
    assert result.diff["dataCentres"].updated == ["nb-site-1"]


def _without_server(doc: dict) -> dict:
    """The import after the server nb-dev-1001 (and its power cable) was deleted in NetBox."""
    a01 = rack(doc, "nb-rack-100")
    a01["placements"] = [pl for pl in a01["placements"] if pl["id"] != "nb-dev-1001"]
    doc["powerLinks"] = []
    doc["catalogue"] = [e for e in doc["catalogue"] if e["key"] != "dell-r650"]
    return doc


def test_objects_gone_from_the_dcim_are_kept_and_reported_stale():
    existing = edited()
    result = merge(existing, _without_server(built()))
    assert device(result.project, "nb-dev-1001") == device(existing, "nb-dev-1001")
    assert result.project["powerLinks"] == existing["powerLinks"]
    assert result.diff["placements"].stale == ["nb-dev-1001"]
    assert result.diff["powerLinks"].stale == ["nb-power-5"]
    assert result.diff["powerInlets"].stale == ["nb-pp-1"]
    assert result.diff["placements"].removed == []
    assert "Stale" in result.diff.summary() and "nb-dev-1001" in result.diff.summary()


def test_allow_deletes_removes_stale_objects_and_what_ends_on_them():
    result = merge(edited(), _without_server(built()), allow_deletes=True)
    doc = result.project
    assert "nb-dev-1001" not in [pl["id"] for r in doc["racks"] for pl in r["placements"]]
    assert doc["powerLinks"] == []
    assert result.diff["placements"].removed == ["nb-dev-1001"]
    assert result.diff["powerLinks"].removed == ["nb-power-5"]
    assert result.diff["powerInlets"].removed == ["nb-pp-1"]
    # The catalogue copy is not the import's to remove; it stays.
    assert "dell-r650" in [e["key"] for e in doc["catalogue"]]
    assert result.diff.retained == []


def test_allow_deletes_removes_a_stale_rack_with_its_devices_cables_and_unused_custom_types():
    imported = built()
    imported["racks"] = [r for r in imported["racks"] if r["id"] != "nb-rack-101"]
    imported["cables"] = []
    result = merge(built(), imported, allow_deletes=True)
    doc = result.project
    assert ids(doc["racks"]) == ["nb-rack-100"]
    assert doc["cables"] == []
    assert result.diff["racks"].removed == ["nb-rack-101"]
    assert result.diff["placements"].removed == ["nb-dev-1003"]
    assert result.diff["cables"].removed == ["nb-cable-1"]
    assert "nb-dt-acme-sw" in [e["key"] for e in doc["catalogue"]]  # still used by nb-dev-1000


def test_allow_deletes_keeps_what_railyard_objects_depend_on():
    existing = edited()
    # A Railyard cable to the server, and a Railyard device in the rack NetBox deleted.
    existing["cables"].append(
        {
            "id": "cab_srv",
            "a": {"rackId": "nb-rack-100", "placementId": "pl_fw"},
            "b": {"rackId": "nb-rack-100", "placementId": "nb-dev-1001"},
        }
    )
    rack(existing, "nb-rack-101")["placements"].append(
        {"id": "pl_patch", "startU": 1, "heightU": 1, "face": "front", "label": "patch-1"}
    )
    imported = _without_server(built())
    imported["racks"] = [r for r in imported["racks"] if r["id"] != "nb-rack-101"]
    imported["cables"] = []

    result = merge(existing, imported, allow_deletes=True)
    doc = result.project
    retained = {(r.kind, r.id): r.reason for r in result.diff.retained}
    assert "nb-dev-1001" in [pl["id"] for pl in rack(doc, "nb-rack-100")["placements"]]
    assert "cab_srv" in retained[("placements", "nb-dev-1001")]
    assert "nb-rack-101" in ids(doc["racks"])
    assert "patch-1" in retained[("racks", "nb-rack-101")]
    # nb-cable-1 is shown on a Railyard topology, so it stays, and with it the switch it ends on.
    assert "nb-cable-1" in ids(doc["cables"])
    assert "Fabric" in retained[("cables", "nb-cable-1")]
    assert "nb-dev-1003" in [pl["id"] for pl in rack(doc, "nb-rack-101")["placements"]]
    # The power link only ended on stale devices; nothing of Railyard's needs it.
    assert doc["powerLinks"] == []
    assert result.diff["placements"].stale == ["nb-dev-1001", "nb-dev-1003"]
    assert "Kept although gone from the source" in result.diff.summary()
    assert next(c for c in doc["cables"] if c["id"] == "cab_srv") == existing["cables"][-1]


def test_allow_deletes_keeps_a_stale_port_a_railyard_cable_uses():
    imported = built()
    device(imported, "nb-dev-1000")["ports"].pop()  # nb-if-2 deleted in NetBox; cab_fw ends on it
    result = merge(edited(), imported, allow_deletes=True)
    assert "nb-if-2" in ids(device(result.project, "nb-dev-1000")["ports"])
    assert ("ports", "nb-if-2") in {(r.kind, r.id) for r in result.diff.retained}
    result = merge(built(), imported, allow_deletes=True)
    assert ids(device(result.project, "nb-dev-1000")["ports"]) == ["nb-if-1"]
    assert result.diff["ports"].removed == ["nb-if-2"]


def test_a_stale_space_is_removed_only_when_empty():
    existing = built()
    existing["containers"].append(
        {"id": "nb-loc-12", "name": "Cage", "type": "Location", "parentId": "nb-site-1", "layout": "group"}
    )
    existing["locations"].append({"id": "nb-loc-12", "name": "Cage"})
    result = merge(existing, built(), allow_deletes=True)
    assert result.diff["containers"].removed == ["nb-loc-12"]
    assert result.diff["locations"].removed == ["nb-loc-12"]
    assert result.project == built()

    existing["racks"].append({"id": "rack_cage", "name": "C1", "uHeight": 42, "containerId": "nb-loc-12"})
    existing["racks"][-1] |= {"dcId": "nb-site-1", "indexInRow": 0, "placements": []}
    result = merge(existing, built(), allow_deletes=True)
    assert "nb-loc-12" in ids(result.project["containers"])
    assert result.diff["containers"].stale == ["nb-loc-12"]


# ---- order --------------------------------------------------------------------------------------


def test_existing_order_is_kept_and_new_objects_are_appended_in_import_order():
    existing = built()
    existing["racks"].reverse()  # someone reordered racks in Railyard
    rack(existing, "nb-rack-100")["placements"].reverse()
    imported = built()
    imported["racks"].insert(
        0,
        {"id": "nb-rack-102", "name": "A00", "uHeight": 42, "containerId": "nb-loc-10", "placements": []},
    )
    imported["racks"].append({"id": "nb-rack-103", "name": "A04", "uHeight": 42, "containerId": "nb-loc-10"})
    imported["racks"][-1]["placements"] = []
    rack(imported, "nb-rack-100")["placements"].insert(
        0, {"id": "nb-dev-1009", "startU": 10, "heightU": 1, "face": "front", "label": "new"}
    )
    doc = merge(existing, imported).project
    assert ids(doc["racks"]) == ["nb-rack-101", "nb-rack-100", "nb-rack-102", "nb-rack-103"]
    assert ids(rack(doc, "nb-rack-100")["placements"]) == ["nb-dev-1002", "nb-dev-1001", "nb-dev-1000", "nb-dev-1009"]
    assert rack(doc, "nb-rack-102")["dcId"] == "nb-site-1"  # projected for a new rack too


# ---- conflicts ------------------------------------------------------------------------------------


def test_a_railyard_device_now_overlapping_an_imported_one_is_a_conflict():
    imported = built()
    rack(imported, "nb-rack-100")["placements"].append(
        {"id": "nb-dev-1010", "startU": 31, "heightU": 1, "face": "front", "label": "ldn1-fw1"}
    )
    result = merge(edited(), imported)
    (conflict,) = result.diff.conflicts
    assert conflict.kind == "overlap"
    assert set(conflict.ids) == {"pl_fw", "nb-dev-1010"}
    assert "Railyard-designed device 'fw-planned'" in conflict.message and "U31" in conflict.message
    assert "Conflicts" in result.diff.summary()
    assert device(result.project, "pl_fw") == device(edited(), "pl_fw")  # reported, not resolved


def test_rear_and_zero_u_devices_do_not_clash_with_front_ones():
    imported = built()
    rack(imported, "nb-rack-100")["placements"].append(
        {"id": "nb-dev-1011", "startU": 30, "heightU": 1, "face": "rear", "label": "rear-panel"}
    )
    rack(imported, "nb-rack-100")["placements"].append(
        {"id": "nb-dev-1012", "startU": 0, "heightU": 0, "face": "front", "mount": "zeroU", "side": "right"}
    )
    assert merge(edited(), imported).diff.conflicts == []


def test_a_stale_device_overlapping_a_new_one_is_a_conflict_until_deleted():
    imported = _without_server(built())
    rack(imported, "nb-rack-100")["placements"].append(
        {"id": "nb-dev-1020", "startU": 2, "heightU": 1, "face": "front", "label": "ldn1-srv2"}
    )
    result = merge(built(), imported)
    assert [c.ids for c in result.diff.conflicts] == [("nb-dev-1001", "nb-dev-1020")]
    assert "stale imported" in result.diff.conflicts[0].message
    assert merge(built(), imported, allow_deletes=True).diff.conflicts == []


def test_a_railyard_rack_with_an_imported_racks_name_is_a_conflict():
    existing = edited()
    imported = built()
    imported["racks"].append(
        {"id": "nb-rack-104", "name": "a03", "uHeight": 42, "containerId": "nb-loc-10", "placements": []}
    )
    result = merge(existing, imported)
    assert [c.kind for c in result.diff.conflicts] == ["rack_name"]
    assert set(result.diff.conflicts[0].ids) == {"rack_new", "nb-rack-104"}


# ---- meta and the report --------------------------------------------------------------------------


def test_meta_railyard_sync_is_replaced_and_the_rest_of_meta_kept():
    imported = built()
    imported["meta"]["railyardSync"]["importedAt"] = "2026-10-06T00:00:00Z"
    doc = merge(edited(), imported).project
    assert doc["meta"]["railyardSync"]["importedAt"] == "2026-10-06T00:00:00Z"
    assert doc["meta"]["owner"] == "network team"


def test_another_prefix_leaves_netbox_objects_alone():
    nautobot = json.loads(json.dumps(built()).replace('"nb-', '"nbt-'))
    result = merge(built(), nautobot, prefix="nbt")
    assert result.diff["racks"].added == ["nbt-rack-100", "nbt-rack-101"]
    assert result.diff["racks"].stale == []  # nb- racks are not Nautobot's
    assert ids(result.project["racks"])[:2] == ["nb-rack-100", "nb-rack-101"]


def test_summary_counts_each_kind():
    result = merge(edited(), _renamed(_without_server(built())))
    text = result.diff.summary()
    assert "devices: 1 updated, 1 stale, 2 unchanged" in text
    assert "racks: 2 unchanged" in text
    assert result.diff.to_dict()["kinds"]["placements"]["stale"] == ["nb-dev-1001"]


@pytest.mark.parametrize("kind", list(FIELD_OWNERSHIP))
def test_every_ownership_table_uses_known_rules(kind):
    assert set(FIELD_OWNERSHIP[kind].values()) <= {"owned", "if-set", "union", "derived", "railyard"}
    assert kind in KINDS


def test_an_imported_name_a_railyard_device_already_has_is_a_conflict():
    existing = built()
    existing["racks"][0]["placements"].append(
        {"id": "my-switch", "startU": 30, "heightU": 1, "face": "front", "label": "LDN1-SW1 "}
    )
    result = merge(existing, built())
    names = [c for c in result.diff.conflicts if c.kind == "device_name"]
    assert len(names) == 1
    assert set(names[0].ids) == {"nb-dev-1000", "my-switch"}
    assert "Railyard-designed device my-switch" in names[0].message
    assert "rename the Railyard device" in names[0].message


def test_a_stale_device_keeping_a_name_the_dcim_reused_points_at_allow_deletes():
    existing = built()
    imported = built()
    # The DCIM deleted device 1001 and created 1004 with its name.
    rack = imported["racks"][0]
    old = next(p for p in rack["placements"] if p["id"] == "nb-dev-1001")
    rack["placements"].remove(old)
    imported["powerLinks"] = []
    # A new NetBox device has new component ids too.
    rack["placements"].append(old | {"id": "nb-dev-1004", "startU": 20, "ports": [], "powerInlets": []})
    result = merge(existing, imported)
    names = [c for c in result.diff.conflicts if c.kind == "device_name"]
    assert names and "--allow-deletes" in names[0].message
    assert not [c for c in merge(existing, imported, allow_deletes=True).diff.conflicts if c.kind == "device_name"]

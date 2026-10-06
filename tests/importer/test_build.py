"""build_project: the LDN1 site mapped onto a Railyard project, and the limits it must respect."""

from __future__ import annotations

import copy
import random

import ldn1
import pytest
from importer_helpers import build

from railyard_sync.dcim.snapshot import (
    Cable,
    Component,
    ComponentTemplate,
    Device,
    DeviceType,
    Rack,
    Site,
    Snapshot,
    Termination,
)
from railyard_sync.importer import BuildResult, build_project

# No conftest here: tests/test_client.py imports the top-level conftest by name.


@pytest.fixture
def ldn1_result() -> BuildResult:
    """LDN1 built with a catalogue that knows its leaf switch."""
    return build(ldn1.snapshot(), catalogue=ldn1.FakeCatalogue())


@pytest.fixture
def ldn1_project(ldn1_result) -> dict:
    return ldn1_result.project


def _racks(project) -> dict[str, dict]:
    return {rack["id"]: rack for rack in project["racks"]}


def _placements(project) -> dict[str, dict]:
    return {pl["id"]: pl for rack in project["racks"] for pl in rack["placements"]}


def _skipped(report, kind) -> dict[str, str]:
    return {item.id: item.reason for item in report.skipped if item.kind == kind}


# ---- spaces --------------------------------------------------------------------------------------


def test_regions_site_and_locations_become_containers_with_their_records(ldn1_project):
    assert ldn1_project["containers"] == [
        {"id": "nb-region-1", "name": "Europe", "type": "Region", "layout": "group"},
        {"id": "nb-region-2", "name": "UK", "type": "Region", "parentId": "nb-region-1", "layout": "group"},
        {
            "id": "nb-site-10",
            "name": "LDN1",
            "type": "Site",
            "parentId": "nb-region-2",
            "layout": "floor",
            "status": "Active",
            "facility": "Equinix LD5",
            "exportSite": True,
        },
        {
            "id": "nb-loc-20",
            "name": "Building A",
            "type": "Location",
            "parentId": "nb-site-10",
            "layout": "group",
            "status": "Active",
            "facility": "LD5-A",
        },
        {
            "id": "nb-loc-21",
            "name": "Hall 1",
            "type": "Location",
            "parentId": "nb-loc-20",
            "layout": "group",
            "status": "Active",
        },
    ]
    # Every group container has a locations record, every floor a data centre under the nearest one.
    assert [loc["id"] for loc in ldn1_project["locations"]] == ["nb-region-1", "nb-region-2", "nb-loc-20", "nb-loc-21"]
    assert ldn1_project["locations"][2] == {
        "id": "nb-loc-20",
        "name": "Building A",
        "facility": "LD5-A",
        "status": "Active",
    }
    assert ldn1_project["dataCentres"] == [
        {"id": "nb-site-10", "name": "LDN1", "locationId": "nb-region-2", "status": "Active"}
    ]
    assert ldn1_project["rows"] == []


def test_unrelated_regions_are_left_out(ldn1_project):
    assert "nb-region-3" not in {c["id"] for c in ldn1_project["containers"]}


# ---- racks ---------------------------------------------------------------------------------------


def test_racks_keep_their_dimensions_numbering_and_space(ldn1_project):
    racks = _racks(ldn1_project)
    a01 = racks["nb-rack-100"]
    assert {k: v for k, v in a01.items() if k != "placements"} == {
        "id": "nb-rack-100",
        "name": "A01",
        "uHeight": 42,
        "widthMm": 600,
        "depthMm": 1070,
        "startingUnit": 1,
        "containerId": "nb-loc-21",
        "dcId": "nb-site-10",
        "rackTypeKey": "nb-rt-apc-ar3100",
        "indexInRow": 0,
        "status": "Active",
        "role": "Network",
        "powerCapacityW": 5888,  # Feed A only: 230 V x 32 A x 80 %; the redundant Feed B is left out
        "maxLoadKg": 1000,
        "notes": "Fabric rack",
        "tags": ["core"],
    }
    a02 = racks["nb-rack-101"]
    assert (a02["widthMm"], a02["uHeight"], a02["status"]) == (800, 47, "Planned")  # 23" rail, no outer width
    assert a02["powerCapacityW"] == 8868  # three-phase: 400 V x 16 A x sqrt 3 x 80 %
    assert racks["nb-rack-102"]["descendingUnits"] is True
    assert all(":" not in rack_id for rack_id in racks)


def test_rack_types(ldn1_project):
    assert ldn1_project["rackTypes"] == [
        {
            "key": "nb-rt-apc-ar3100",
            "manufacturer": "APC",
            "model": "NetShelter SX 42U",
            "uHeight": 42,
            "widthMm": 600,
            "depthMm": 1070,
            "formFactor": "4-post-cabinet",
        }
    ]


def test_duplicate_rack_name_in_a_space_is_renamed_and_reported(ldn1_result):
    assert _racks(ldn1_result.project)["nb-rack-102"]["name"] == "a01 (102)"
    assert any("renamed 'a01 (102)'" in w for w in ldn1_result.report.warnings)


# ---- devices -------------------------------------------------------------------------------------


def test_racked_device_becomes_a_manual_placement_with_its_real_components(ldn1_project):
    leaf = _placements(ldn1_project)["nb-dev-1000"]
    assert {k: v for k, v in leaf.items() if k not in ("ports", "powerInlets")} == {
        "id": "nb-dev-1000",
        "startU": 40,
        "heightU": 1,
        "face": "full",  # full-depth type
        "label": "ldn1-leaf-01",
        "namingMode": "manual",
        "serial": "FDO12345678",
        "deviceTypeRef": "cisco-nexus-93180yc-fx",
        "role": "Leaf switch",
        "tags": ["fabric"],  # de-duplicated, blanks dropped
    }
    ports = {p["id"]: p for p in leaf["ports"]}
    assert ports["nb-if-101"] == {
        "id": "nb-if-101",
        "name": "Ethernet1/1",
        "side": "front",
        "kind": "interface",
        "origin": "template",
        "connector": "SFP28",
        "templateIndex": 1,
    }
    assert ports["nb-if-149"]["connector"] == "QSFP28"
    # A module's interface is a module port, its 10GBASE-T type kept for the export.
    assert ports["nb-if-151"] == {
        "id": "nb-if-151",
        "name": "Ethernet2/1",
        "side": "front",
        "kind": "interface",
        "origin": "module",
        "connector": "10gbase-t",
        "module": "Slot 2",
    }
    assert [p["name"] for p in leaf["ports"]] == [
        "Ethernet1/1",
        "Ethernet1/2",
        "Ethernet1/3",
        "Ethernet1/4",
        "Ethernet1/49",
        "Ethernet2/1",
        "mgmt0",
    ]
    assert leaf["powerInlets"] == [
        {"id": "nb-pp-31", "name": "PSU1", "connector": "C14", "origin": "template"},
        {"id": "nb-pp-32", "name": "PSU2", "connector": "C14", "origin": "template"},
    ]


def test_patch_panel_front_ports_couple_to_their_rear_port(ldn1_project):
    panel = _placements(ldn1_project)["nb-dev-1001"]
    assert panel["face"] == "front"
    assert panel["ports"][0] == {
        "id": "nb-fp-61",
        "name": "Front 1",
        "side": "front",
        "kind": "front-port",
        "origin": "template",
        "connector": "LC",
        "templateIndex": 1,
        "peerId": "nb-rp-71",
    }
    assert panel["ports"][3]["peerPosition"] == 4
    assert panel["ports"][4] == {
        "id": "nb-rp-71",
        "name": "Rear 1",
        "side": "rear",
        "kind": "rear-port",
        "origin": "template",
        "connector": "MPO",
        "templateIndex": 1,
    }
    assert panel["powerInlets"] == []  # explicit: the panel has no power ports


def test_virtual_interfaces_and_console_ports_are_reported_not_imported(ldn1_result):
    server = _placements(ldn1_result.project)["nb-dev-1002"]
    assert [p["name"] for p in server["ports"]] == ["iDRAC", "NIC1", "NIC2"]
    assert _skipped(ldn1_result.report, "ports")["204"] == "virtual or wireless interface; it takes no cable"
    assert _skipped(ldn1_result.report, "consolePorts") == {"9": "console ports are not modelled"}


def test_long_serial_is_truncated_and_reported(ldn1_result):
    server = _placements(ldn1_result.project)["nb-dev-1002"]
    assert server["serial"] == "X" * 50
    assert server["face"] == "full"  # a rear-mounted full-depth device spans both faces
    assert server["notes"] == "Rebuilt after RMA."
    assert any("serial" in w and "truncated" in w for w in ldn1_result.report.warnings)


def test_zero_u_devices_are_side_mounted_alternately(ldn1_project):
    placements = _placements(ldn1_project)
    pdu_a, pdu_b = placements["nb-dev-1003"], placements["nb-dev-1004"]
    assert (pdu_a["mount"], pdu_a["side"], pdu_a["startU"], pdu_a["heightU"], pdu_a["face"]) == (
        "zeroU",
        "left",
        1,
        1,
        "front",
    )
    assert (pdu_b["mount"], pdu_b["side"]) == ("zeroU", "right")
    assert pdu_a["ports"] == []
    assert pdu_a["powerInlets"] == [
        {"id": "nb-pp-500", "name": "Input", "connector": "iec-60309-p-n-e-6h", "origin": "template"}
    ]


def test_fractional_u_is_rounded_to_the_units_it_touches_and_reported(ldn1_result):
    shelf = _placements(ldn1_result.project)["nb-dev-1005"]
    assert (shelf["startU"], shelf["heightU"]) == (20, 2)  # U20.5 + 1.5U touches U20-U21
    pi_shelf = next(dt for dt in ldn1_result.project["catalogue"] if dt["key"] == "nb-dt-pi-shelf")
    assert pi_shelf["uHeight"] == 2
    assert any("U20.5" in w for w in ldn1_result.report.warnings)
    assert any("1.5U is not a whole number" in w for w in ldn1_result.report.warnings)


def test_unmodelled_devices_are_skipped_with_reasons(ldn1_result):
    skipped = _skipped(ldn1_result.report, "devices")
    assert skipped == {
        "1007": "installed in a device bay of ldn1-chassis-01; child devices are not modelled",
        "1008": "not in a rack; unracked devices are not modelled",
        "1009": "overlaps ldn1-srv-01 in rack A01 at U10-U10",
    }
    assert ldn1_result.report.counts()["devices"] == {"imported": 8, "skipped": 3}
    assert set(_placements(ldn1_result.project)) == {
        f"nb-dev-{n}" for n in (1000, 1001, 1002, 1003, 1004, 1005, 1006, 1010)
    }


def test_non_active_device_status_goes_to_meta(ldn1_project):
    assert ldn1_project["meta"]["railyardSync"]["deviceStatus"] == {"nb-dev-1002": "planned"}


# ---- device types --------------------------------------------------------------------------------


def test_catalogue_match_is_used_with_its_provenance(ldn1_result):
    leaf = ldn1_result.project["catalogue"][0]
    assert leaf["key"] == "cisco-nexus-93180yc-fx"
    assert leaf["source"] == {"kind": "catalogue", "ref": "a" * 64, "revision": "2026.10.1"}
    assert ldn1_result.report.imported["catalogueDeviceTypes"] == 1
    assert ldn1_result.report.imported["customDeviceTypes"] == 5


def test_custom_types_are_built_from_component_templates(ldn1_project):
    types = {dt["key"]: dt for dt in ldn1_project["catalogue"]}
    assert [dt["key"] for dt in ldn1_project["catalogue"]] == sorted(types)
    assert types["nb-dt-dell-poweredge-r650"] == {
        "key": "nb-dt-dell-poweredge-r650",
        "manufacturer": "Dell",
        "model": "PowerEdge R650",
        "uHeight": 1,
        "fullDepth": True,
        "powerW": 400,
        "powerInlets": [{"name": "PSU1", "connector": "C14"}, {"name": "PSU2", "connector": "C14"}],
        "ports": {
            "front": 3,
            "passThrough": False,
            "media": "SFP+",
            "frontPorts": [
                {"name": "NIC1", "type": "SFP+"},
                {"name": "NIC2", "type": "SFP+"},
                {"name": "iDRAC", "type": "RJ45"},
            ],
        },
        "weightKg": 19.9,
        "airflow": "front-to-rear",
        "source": {"kind": "custom"},
    }
    panel = types["nb-dt-panduit-lc-4"]["ports"]
    assert (panel["front"], panel["rear"], panel["passThrough"], panel["media"]) == (4, 1, True, "LC")
    assert panel["frontPorts"][1] == {
        "name": "Front 2",
        "type": "LC",
        "netboxType": "lc-apc",
        "rear": "Rear 1",
        "rearPos": 2,
    }
    assert panel["rearPorts"] == [{"name": "Rear 1", "type": "MPO"}]
    pdu = types["nb-dt-apc-ap8868"]
    assert pdu["outlets"] == {"count": 12, "type": "C13/C19", "capacityW": 7400}
    assert "powerW" not in pdu  # a PDU's draw is throughput, not consumption
    assert "nb-dt-hpe-bl460c" not in types  # the blade was skipped, so its type is not pulled in


def test_without_a_catalogue_every_type_is_custom():
    result = build(ldn1.snapshot())
    leaf = next(dt for dt in result.project["catalogue"] if dt["key"] == "nb-dt-cisco-nexus-93180yc-fx")
    assert leaf["source"] == {"kind": "custom"}
    assert leaf["ports"]["frontPorts"][0] == {"name": "Ethernet1/1", "type": "SFP28"}
    assert leaf["ports"]["media"] == "SFP28"  # the data interfaces' connector, not mgmt0's
    assert _placements(result.project)["nb-dev-1000"]["deviceTypeRef"] == "nb-dt-cisco-nexus-93180yc-fx"


def test_catalogue_is_asked_once_per_type():
    catalogue = ldn1.FakeCatalogue()
    build(ldn1.snapshot(), catalogue=catalogue)
    slugs = [call[0] for call in catalogue.calls]
    assert sorted(slugs) == sorted(set(slugs))
    assert ("cisco-nexus-93180yc-fx", "Cisco", "Nexus 93180YC-FX") in catalogue.calls


# ---- cables and power ----------------------------------------------------------------------------


def test_data_cables_between_imported_ports(ldn1_project):
    assert ldn1_project["cables"] == [
        {
            "id": "nb-cable-5000",
            "a": {"rackId": "nb-rack-100", "placementId": "nb-dev-1000", "portId": "nb-if-101"},
            "b": {"rackId": "nb-rack-100", "placementId": "nb-dev-1002", "portId": "nb-if-201"},
            "media": "DAC",
            "colour": "#ff0000",
            "kind": "patch",
        },
        {
            "id": "nb-cable-5001",
            "a": {"rackId": "nb-rack-100", "placementId": "nb-dev-1000", "portId": "nb-if-149"},
            "b": {"rackId": "nb-rack-100", "placementId": "nb-dev-1001", "portId": "nb-fp-61"},
            "media": "OS2",
            "label": "LDN-0001",
            "kind": "patch",
        },
        {
            "id": "nb-cable-5008",
            "a": {"rackId": "nb-rack-100", "placementId": "nb-dev-1000", "portId": "nb-if-151"},
            "b": {"rackId": "nb-rack-102", "placementId": "nb-dev-1010", "portId": "nb-if-403"},
            "media": "Cat6A",
            "kind": "patch",
        },
    ]
    assert ldn1_project["meta"]["railyardSync"]["unmodelled"]["cableStatus"] == {"nb-cable-5008": "planned"}


def test_power_cables_become_links_to_the_outlet_in_name_order(ldn1_project):
    assert ldn1_project["powerLinks"] == [
        {
            "id": "nb-power-5003",
            "device": {"rackId": "nb-rack-100", "placementId": "nb-dev-1002", "inletId": "nb-pp-205"},
            "pdu": {"rackId": "nb-rack-100", "placementId": "nb-dev-1003", "outlet": 10},  # "Outlet 10", not 2nd by id
        },
        {
            "id": "nb-power-5004",
            "device": {"rackId": "nb-rack-100", "placementId": "nb-dev-1002", "inletId": "nb-pp-206"},
            "pdu": {"rackId": "nb-rack-100", "placementId": "nb-dev-1004", "outlet": 1},
        },
    ]


def test_unmodelled_cables_are_skipped_with_reasons(ldn1_result):
    assert _skipped(ldn1_result.report, "cables") == {
        "5002": "one end is a circuit (circuits are not modelled)",
        "5006": "its device ldn1-srv-02 is not imported",
        "5007": "2 terminations on one end; Railyard cables are point to point",
        "5009": "one end is a console port (console cabling is not modelled)",
        "5010": "one end is outside the imported sites",
    }
    assert _skipped(ldn1_result.report, "powerLinks") == {
        "5005": "one end is a power feed (power feeds are not modelled; they set the rack's power capacity)"
    }


# ---- meta and report -----------------------------------------------------------------------------


def test_meta_records_the_source_and_what_was_not_modelled(ldn1_result):
    meta = ldn1_result.project["meta"]["railyardSync"]
    assert {k: meta[k] for k in ("source", "url", "version", "sites", "importedAt", "prefix")} == {
        "source": "netbox",
        "url": "https://netbox.example.com",
        "version": "4.3.7",
        "sites": [{"id": "10", "slug": "ldn1", "name": "LDN1"}],
        "importedAt": "2026-10-06T12:00:00Z",
        "prefix": "nb",
    }
    unmodelled = meta["unmodelled"]
    assert unmodelled["powerPanels"] == [{"id": "1", "name": "PP-A", "site_id": "10", "location_id": "20"}]
    feeds = {feed["id"]: feed for feed in unmodelled["powerFeeds"]}
    assert feeds["1"]["railyardRackId"] == "nb-rack-100"
    assert feeds["1"]["availablePowerW"] == 5888.0
    assert feeds["2"]["type"] == "redundant"
    assert unmodelled["moduleComponents"] == {"nb-dev-1000": 1}
    assert len(unmodelled["skipped"]) == len(ldn1_result.report.skipped)
    assert unmodelled is ldn1_result.report.unmodelled
    assert ldn1_result.report.warnings[0] == "cable 5010: far end interface 99999 is outside the loaded sites"


def test_report_counts_and_summary(ldn1_result):
    counts = ldn1_result.report.counts()
    assert counts["racks"] == {"imported": 3, "skipped": 0}
    assert counts["cables"] == {"imported": 3, "skipped": 5}
    assert counts["powerLinks"] == {"imported": 2, "skipped": 1}
    assert counts["ports"]["imported"] == 18
    lines = ldn1_result.report.summary_lines()
    assert "devices: 8 imported, 3 skipped" in lines
    assert ldn1_result.report.to_dict()["counts"] == counts


def test_output_is_deterministic_whatever_the_snapshot_order(ldn1_project):
    shuffled = ldn1.snapshot()
    rng = random.Random(7)
    for name in (
        "regions",
        "sites",
        "locations",
        "racks",
        "device_types",
        "devices",
        "components",
        "cables",
        "power_feeds",
    ):
        items = getattr(shuffled, name)
        rng.shuffle(items)
    again = build(shuffled, catalogue=ldn1.FakeCatalogue()).project
    assert again == ldn1_project


def test_nautobot_prefix():
    project = build(ldn1.snapshot(), prefix="nbt").project
    assert project["racks"][0]["id"] == "nbt-rack-100"
    assert project["meta"]["railyardSync"]["prefix"] == "nbt"


@pytest.mark.parametrize("bad", ["", "a:b", "x" * 40, "-nb"])
def test_invalid_prefix_is_refused(bad):
    with pytest.raises(ValueError):
        build_project(ldn1.snapshot(), project_id="p", name="n", prefix=bad)


# ---- limits Railyard enforces and NetBox does not ------------------------------------------------


def _site_snapshot(**kwargs) -> Snapshot:
    """One site, one 42U rack (id 1) and the server type, plus whatever the test adds."""
    snapshot = Snapshot(source="netbox", sites=[Site("1", "S1", "s1")], racks=[Rack("1", "R1", site_id="1")])
    snapshot.device_types = [copy.deepcopy(ldn1.device_types()[2]), copy.deepcopy(ldn1.device_types()[3])]
    for key, value in kwargs.items():
        getattr(snapshot, key).extend(value)
    return snapshot


def test_device_beyond_the_rack_top_is_skipped():
    result = build(_site_snapshot(devices=[Device("1", "high", "3", "1", rack_id="1", position=42.5, face="front")]))
    assert _skipped(result.report, "devices") == {"1": "U42-U43 does not fit rack R1 (U1-U42)"}


def test_half_u_neighbours_that_overlap_after_rounding_keep_the_first():
    half = DeviceType("9", "Generic", "Half", "half", u_height=0.5, is_full_depth=False)
    snapshot = _site_snapshot(
        device_types=[half],
        devices=[
            Device("2", "upper", "9", "1", rack_id="1", position=5.5, face="front"),
            Device("1", "lower", "9", "1", rack_id="1", position=5, face="front"),
            Device("3", "behind", "9", "1", rack_id="1", position=5, face="rear"),
        ],
    )
    result = build(snapshot)
    assert _skipped(result.report, "devices") == {"2": "overlaps lower in rack R1 at U5-U5"}
    assert {pl["label"]: pl["face"] for pl in result.project["racks"][0]["placements"]} == {
        "lower": "front",
        "behind": "rear",
    }


def test_device_in_a_rack_without_a_position_is_skipped():
    result = build(_site_snapshot(devices=[Device("1", "loose", "3", "1", rack_id="1")]))
    assert _skipped(result.report, "devices") == {"1": "in rack R1 without a position"}


def test_long_names_tags_and_notes_are_fitted_and_reported():
    long_name = "rack-" + "x" * 300
    snapshot = _site_snapshot()
    snapshot.racks[0] = Rack(
        "1", long_name, site_id="1", tags=[f"tag-{n}" for n in range(70)] + ["t" * 120], comments="n" * 10_050
    )
    snapshot.devices.append(Device("1", "d" * 300, "3", "1", rack_id="1", position=1, face="front"))
    result = build(snapshot)
    rack = result.project["racks"][0]
    assert len(rack["name"]) == 256 and rack["name"].startswith("rack-xxx") and "~" in rack["name"]
    assert len(rack["tags"]) == 64
    assert len(rack["notes"]) == 10_000
    assert len(rack["placements"][0]["label"]) == 256
    warnings = "\n".join(result.report.warnings)
    assert "shortened to 256 characters" in warnings
    assert "only the first 64 of 71 tags" in warnings
    assert "comments shortened to 10000" in warnings


def test_ids_that_would_break_railyard_rules_are_digested():
    snapshot = _site_snapshot()
    snapshot.racks[0] = Rack("a:b", "R1", site_id="1")
    project = build(snapshot).project
    rack_id = project["racks"][0]["id"]
    assert ":" not in rack_id and rack_id.startswith("nb-rack-x")
    assert build(snapshot).project["racks"][0]["id"] == rack_id  # still deterministic


def test_duplicate_port_and_inlet_names_are_made_unique():
    snapshot = _site_snapshot(
        devices=[Device("1", "srv", "3", "1", rack_id="1", position=1, face="front")],
        components=[
            Component("1", "1", "eth0", "interface", "1000base-t"),
            Component("2", "1", "ETH0", "interface", "1000base-t"),
            Component("3", "1", "PSU", "power-port", "iec-60320-c14"),
            Component("4", "1", "psu ", "power-port", "iec-60320-c14"),
        ],
    )
    result = build(snapshot)
    placement = result.project["racks"][0]["placements"][0]
    assert [p["name"] for p in placement["ports"]] == ["eth0", "ETH0 (2)"]
    assert [i["name"] for i in placement["powerInlets"]] == ["PSU", "psu (4)"]


def test_front_port_without_its_rear_port_is_skipped():
    snapshot = _site_snapshot(
        devices=[Device("1", "panel", "3", "1", rack_id="1", position=1, face="front")],
        components=[Component("1", "1", "F1", "front-port", "lc", rear_port_id="99")],
    )
    result = build(snapshot)
    assert result.project["racks"][0]["placements"][0]["ports"] == []
    assert _skipped(result.report, "ports") == {"1": "front port without a rear port on its device"}


def _power_snapshot(*cables: Cable, devices=(), components=()) -> Snapshot:
    """Rack 1 with a server (device 1, PSU1 = power port 11) and a 12-outlet PDU (device 2)."""
    pdu_outlets = [Component(str(100 + n), "2", f"Outlet {n}", "power-outlet", "iec-60320-c13") for n in range(1, 13)]
    return _site_snapshot(
        devices=[
            Device("1", "srv", "3", "1", rack_id="1", position=1, face="front"),
            Device("2", "pdu", "4", "1", rack_id="1"),
            *devices,
        ],
        components=[
            Component("11", "1", "PSU1", "power-port", "iec-60320-c14"),
            Component("21", "2", "Input", "power-port", "iec-60309-p-n-e-6h"),
            *pdu_outlets,
            *components,
        ],
        cables=list(cables),
    )


def _power_cable(cable_id, port, outlet) -> Cable:
    return Cable(cable_id, [Termination("dcim.powerport", port)], [Termination("dcim.poweroutlet", outlet)])


def test_pdu_fed_from_another_pdu_is_refused():
    other = Device("3", "pdu-2", "4", "1", rack_id="1")
    snapshot = _power_snapshot(
        _power_cable("1", "31", "105"),
        devices=[other],
        components=[Component("31", "3", "Input", "power-port", "iec-60309-p-n-e-6h")],
    )
    result = build(snapshot)
    assert result.project["powerLinks"] == []
    assert "itself a PDU" in _skipped(result.report, "powerLinks")["1"]


def test_outlet_beyond_the_pdu_types_outlets_is_refused():
    snapshot = _power_snapshot(
        _power_cable("1", "11", "113"), components=[Component("113", "2", "Outlet 13", "power-outlet", "iec-60320-c13")]
    )
    result = build(snapshot)
    assert result.project["powerLinks"] == []
    assert _skipped(result.report, "powerLinks") == {"1": "outlet 'Outlet 13' is beyond the PDU type's 12 outlets"}


def test_power_port_to_power_port_is_refused():
    snapshot = _power_snapshot(Cable("1", [Termination("dcim.powerport", "11")], [Termination("dcim.powerport", "21")]))
    result = build(snapshot)
    assert result.project["powerLinks"] == []
    assert "connects two power-ports" in _skipped(result.report, "powerLinks")["1"]


def test_cable_between_two_ports_of_one_device_is_refused():
    snapshot = _site_snapshot(
        devices=[Device("1", "srv", "3", "1", rack_id="1", position=1, face="front")],
        components=[
            Component("1", "1", "a", "interface", "1000base-t"),
            Component("2", "1", "b", "interface", "1000base-t"),
        ],
        cables=[Cable("1", [Termination("dcim.interface", "1")], [Termination("dcim.interface", "2")])],
    )
    result = build(snapshot)
    assert result.project["cables"] == []
    assert "one device" in _skipped(result.report, "cables")["1"]


def test_device_types_sharing_a_slug_get_distinct_keys():
    other = DeviceType("8", "Other", "R650 clone", "dell-poweredge-r650", u_height=1, components=[])
    snapshot = _site_snapshot(
        device_types=[other],
        devices=[
            Device("1", "a", "3", "1", rack_id="1", position=1, face="front"),
            Device("2", "b", "8", "1", rack_id="1", position=2, face="front"),
        ],
    )
    project = build(snapshot).project
    refs = [pl["deviceTypeRef"] for pl in project["racks"][0]["placements"]]
    assert refs == ["nb-dt-dell-poweredge-r650", "nb-dt-dell-poweredge-r650-8"]


def test_patch_panel_type_with_an_unmapped_front_port_keeps_its_device_ports():
    panel = DeviceType(
        "9",
        "Generic",
        "Odd panel",
        "odd-panel",
        components=[
            ComponentTemplate("F1", "front-port", "lc", rear_port_name="R1"),
            ComponentTemplate("F2", "front-port", "lc", rear_port_name="missing"),
            ComponentTemplate("R1", "rear-port", "lc"),
        ],
    )
    snapshot = _site_snapshot(
        device_types=[panel],
        devices=[Device("1", "odd", "9", "1", rack_id="1", position=1, face="front")],
        components=[
            Component("2", "1", "R1", "rear-port", "lc"),
            Component("1", "1", "F1", "front-port", "lc", rear_port_id="2"),
        ],
    )
    result = build(snapshot)
    odd = next(dt for dt in result.project["catalogue"] if dt["key"] == "nb-dt-odd-panel")
    assert "ports" not in odd
    assert [p["origin"] for p in result.project["racks"][0]["placements"][0]["ports"]] == ["custom", "custom"]
    assert any("no rear port mapping" in w for w in result.report.warnings)

"""The Nautobot loader against a fake Nautobot serving what a real Nautobot 2.4 returned. No network."""

from __future__ import annotations

import pytest
import requests
from conftest import FakeResponse, FakeSession
from fake_nautobot import (
    BASE,
    BUILDING_A,
    EUROPE,
    HALL_1,
    LDN1,
    MAN1,
    NEXT_HOST,
    TOKEN,
    UK,
    FakeNautobot,
)

from railyard_sync.dcim import nautobot
from railyard_sync.dcim.errors import DCIMAuthError, DCIMConnectionError, DCIMNotFoundError, DCIMVersionError
from railyard_sync.dcim.nautobot import NautobotLoader, load_nautobot_snapshot, status_slug
from railyard_sync.dcim.snapshot import Snapshot, Termination


def load(nb: FakeNautobot, locations=("LDN1",), **kw) -> Snapshot:
    return NautobotLoader(BASE, nb.token, session=nb.session, **kw).load(
        list(locations) if locations is not None else None
    )


def named(items, name):
    return next(i for i in items if i.name == name)


@pytest.fixture
def nb() -> FakeNautobot:
    return FakeNautobot()


@pytest.fixture
def snap(nb) -> Snapshot:
    return load(nb)


# ---- the location tree ------------------------------------------------------------------------


def test_source_and_version(snap):
    assert (snap.source, snap.source_url, snap.source_version) == ("nautobot", BASE, "2.4.43")


def test_locations_above_the_import_are_its_regions_outermost_first(snap):
    assert [(r.id, r.name, r.parent_id, r.location_type) for r in snap.regions] == [
        (EUROPE, "Europe", None, "Region"),
        (UK, "United Kingdom", EUROPE, "Region"),
    ]
    assert [r.slug for r in snap.regions] == ["europe", "united-kingdom"]


def test_the_imported_location_is_the_site(snap):
    [site] = snap.sites
    assert (site.id, site.name, site.slug, site.location_type) == (LDN1, "LDN1", "ldn1", "Site")
    assert (site.status, site.facility, site.description, site.region_id) == ("active", "Equinix LD8", "London one", UK)
    assert site.tags == ["production"]


def test_locations_below_it_nest_under_the_site(snap):
    assert [(loc.id, loc.name, loc.parent_id, loc.site_id, loc.location_type) for loc in snap.locations] == [
        (BUILDING_A, "Building A", None, LDN1, "Building"),
        (HALL_1, "Data Hall 1", BUILDING_A, LDN1, "Room"),
    ]
    hall = named(snap.locations, "Data Hall 1")
    assert (hall.status, hall.facility) == ("planned", "DH1")


# ---- racks and devices ------------------------------------------------------------------------


def test_racks_with_units_converted(snap):
    assert sorted(r.name for r in snap.racks) == ["A01", "A02", "B01"]  # not MAN1's M01
    a01, a02, b01 = (named(snap.racks, n) for n in ("A01", "A02", "B01"))
    assert (a01.site_id, a01.location_id, a01.role, a01.status, a01.form_factor) == (
        LDN1,
        HALL_1,
        "Network",
        "active",
        "4-post-cabinet",
    )
    assert (a01.outer_width_mm, a01.outer_depth_mm) == (600, 1070)
    assert (a01.serial, a01.asset_tag, a01.facility_id, a01.comments) == (
        "AR3300-0001",
        "LDN1-R-A01",
        "LD8-0401-A01",
        "Row A rack 1",
    )
    assert a01.tags == ["production"] and a01.custom_fields == {"support_contract": "RK-1"}
    assert a02.outer_width_mm == pytest.approx(609.6) and a02.outer_depth_mm == pytest.approx(1066.8)
    assert a02.desc_units is True and a02.role == ""
    assert (b01.location_id, b01.width_in, b01.u_height, b01.status, b01.form_factor) == (
        BUILDING_A,
        23,
        45,
        "planned",
        "2-post-frame",
    )
    assert (b01.outer_width_mm, b01.max_weight_kg, b01.rack_type_id) == (None, None, None)


def test_devices(nb, snap):
    assert sorted(d.name for d in snap.devices) == [
        "ldn1-blade1",
        "ldn1-chassis",
        "ldn1-leaf1",
        "ldn1-oob",
        "ldn1-pdu-a01",
        "ldn1-pp1",
        "ldn1-srv1",
    ]
    leaf = named(snap.devices, "ldn1-leaf1")
    a01 = named(snap.racks, "A01")
    assert (leaf.rack_id, leaf.position, leaf.face, leaf.location_id, leaf.site_id) == (
        a01.id,
        40,
        "front",
        HALL_1,
        LDN1,
    )
    assert (leaf.role, leaf.status, leaf.tenant, leaf.serial, leaf.asset_tag) == (
        "Leaf switch",
        "active",
        "Acme Ltd",
        "JPE21130456",
        "LDN1-0040",
    )
    assert sorted(leaf.tags) == ["core", "production"] and leaf.custom_fields == {"support_contract": "SC-1001"}
    assert leaf.comments == "Leaf for row A."
    assert named(snap.devices, "ldn1-srv1").face == "rear"
    assert named(snap.devices, "ldn1-srv1").status == "planned"

    pdu = named(snap.devices, "ldn1-pdu-a01")  # 0U: racked, no position or face
    assert (pdu.rack_id, pdu.position, pdu.face) == (a01.id, None, "")
    blade = named(snap.devices, "ldn1-blade1")  # in a device bay of the chassis
    assert blade.parent_device_id == nb.named("devices", "ldn1-chassis")["id"]
    oob = named(snap.devices, "ldn1-oob")  # unracked, in the building
    assert (oob.rack_id, oob.location_id, oob.position) == (None, BUILDING_A, None)


def test_device_types_have_a_derived_slug_and_their_templates(snap):
    leaf = named(snap.devices, "ldn1-leaf1")
    switch = next(t for t in snap.device_types if t.id == leaf.device_type_id)
    assert (switch.manufacturer, switch.model, switch.slug, switch.part_number) == (
        "Arista",
        "DCS-7050SX3-48YC8",
        "arista-dcs-7050sx3-48yc8",
        "DCS-7050SX3-48YC8-R",
    )
    assert (switch.u_height, switch.is_full_depth, switch.weight_kg) == (1, True, None)
    assert [(c.kind, c.name, c.type) for c in switch.components] == [
        ("interface", "Ethernet1", "25gbase-x-sfp28"),
        ("interface", "Ethernet2", "25gbase-x-sfp28"),
        ("interface", "Management1", "1000base-t"),
        ("power-port", "PSU1", "iec-60320-c14"),
        ("power-port", "PSU2", "iec-60320-c14"),
    ]
    assert next(c for c in switch.components if c.name == "Management1").mgmt_only is True
    assert next(c for c in switch.components if c.name == "PSU1").maximum_draw_w == 300

    panel = next(t for t in snap.device_types if t.model == "24-port LC panel")
    fronts = [(c.name, c.rear_port_name, c.rear_port_position) for c in panel.components if c.kind == "front-port"]
    assert sorted(fronts) == [("F1", "R1", 1), ("F2", "R2", 1)]
    pdu = next(t for t in snap.device_types if t.model == "AP8868")
    assert (pdu.u_height, pdu.is_full_depth) == (0, False)
    assert {c.feed_leg for c in pdu.components if c.kind == "power-outlet"} == {"A"}
    chassis = next(t for t in snap.device_types if t.model == "MX7000")
    assert chassis.subdevice_role == "parent"


def test_only_the_device_types_in_use_are_read_with_their_templates_at_depth_0(nb, snap):
    [call] = nb.list_calls("device-types")
    assert {v for k, v in call["params"] if k == "id"} == {d.device_type_id for d in snap.devices}
    for call in nb.list_calls("interface-templates"):
        assert ("depth", 0) not in call["params"] and not any(k == "depth" for k, _ in call["params"])


def test_components_of_the_loaded_devices(nb, snap):
    devices = {d.id: d.name for d in snap.devices}
    by_kind = {}
    for c in snap.components:
        by_kind.setdefault(c.kind, []).append((devices[c.device_id], c.name))
    assert sorted(by_kind["interface"]) == [
        ("ldn1-leaf1", "Ethernet1"),
        ("ldn1-leaf1", "Ethernet2"),
        ("ldn1-leaf1", "Management1"),
        ("ldn1-oob", "eno1"),
        ("ldn1-srv1", "eno1"),
    ]
    assert sorted(by_kind["console-port"]) == [("ldn1-leaf1", "Console")]
    front = next(c for c in snap.components if c.kind == "front-port" and c.name == "F1")
    rear = next(c for c in snap.components if c.kind == "rear-port" and c.name == "R1")
    assert (front.rear_port_id, front.rear_port_position) == (rear.id, 1)
    outlet = next(c for c in snap.components if c.kind == "power-outlet" and c.name == "Outlet 1")
    assert (outlet.type, outlet.feed_leg, outlet.power_port_id) == ("iec-60320-c13", "A", None)
    mgmt = next(c for c in snap.components if c.name == "Management1")
    assert (mgmt.mgmt_only, mgmt.enabled) == (True, True)


def test_power_panels_and_feeds(snap):
    [panel] = snap.power_panels
    assert (panel.name, panel.site_id, panel.location_id) == ("PP-A", LDN1, HALL_1)
    a, b = sorted(snap.power_feeds, key=lambda f: f.name)
    assert (a.type, a.phase, a.voltage, a.amperage, a.max_utilization) == ("primary", "three-phase", 400, 32, 80)
    assert a.rack_id == named(snap.racks, "A01").id and a.power_panel_id == panel.id
    assert round(a.available_power_w) == 17736  # what Nautobot reports as available_power
    assert (b.type, b.status) == ("redundant", "active")


def test_cables_with_their_terminations_and_units(nb, snap):
    by_label = {c.label: c for c in snap.cables}
    assert set(by_label) == {"L1", "L2", "WAN", "Uplink", ""}
    assert len(snap.cables) == 6  # the two power cables have no label
    l1 = by_label["L1"]
    leaf_e1 = next(
        c for c in snap.components if c.name == "Ethernet1" and c.device_id == named(snap.devices, "ldn1-leaf1").id
    )
    assert l1.a == [Termination("dcim.interface", leaf_e1.id)]
    assert l1.b[0].object_type == "dcim.frontport"
    assert (l1.type, l1.status, l1.color, l1.length_m, l1.tags) == ("mmf-om4", "connected", "ff0000", 3, ["production"])
    assert (by_label["L2"].status, by_label["L2"].length_m) == ("planned", 1.5)
    assert by_label["Uplink"].b[0].object_type == "circuits.circuittermination"
    types = sorted((c.a[0].object_type, c.b[0].object_type) for c in snap.cables if not c.label)
    assert types == [("dcim.powerport", "dcim.powerfeed"), ("dcim.powerport", "dcim.poweroutlet")]


def test_a_cable_to_another_location_is_kept_with_a_warning(snap):
    [warning] = [w for w in snap.warnings if w.startswith("Cable ")]
    assert "(WAN)" in warning and "B end (dcim.interface" in warning and "outside the loaded locations" in warning


def test_reads_are_filtered_by_the_imported_locations_and_their_descendants(nb, snap):
    subtree = {LDN1, BUILDING_A, HALL_1}
    for endpoint, key in (("racks", "location"), ("devices", "location"), ("interfaces", "location")):
        [call] = nb.list_calls(endpoint)
        assert {v for k, v in call["params"] if k == key} == subtree, endpoint
    [call] = nb.list_calls("cables")
    assert {v for k, v in call["params"] if k == "location_id"} == subtree


def test_the_build_from_nautobot_keeps_the_tree_and_uuid_ids(snap):
    from importer.importer_helpers import build

    result = build(snap, prefix="nbt")
    project = result.project
    containers = {c["id"]: c for c in project["containers"]}
    site = containers[f"nbt-site-{LDN1}"]
    assert (site["type"], site["layout"], site["parentId"]) == ("Site", "floor", f"nbt-region-{UK}")
    assert containers[f"nbt-region-{UK}"]["type"] == "Region"
    assert containers[f"nbt-loc-{BUILDING_A}"]["parentId"] == site["id"]
    hall = containers[f"nbt-loc-{HALL_1}"]
    assert (hall["type"], hall["parentId"], hall["status"]) == ("Room", f"nbt-loc-{BUILDING_A}", "Planned")
    racks = {r["name"]: r for r in project["racks"]}
    assert racks["A01"]["containerId"] == hall["id"] and racks["A01"]["dcId"] == site["id"]
    assert racks["A01"]["powerCapacityW"] == 17736  # the primary feed only
    leaf = next(p for r in project["racks"] for p in r["placements"] if p.get("label") == "ldn1-leaf1")
    assert leaf["id"].startswith("nbt-dev-") and len(leaf["id"]) == len("nbt-dev-") + 36
    assert all(len(port["id"]) <= 200 for port in leaf["ports"])
    assert {c["label"] for c in project["cables"]} == {"L1", "L2"}
    assert len(project["powerLinks"]) == 1
    assert project["meta"]["railyardSync"]["source"] == "nautobot"
    assert project["meta"]["railyardSync"]["prefix"] == "nbt"
    skipped = {item["name"] for item in project["meta"]["railyardSync"]["unmodelled"]["skipped"]}
    assert {"ldn1-blade1", "ldn1-oob"} <= skipped


# ---- choosing locations -----------------------------------------------------------------------


def test_all_locations_takes_the_first_type_that_holds_racks(nb):
    snap = load(nb, None)
    assert sorted(s.name for s in snap.sites) == ["LDN1", "MAN1"]
    assert {r.name for r in snap.regions} == {"Europe", "United Kingdom"}
    assert "M01" in {r.name for r in snap.racks}


def test_a_location_by_id_or_by_name_in_any_case(nb):
    assert [s.id for s in load(nb, [LDN1.upper()]).sites] == [LDN1]
    assert [s.id for s in load(nb, ["ldn1"]).sites] == [LDN1]


def test_several_locations_and_one_inside_another(nb):
    snap = load(nb, ["LDN1", "Data Hall 1", "MAN1"])
    assert [s.name for s in snap.sites] == ["LDN1", "MAN1"]
    assert any("Data Hall 1 is inside Europe → United Kingdom → LDN1" in w for w in snap.warnings)
    assert len({loc.id for loc in snap.locations}) == len(snap.locations)


def test_an_unknown_location(nb):
    with pytest.raises(DCIMNotFoundError, match="No Nautobot location is named 'nowhere'"):
        load(nb, ["nowhere"])
    with pytest.raises(DCIMNotFoundError, match="has the id"):
        load(nb, ["00000000-0000-4000-8000-000000000000"])


def test_an_ambiguous_name_asks_for_the_id(nb):
    clone = dict(nb.named("locations", "Data Hall 1"))
    clone["id"] = "aaaaaaaa-0000-4000-8000-000000000001"
    clone["parent"] = {"id": MAN1}
    nb.data["locations"].append(clone)
    with pytest.raises(DCIMNotFoundError) as err:
        load(nb, ["Data Hall 1"])
    message = str(err.value)
    assert "2 Nautobot locations are named 'Data Hall 1'" in message and "by its id" in message
    assert "Europe → United Kingdom → MAN1 → Data Hall 1 (aaaaaaaa-" in message


def test_at_least_one_location_is_required(nb):
    with pytest.raises(ValueError, match="at least one location"):
        NautobotLoader(BASE, TOKEN, session=nb.session).load([])


# ---- versions, errors, secrets ----------------------------------------------------------------


@pytest.mark.parametrize("version", ["1.6.20", "4.0.0"])
def test_unsupported_versions_are_refused(version):
    with pytest.raises(DCIMVersionError, match=f"Nautobot {version} is not supported"):
        load(FakeNautobot(version=version))


def test_a_newer_release_warns(nb):
    snap = load(FakeNautobot(version="3.2.6"))
    assert "newer than railyard-sync has been tested with (2.4)" in snap.warnings[0]


def test_an_unreadable_version(nb):
    nb.status["nautobot-version"] = "unknown"
    with pytest.raises(DCIMVersionError, match="Could not read the Nautobot version"):
        load(nb)


def test_pagination_stays_on_the_configured_host(nb):
    snap = load(nb, page_size=2)
    assert len(snap.devices) == 7
    assert all(not c["url"].startswith(NEXT_HOST) for c in nb.session.calls)
    assert len(nb.list_calls("devices")) > 1


def test_a_refused_token_is_never_echoed():
    nb = FakeNautobot()
    loader = NautobotLoader(BASE, "f" * 40, session=nb.session)
    with pytest.raises(DCIMAuthError) as err:
        loader.load(["LDN1"])
    message = str(err.value)
    assert "Nautobot did not accept the API token (HTTP 403)" in message and "NAUTOBOT_TOKEN" in message
    assert "f" * 40 not in message and "f" * 40 not in repr(loader)


def test_the_token_is_scrubbed_from_an_echoing_error():
    session = FakeSession(lambda *a: FakeResponse(500, {"detail": f"bad header Token {TOKEN}"}))
    with pytest.raises(Exception) as err:
        NautobotLoader(BASE, TOKEN, session=session).load(["LDN1"])
    assert TOKEN not in str(err.value) and "***" in str(err.value)
    assert "Nautobot failed (HTTP 500)" in str(err.value)


def test_an_unreachable_nautobot():
    def boom(*a):
        raise requests.ConnectionError(f"refused for {TOKEN}")

    with pytest.raises(DCIMConnectionError) as err:
        NautobotLoader(BASE, TOKEN, session=FakeSession(boom)).load(["LDN1"])
    assert "Could not reach Nautobot at https://nautobot.example.com" in str(err.value)
    assert "--nautobot-url" in str(err.value) and TOKEN not in str(err.value)


@pytest.mark.parametrize(
    ("url", "error"),
    [("", "URL is required"), ("ftp://x", "http:// or https://"), ("https://u:p@x", "must not contain credentials")],
)
def test_bad_urls(url, error):
    with pytest.raises(ValueError, match=error):
        NautobotLoader(url, TOKEN)


def test_the_api_root_is_accepted_as_the_url(nb):
    assert NautobotLoader(f"{BASE}/api/", TOKEN, session=nb.session).url == BASE


def test_convenience_function(nb):
    snap = load_nautobot_snapshot(BASE, TOKEN, ["LDN1"], session=nb.session)
    assert [s.name for s in snap.sites] == ["LDN1"]


@pytest.mark.parametrize(
    ("field", "slug"),
    [
        ({"name": "Active"}, "active"),
        ({"name": "Pre production"}, "pre-production"),
        (None, "active"),
        ("Staged", "staged"),
    ],
)
def test_status_slugs(field, slug):
    assert status_slug(field) == slug


def test_ids_are_chunked(nb, monkeypatch):
    monkeypatch.setattr(NautobotLoader, "id_chunk", 2)
    snap = load(nb)
    assert len(nb.list_calls("racks")) == 2  # three locations, two per request
    assert len(snap.racks) == 3  # each rack once, though the tree filter finds it from two chunks
    assert nautobot.ID_CHUNK == 50


def test_a_reimport_from_nautobot_merges_by_its_uuid_ids(nb):
    from importer.importer_helpers import assert_valid, build

    from railyard_sync.importer.merge import merge

    first = build(load(nb), prefix="nbt").project
    # In Nautobot meanwhile: the hall is renamed, the server is moved up and the out-of-band server deleted.
    nb.named("locations", "Data Hall 1")["name"] = "Hall One"
    nb.named("devices", "ldn1-srv1")["position"] = 12
    nb.data["devices"] = [d for d in nb.data["devices"] if d["name"] != "ldn1-pp1"]
    second = build(load(nb), prefix="nbt").project
    result = merge(first, second, prefix="nbt")
    assert_valid(result.project)
    diff = result.diff
    assert f"nbt-loc-{HALL_1}" in diff["containers"].updated
    srv = nb.named("devices", "ldn1-srv1")["id"]
    assert f"nbt-dev-{srv}" in diff["placements"].updated
    assert any(i.startswith("nbt-dev-") for i in diff["placements"].stale)  # the panel: kept, reported
    assert not diff.conflicts
    hall = next(c for c in result.project["containers"] if c["id"] == f"nbt-loc-{HALL_1}")
    assert hall["name"] == "Hall One"
    again = merge(result.project, second, prefix="nbt")
    assert not again.diff.changed

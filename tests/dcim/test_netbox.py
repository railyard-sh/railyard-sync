"""The NetBox loader against a fake NetBox serving 4.x-shaped fixtures. No network."""

from __future__ import annotations

import json

import pytest
import requests
from conftest import FakeResponse, FakeSession
from fake_netbox import BASE, NEXT_HOST, V1_TOKEN, V2_TOKEN, FakeNetBox

from railyard_sync.dcim import netbox
from railyard_sync.dcim.errors import (
    DCIMAuthError,
    DCIMConnectionError,
    DCIMError,
    DCIMNotFoundError,
    DCIMVersionError,
)
from railyard_sync.dcim.netbox import NetBoxLoader, load_netbox_snapshot
from railyard_sync.dcim.snapshot import Snapshot, Termination


def load(nb: FakeNetBox, sites=("ldn1",), **kw) -> Snapshot:
    return NetBoxLoader(BASE, nb.token, session=nb.session, **kw).load(list(sites))


def by_id(items, item_id):
    return next(i for i in items if i.id == str(item_id))


@pytest.fixture
def nb() -> FakeNetBox:
    return FakeNetBox()


@pytest.fixture
def snap(nb) -> Snapshot:
    return load(nb)


# ---- the whole site ---------------------------------------------------------------------------


def test_source_and_version(snap):
    assert snap.source == "netbox"
    assert snap.source_url == BASE
    assert snap.source_version == "4.6.2"


def test_region_chain_outermost_first(snap):
    assert [(r.id, r.name, r.parent_id) for r in snap.regions] == [
        ("1", "Europe", None),
        ("2", "United Kingdom", "1"),
        ("3", "London", "2"),
    ]


def test_site(snap):
    [site] = snap.sites
    assert (site.id, site.name, site.slug, site.status) == ("1", "LDN1", "ldn1", "active")
    assert site.facility == "Equinix LD8"
    assert site.region_id == "3"
    assert site.comments == "Cage 4, suite 2."
    assert site.tags == ["production"]


def test_locations_nest(snap):
    assert [(loc.id, loc.name, loc.parent_id, loc.site_id) for loc in snap.locations] == [
        ("10", "Building A", None, "1"),
        ("11", "Data Hall 1", "10", "1"),
    ]
    assert by_id(snap.locations, 11).facility == "DH1"


def test_only_referenced_rack_types_are_loaded(nb, snap):
    [rack_type] = snap.rack_types
    assert (rack_type.id, rack_type.manufacturer, rack_type.slug) == ("5", "APC", "apc-ar3300")
    assert (rack_type.outer_width_mm, rack_type.outer_depth_mm, rack_type.form_factor) == (600, 1070, "4-post-cabinet")
    assert nb.list_calls("rack-types")[0]["params"][0] == ("id", "5")


def test_racks_and_unit_conversion(snap):
    a01, a02, b01 = (by_id(snap.racks, i) for i in (20, 21, 22))
    assert (a01.name, a01.location_id, a01.rack_type_id, a01.role) == ("A01", "11", "5", "Network")
    assert (a01.outer_width_mm, a01.outer_depth_mm, a01.max_weight_kg) == (600, 1070, 1361)
    assert (a01.serial, a01.asset_tag, a01.facility_id) == ("AR3300-0001", "LDN1-R-A01", "LD8-0401-A01")
    assert a01.custom_fields == {"power_budget_kw": 8}
    assert a01.tags == ["production"]
    # A02 is measured in inches and pounds.
    assert a02.outer_width_mm == pytest.approx(609.6)
    assert a02.outer_depth_mm == pytest.approx(1066.8)
    assert a02.max_weight_kg == pytest.approx(1360.777, abs=1e-3)
    assert a02.desc_units is True
    assert a02.rack_type_id is None
    # B01: a 23" two-post frame without outer dimensions.
    assert (b01.width_in, b01.u_height, b01.form_factor, b01.status) == (23, 45, "2-post-frame", "planned")
    assert (b01.outer_width_mm, b01.outer_depth_mm, b01.max_weight_kg) == (None, None, None)
    assert b01.location_id == "10"


def test_devices(snap):
    assert [d.id for d in snap.devices] == ["40", "41", "42", "43", "44", "45", "46"]  # not MAN1's
    leaf = by_id(snap.devices, 40)
    assert (leaf.name, leaf.device_type_id, leaf.rack_id, leaf.position, leaf.face) == (
        "ldn1-leaf1",
        "30",
        "20",
        40,
        "front",
    )
    assert (leaf.role, leaf.platform, leaf.tenant, leaf.status) == ("Leaf switch", "Arista EOS", "Acme Ltd", "active")
    assert (leaf.serial, leaf.asset_tag, leaf.airflow) == ("JPE21130456", "LDN1-0040", "front-to-rear")
    assert leaf.tags == ["production", "core"]
    assert leaf.custom_fields == {"support_contract": "SC-1001", "rma_count": 0}
    assert leaf.comments == "Leaf for row A."

    pdu = by_id(snap.devices, 43)  # 0U: in the rack, no position or face
    assert (pdu.rack_id, pdu.position, pdu.face) == ("20", None, "")

    blade = by_id(snap.devices, 45)  # in a device bay of the chassis
    assert (blade.parent_device_id, blade.rack_id, blade.position) == ("44", "21", None)

    oob = by_id(snap.devices, 46)  # unracked
    assert (oob.rack_id, oob.location_id, oob.position, oob.status) == (None, "10", None, "staged")


def test_device_types_with_templates(snap):
    assert [t.id for t in snap.device_types] == ["30", "31", "32", "33", "34", "35", "36"]
    switch = by_id(snap.device_types, 30)
    assert (switch.manufacturer, switch.model, switch.slug) == (
        "Arista",
        "DCS-7050SX3-48YC8",
        "arista-dcs-7050sx3-48yc8",
    )
    assert (switch.u_height, switch.is_full_depth, switch.weight_kg, switch.airflow) == (1, True, 9.1, "front-to-rear")
    assert switch.part_number == "DCS-7050SX3-48YC8-R"
    assert [(c.kind, c.name, c.type) for c in switch.components] == [
        ("interface", "Ethernet1", "25gbase-x-sfp28"),
        ("interface", "Ethernet2", "25gbase-x-sfp28"),
        ("interface", "Management1", "1000base-t"),
        ("power-port", "PS1", "iec-60320-c14"),
        ("power-port", "PS2", "iec-60320-c14"),
        ("console-port", "Console", "rj-45"),
    ]
    assert [c.mgmt_only for c in switch.components if c.kind == "interface"] == [False, False, True]
    ps1 = switch.components[3]
    assert (ps1.maximum_draw_w, ps1.allocated_draw_w) == (450, 180)

    pdu = by_id(snap.device_types, 33)
    assert pdu.u_height == 0
    assert pdu.weight_kg == pytest.approx(5.443, abs=1e-3)  # 12 lb
    outlets = [c for c in pdu.components if c.kind == "power-outlet"]
    assert [(o.name, o.type, o.feed_leg) for o in outlets] == [
        ("Outlet 1", "iec-60320-c13", "A"),
        ("Outlet 2", "iec-60320-c13", "A"),
        ("Outlet 3", "iec-60320-c13", "A"),
    ]
    assert by_id(snap.device_types, 31).weight_kg == pytest.approx(1.2)  # 1200 g
    isr = by_id(snap.device_types, 36)
    assert isr.u_height == 0.5  # fractional U survives
    assert isr.weight_kg == pytest.approx(1.134, abs=1e-3)  # 40 oz
    assert (by_id(snap.device_types, 34).subdevice_role, by_id(snap.device_types, 35).subdevice_role) == (
        "parent",
        "child",
    )


def test_patch_panel_templates_map_front_to_rear(snap):
    panel = by_id(snap.device_types, 31)
    assert [(c.kind, c.name, c.positions, c.rear_port_name, c.rear_port_position) for c in panel.components] == [
        ("front-port", "Front1", 1, "Rear", 1),
        ("front-port", "Front2", 1, "Rear", 2),
        ("rear-port", "Rear", 2, "", 1),
    ]


def test_components(snap):
    kinds = {}
    for c in snap.components:
        kinds.setdefault(c.kind, []).append(c.id)
    assert kinds == {
        "interface": ["100", "101", "102", "103", "110", "111", "120"],
        "rear-port": ["200"],
        "front-port": ["210", "211"],
        "power-port": ["300", "301", "310", "311", "320"],
        "power-outlet": ["330", "331", "332"],
        "console-port": ["340"],
    }
    mgmt = by_id(snap.components, 102)
    assert (mgmt.device_id, mgmt.name, mgmt.type, mgmt.mgmt_only) == ("40", "Management1", "1000base-t", True)
    module_port = by_id(snap.components, 103)
    assert (module_port.module, module_port.enabled) == ("Slot 49", False)
    rear = by_id(snap.components, 200)
    assert (rear.positions, rear.type) == (2, "mpo")
    psu = by_id(snap.components, 310)
    assert (psu.maximum_draw_w, psu.allocated_draw_w) == (1400, 700)
    outlet = by_id(snap.components, 330)
    assert (outlet.power_port_id, outlet.feed_leg, outlet.type) == ("320", "A", "iec-60320-c13")


def test_front_ports_map_to_rear_ports(snap):
    assert [(c.id, c.rear_port_id, c.rear_port_position) for c in snap.components if c.kind == "front-port"] == [
        ("210", "200", 1),
        ("211", "200", 2),
    ]


def test_cables(snap):
    assert [c.id for c in snap.cables] == ["500", "501", "502", "503", "504", "505"]
    data = by_id(snap.cables, 500)
    assert data.a == [Termination("dcim.interface", "100")]
    assert data.b == [Termination("dcim.frontport", "210")]
    assert (data.type, data.status, data.label, data.color, data.length_m) == (
        "mmf-om4",
        "connected",
        "LDN1-0500",
        "00bcd4",
        3,
    )
    assert data.tags == ["production"]
    assert by_id(snap.cables, 501).length_m == pytest.approx(3.0)  # 300 cm
    power = by_id(snap.cables, 502)
    assert (power.type, power.color) == ("power", "f44336")
    assert power.length_m == pytest.approx(1.8288)  # 6 ft
    assert power.b == [Termination("dcim.poweroutlet", "330")]
    assert by_id(snap.cables, 503).b == [Termination("dcim.powerfeed", "61")]
    assert by_id(snap.cables, 503).length_m is None
    circuit = by_id(snap.cables, 504)
    assert circuit.b == [Termination("circuits.circuittermination", "700")]
    assert circuit.length_m == pytest.approx(50.0)  # 0.05 km
    assert by_id(snap.cables, 505).status == "planned"


def test_cable_to_another_site_is_kept_with_a_warning(snap):
    assert by_id(snap.cables, 505).b == [Termination("dcim.interface", "900")]
    assert snap.warnings == [
        "Cable 505 (OOB-MAN): its B end (dcim.interface 900) is outside the loaded sites; "
        "the cable is kept for the importer to decide."
    ]


def test_power_panels_and_feeds(snap):
    [panel] = snap.power_panels
    assert (panel.id, panel.name, panel.site_id, panel.location_id) == ("60", "PP-LDN1-A", "1", "10")
    a, b = snap.power_feeds
    assert (a.name, a.power_panel_id, a.rack_id, a.type, a.phase) == (
        "LDN1-A01-Feed-A",
        "60",
        "20",
        "primary",
        "single-phase",
    )
    assert round(a.available_power_w) == 5888
    assert (b.type, b.phase, b.voltage, b.amperage) == ("redundant", "three-phase", 400, 16)


def test_snapshot_round_trips_through_json(snap):
    assert Snapshot.from_dict(json.loads(json.dumps(snap.to_dict()))) == snap


def test_two_sites_share_regions_and_resolve_cross_site_cables(nb):
    snap = load(nb, ["ldn1", "man1"])
    assert [s.slug for s in snap.sites] == ["ldn1", "man1"]
    assert [r.id for r in snap.regions] == ["1", "2", "3"]  # United Kingdom read once
    assert nb.requests["/api/dcim/regions/2/"] == 1
    assert "90" in {d.id for d in snap.devices}
    assert [c.id for c in snap.cables].count("505") == 1
    assert snap.warnings == []
    assert nb.list_calls("devices")[0]["params"][:2] == [("site_id", "1"), ("site_id", "2")]


# ---- version differences ------------------------------------------------------------------------


def test_legacy_front_port_shape(nb):
    legacy = FakeNetBox(version="4.4.9", legacy_ports=True)
    assert "rear_port" in legacy.item("front-ports", 210) and "rear_ports" not in legacy.item("front-ports", 210)
    old, new = load(legacy), load(nb)
    assert old.source_version == "4.4.9"
    for kind in ("front-port", "rear-port"):
        assert [c for c in old.components if c.kind == kind] == [c for c in new.components if c.kind == kind]
    assert by_id(old.device_types, 31).components == by_id(new.device_types, 31).components


def test_front_port_with_several_mappings_keeps_the_first_and_warns(nb):
    nb.item("front-ports", 210)["rear_ports"] = [
        {"position": 2, "rear_port": 200, "rear_port_position": 2},
        {"position": 1, "rear_port": 200, "rear_port_position": 1},
    ]
    snap = load(nb)
    front = by_id(snap.components, 210)
    assert (front.rear_port_id, front.rear_port_position) == ("200", 1)
    assert any("Front port 210" in w and "2 rear port positions" in w for w in snap.warnings)


def test_netbox_4_0_racks_without_rack_types(nb):
    nb.status["netbox-version"] = "4.0.11"
    for rack in nb.data["racks"]:
        rack["type"] = rack.pop("form_factor")  # 4.0's name for it
        rack.pop("rack_type")
    snap = load(nb)
    assert snap.rack_types == []
    assert nb.list_calls("rack-types") == []
    assert by_id(snap.racks, 22).form_factor == "2-post-frame"


def test_rack_types_are_not_requested_on_4_0_even_if_referenced(nb):
    nb.status["netbox-version"] = "4.0.0"
    load(nb)
    assert nb.list_calls("rack-types") == []


@pytest.mark.parametrize("version", ["3.7.8", "2.11.12"])
def test_refuses_netbox_before_4_0(nb, version):
    nb.status["netbox-version"] = version
    with pytest.raises(DCIMVersionError, match=f"NetBox {version} is not supported"):
        load(nb)
    assert list(nb.requests) == ["/api/status/"]  # nothing else read


def test_refuses_netbox_5(nb):
    nb.status["netbox-version"] = "5.0.0"
    with pytest.raises(DCIMVersionError, match="not supported yet"):
        load(nb)


def test_newer_4_x_loads_with_a_warning(nb):
    nb.status["netbox-version"] = "4.7.0-beta1"
    snap = load(nb)
    assert snap.source_version == "4.7.0-beta1"
    assert "newer than railyard-sync has been tested with" in snap.warnings[0]


def test_unreadable_version(nb):
    nb.status["netbox-version"] = "unknown"
    with pytest.raises(DCIMVersionError, match="Could not read the NetBox version"):
        load(nb)


# ---- pagination and efficiency ----------------------------------------------------------------


def test_pagination_follows_next_but_stays_on_the_configured_host(nb, snap):
    paged = FakeNetBox()
    small = load(paged, page_size=2)
    assert small == snap
    devices = paged.list_calls("devices")
    assert [str(dict(c["params"])["offset"]) for c in devices] == ["0", "2", "4", "6"]
    assert all(c["url"] == f"{BASE}/api/dcim/devices/" for c in devices)
    assert all(NEXT_HOST not in c["url"] for c in paged.session.calls)
    # The filters survive from page to page.
    assert all(("site_id", "1") in c["params"] for c in devices)
    assert all(("exclude", "config_context") in c["params"] for c in devices)


def test_one_request_per_endpoint_for_a_site(nb, snap):
    lists = {path: n for path, n in nb.requests.items() if path.startswith("/api/dcim/") and "regions" not in path}
    assert set(lists.values()) == {1}
    # sites, locations, racks, rack types, devices, device types, 6 template and 6 component lists,
    # power panels, power feeds, cables
    assert len(lists) == 21


def test_templates_are_requested_in_chunks_of_device_types(nb, monkeypatch):
    monkeypatch.setattr(netbox, "ID_CHUNK", 3)
    snap = load(nb)
    calls = nb.list_calls("interface-templates")
    assert [[v for k, v in c["params"] if k == "device_type_id"] for c in calls] == [
        ["30", "31", "32"],
        ["33", "34", "35"],
        ["36"],
    ]
    assert len(nb.list_calls("device-types")) == 3
    assert len(by_id(snap.device_types, 30).components) == 6


def test_device_components_are_filtered_by_site_not_device(nb, snap):
    for endpoint in ("interfaces", "front-ports", "rear-ports", "power-ports", "power-outlets", "console-ports"):
        [call] = nb.list_calls(endpoint)
        assert [k for k, _ in call["params"]] == ["site_id", "limit", "offset"]


# ---- auth, lookup and errors ------------------------------------------------------------------


def test_v2_token_uses_bearer(nb, snap):
    assert nb.session.calls[0]["headers"]["Authorization"] == f"Bearer {V2_TOKEN}"


def test_v1_token_uses_token_scheme():
    nb = FakeNetBox(token=V1_TOKEN)
    load(nb)
    assert nb.session.calls[0]["headers"]["Authorization"] == f"Token {V1_TOKEN}"


def test_verify_and_timeout_are_passed_to_the_session(nb):
    NetBoxLoader(BASE, nb.token, session=nb.session, verify="/etc/ssl/netbox-ca.pem", timeout=5).load(["ldn1"])
    assert {(c["verify"], c["timeout"]) for c in nb.session.calls} == {("/etc/ssl/netbox-ca.pem", 5)}


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_never_include_the_token(status):
    session = FakeSession(lambda *a: FakeResponse(status, {"detail": "Invalid token"}))
    loader = NetBoxLoader(BASE, V2_TOKEN, session=session)
    with pytest.raises(DCIMAuthError) as err:
        loader.load(["ldn1"])
    assert err.value.status == status
    assert V2_TOKEN not in str(err.value)
    assert V2_TOKEN not in repr(err.value)
    assert V2_TOKEN not in repr(loader)


def test_wrong_token_is_an_auth_error():
    nb = FakeNetBox(token=V2_TOKEN)
    with pytest.raises(DCIMAuthError, match="HTTP 403"):
        NetBoxLoader(BASE, "nbt_wrong.token", session=nb.session).load(["ldn1"])


def test_server_error_body_is_scrubbed_of_the_token():
    session = FakeSession(lambda *a: FakeResponse(500, text=f"Traceback… Authorization: Bearer {V2_TOKEN}"))
    with pytest.raises(DCIMError) as err:
        NetBoxLoader(BASE, V2_TOKEN, session=session).load(["ldn1"])
    assert err.value.status == 500
    assert V2_TOKEN not in str(err.value)
    assert "***" in str(err.value)


def test_the_secret_half_of_a_v2_token_is_scrubbed_too():
    secret = V2_TOKEN.partition(".")[2]
    session = FakeSession(lambda *a: FakeResponse(502, text=f"upstream said: {secret}"))
    with pytest.raises(DCIMError) as err:
        NetBoxLoader(BASE, V2_TOKEN, session=session).load(["ldn1"])
    assert secret not in str(err.value)


def test_a_device_type_netbox_does_not_return_is_reported(nb):
    nb.data["device-types"] = [t for t in nb.data["device-types"] if t["id"] != 36]
    snap = load(nb)
    assert "36" not in {t.id for t in snap.device_types}
    assert any("device type(s) 36" in w for w in snap.warnings)


def test_connection_errors_are_typed_and_scrubbed():
    def refuse(*a):
        raise requests.ConnectionError(f"Max retries exceeded (token {V1_TOKEN})")

    with pytest.raises(DCIMConnectionError) as err:
        NetBoxLoader(BASE, V1_TOKEN, session=FakeSession(refuse)).load(["ldn1"])
    assert "Could not reach NetBox at https://netbox.example.com" in str(err.value)
    assert V1_TOKEN not in str(err.value)
    assert err.value.__cause__ is None


def test_non_json_response():
    class HTML(FakeResponse):
        def json(self):
            raise ValueError("Expecting value")

    with pytest.raises(DCIMError, match="not JSON"):
        NetBoxLoader(BASE, V2_TOKEN, session=FakeSession(lambda *a: HTML(200, text="<html>"))).load(["ldn1"])


def test_unknown_site(nb):
    with pytest.raises(DCIMNotFoundError, match="No NetBox site matched 'nowhere'"):
        load(nb, ["nowhere"])


@pytest.mark.parametrize("ref", ["ldn1", "LDN1", "1"])
def test_site_by_slug_name_or_id(nb, ref):
    assert [s.id for s in load(nb, [ref]).sites] == ["1"]


def test_the_same_site_twice_is_read_once(nb):
    snap = load(nb, ["ldn1", "LDN1", "1"])
    assert [s.id for s in snap.sites] == ["1"]
    assert len(snap.devices) == 7


def test_a_single_site_string_is_accepted(nb):
    assert NetBoxLoader(BASE, nb.token, session=nb.session).load("ldn1").sites[0].slug == "ldn1"


def test_no_sites():
    with pytest.raises(ValueError, match="at least one site"):
        NetBoxLoader(BASE, V2_TOKEN, session=FakeSession(lambda *a: None)).load([])


# ---- construction -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["ftp://netbox.example.com", "netbox.example.com", "file:///etc/passwd", "https://", "javascript:alert(1)"]
)
def test_rejects_non_http_urls(url):
    with pytest.raises(ValueError, match="http:// or https://"):
        NetBoxLoader(url, V2_TOKEN, session=FakeSession(lambda *a: None))


def test_rejects_credentials_in_the_url():
    with pytest.raises(ValueError, match="must not contain credentials"):
        NetBoxLoader("https://admin:secret@netbox.example.com", V2_TOKEN, session=FakeSession(lambda *a: None))


@pytest.mark.parametrize(
    "url", ["https://netbox.example.com/", "https://netbox.example.com//", "https://netbox.example.com/api/"]
)
def test_strips_trailing_slashes_and_api(url):
    assert NetBoxLoader(url, V2_TOKEN, session=FakeSession(lambda *a: None)).url == BASE


def test_requires_a_token():
    with pytest.raises(ValueError, match="token is required"):
        NetBoxLoader(BASE, "", session=FakeSession(lambda *a: None))


def test_repr_hides_the_token():
    loader = NetBoxLoader(BASE, V2_TOKEN, session=FakeSession(lambda *a: None))
    assert repr(loader) == f"NetBoxLoader(url={BASE!r})"
    assert V2_TOKEN not in repr(loader) and V2_TOKEN not in str(vars(loader).get("url"))


def test_load_netbox_snapshot(nb):
    snap = load_netbox_snapshot(BASE, nb.token, ["ldn1"], session=nb.session, verify=False)
    assert [s.slug for s in snap.sites] == ["ldn1"]
    assert {c["verify"] for c in nb.session.calls} == {False}


# ---- units ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "unit", "mm"), [(600, "mm", 600), (24, "in", 609.6), (60, "cm", 600)])
def test_lengths_to_mm(value, unit, mm):
    assert netbox._mm(value, {"value": unit, "label": unit}) == pytest.approx(mm)


@pytest.mark.parametrize(
    ("value", "unit", "kg"),
    [(10, "kg", 10), (500, "g", 0.5), (10, "lb", 4.5359237), (16, "oz", 0.45359237)],
)
def test_weights_to_kg(value, unit, kg):
    assert netbox._kg(value, {"value": unit, "label": unit}) == pytest.approx(kg)


@pytest.mark.parametrize(
    ("value", "unit", "m"),
    [(1, "km", 1000), (3, "m", 3), (250, "cm", 2.5), (1, "mi", 1609.344), (10, "ft", 3.048), (12, "in", 0.3048)],
)
def test_cable_lengths_to_metres(value, unit, m):
    assert netbox._metres(value, {"value": unit, "label": unit}) == pytest.approx(m)


def test_missing_values_and_units():
    assert netbox._mm(None, {"value": "in"}) is None
    assert netbox._metres("", None) is None
    assert netbox._mm("1070", None) == 1070  # no unit: NetBox's default (mm)
    with pytest.raises(DCIMError, match="Unknown unit 'furlong'"):
        netbox._metres(1, {"value": "furlong"})

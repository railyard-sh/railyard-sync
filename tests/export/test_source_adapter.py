"""RailyardAdapter.load() turns a Project document into the canonical DiffSync object graph."""

from railyard_sync.export import models
from railyard_sync.export.devicetype_library import DeviceTypeLibrary
from railyard_sync.export.source import RailyardAdapter

# force the offline catalogue path so tests never hit the devicetype-library over the network
OFFLINE = lambda: DeviceTypeLibrary(fetcher=lambda url: None)  # noqa: E731


def load(project_doc) -> RailyardAdapter:
    adapter = RailyardAdapter(project_doc, devicetype_library=OFFLINE())
    adapter.load()
    return adapter


def names(adapter, model) -> set:
    return {tuple(getattr(o, k) for k in model._identifiers) for o in adapter.get_all(model)}


# ---- example project (containers + devices, one unresolved) ---------------


def test_example_project_containers_and_devices(example_project):
    a = load(example_project)

    assert {s.name for s in a.get_all(models.Site)} == {"LDN1", "FRA1"}
    assert names(a, models.Rack) == {("LDN1", "LDN1-A01"), ("LDN1", "LDN1-A02"), ("FRA1", "FRA1-A01")}

    devices = {d.name for d in a.get_all(models.Device)}
    assert devices == {"LDN1-A01-LEAF-01", "LDN1-A02-LEAF-01", "FRA1-A01-LEAF-01"}

    # the "some 2U server" placement has no catalogue entry -> skipped + reported
    assert len(a.unresolved) == 1
    assert a.unresolved[0]["ref"] == "some 2U server"

    assert {m.name for m in a.get_all(models.Manufacturer)} == {"Cisco"}
    dt = a.get_all(models.DeviceType)[0]
    assert (dt.manufacturer, dt.model, dt.slug) == ("Cisco", "Nexus 93180YC-FX", "cisco-nexus-93180yc-fx")
    assert dt.part_number == "N9K-C93180YC-FX"


def test_example_project_rack_fields(example_project):
    a = load(example_project)
    rack = a.get(models.Rack, {"site": "LDN1", "name": "LDN1-A01"})
    assert rack.width == 19  # 600mm
    assert rack.u_height == 42
    assert rack.desc_units is False
    assert rack.status == "active"


def test_device_role_defaulted(example_project):
    a = load(example_project)
    roles = {r.name for r in a.get_all(models.DeviceRole)}
    assert "leaf" in roles


# ---- cabled project (ports, cables, power) --------------------------------


def test_cabled_project_devices(cabled_project):
    a = load(cabled_project)
    assert {d.name for d in a.get_all(models.Device)} == {"SW-1", "SRV-1", "PDU-1"}
    # 23" rail from the 800mm rack
    assert a.get(models.Rack, {"site": "DC1", "name": "R1"}).width == 23
    # 0U PDU has no rack position
    assert a.get(models.Device, "PDU-1").position is None
    assert a.get(models.Device, "SW-1").position == 10


def test_cabled_project_interfaces(cabled_project):
    a = load(cabled_project)
    ifaces = {(i.device, i.name, i.type) for i in a.get_all(models.Interface)}
    # the switch's real port, typed from its SFP+ media
    assert ("SW-1", "Eth1", "10gbase-x-sfpp") in ifaces
    # a synthesized interface on the port-less server, typed from the cable media
    assert ("SRV-1", "iface1", "10gbase-x-sfpp") in ifaces


def test_cabled_project_power(cabled_project):
    a = load(cabled_project)
    outlets = {(o.device, o.name, o.type) for o in a.get_all(models.PowerOutlet)}
    assert outlets == {("PDU-1", "OUT1", "iec-60320-c13"), ("PDU-1", "OUT2", "iec-60320-c13")}
    ports = {(p.device, p.name, p.type) for p in a.get_all(models.PowerPort)}
    assert ports == {("SRV-1", "PSU1", "iec-60320-c14")}


def test_cabled_project_cables(cabled_project):
    a = load(cabled_project)
    cables = {
        (c.a_device, c.a_type, c.a_name, c.b_device, c.b_type, c.b_name, c.is_power) for c in a.get_all(models.Cable)
    }
    # data cable: switch interface -> server (synthesized) interface
    assert ("SW-1", "dcim.interface", "Eth1", "SRV-1", "dcim.interface", "iface1", False) in cables
    # power cable: server PSU -> PDU outlet
    assert ("SRV-1", "dcim.powerport", "PSU1", "PDU-1", "dcim.poweroutlet", "OUT1", True) in cables


def test_cable_label_kept_from_railyard(cabled_project):
    a = load(cabled_project)
    data = next(c for c in a.get_all(models.Cable) if not c.is_power)
    assert data.label == "L1"  # the Railyard-set label is preserved


def test_cable_label_synthesised_from_endpoints(cabled_project):
    cabled_project["cables"][0].pop("label")  # drop the Railyard label so synthesis kicks in
    a = load(cabled_project)
    labels = {(c.a_device, c.a_name, c.b_device, c.b_name): c.label for c in a.get_all(models.Cable)}
    assert labels[("SW-1", "Eth1", "SRV-1", "iface1")] == "SW-1:Eth1 <-> SRV-1:iface1"
    assert labels[("SRV-1", "PSU1", "PDU-1", "OUT1")] == "SRV-1:PSU1 -> PDU-1:OUT1"

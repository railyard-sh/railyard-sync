"""The cabling planner caps the components it emits per device, so a malformed or hostile project
can't make a sync create an unbounded number of NetBox objects."""

from railyard_sync.export import models
from railyard_sync.export.cabling import MAX_OUTLETS, MAX_PORTS_PER_SIDE
from railyard_sync.export.devicetype_library import DeviceTypeLibrary
from railyard_sync.export.source import RailyardAdapter


def load(doc) -> RailyardAdapter:
    adapter = RailyardAdapter(doc, devicetype_library=DeviceTypeLibrary(fetcher=lambda url: None))
    adapter.load()
    return adapter


def test_patch_panel_port_count_is_capped(cabled_project):
    cabled_project["catalogue"].append(
        {
            "key": "pp",
            "manufacturer": "Acme",
            "model": "PP",
            "uHeight": 1,
            "ports": {"front": 10_000_000, "rear": 10_000_000, "passThrough": True, "media": "RJ45"},
        }
    )
    cabled_project["racks"][0]["placements"].append(
        {"id": "p_pp", "startU": 20, "heightU": 1, "face": "front", "label": "PP-1", "deviceTypeRef": "pp"}
    )
    cabled_project["cables"].append(
        {
            "id": "c2",
            "a": {"rackId": "r1", "placementId": "p_pp", "side": "front", "portIndex": 1},
            "b": {"rackId": "r1", "placementId": "p_srv"},
        }
    )
    a = load(cabled_project)
    assert len([p for p in a.get_all(models.FrontPort) if p.device == "PP-1"]) == MAX_PORTS_PER_SIDE
    assert len([p for p in a.get_all(models.RearPort) if p.device == "PP-1"]) == MAX_PORTS_PER_SIDE
    assert any("exceeds the limit" in w for w in a.warnings)


def test_outlet_count_is_capped(cabled_project):
    for entry in cabled_project["catalogue"]:
        if entry["key"] == "pdu":
            entry["outlets"]["count"] = 10_000_000
    a = load(cabled_project)
    assert len([o for o in a.get_all(models.PowerOutlet) if o.device == "PDU-1"]) == MAX_OUTLETS


def test_non_numeric_counts_do_not_crash(cabled_project):
    for entry in cabled_project["catalogue"]:
        if entry["key"] == "pdu":
            entry["outlets"]["count"] = "lots"
    a = load(cabled_project)  # the power link then has no outlet to land on and is reported, not raised
    assert any("pw1" in w for w in a.warnings)

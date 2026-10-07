"""sync_to_nautobot against an in-memory Nautobot (``fake_nautobot_rest.py``): the NetBox plugin's ownership rules,
for Nautobot 2.x, over REST.

The documents are what Railyard's own exporter writes for a small cabled estate (a switch, a patch panel, a server
and a 0U PDU in one rack, in DC1 → Hall 1 → row A); see ``tests/fixtures/sync/make_documents.py``.
"""

import copy
import json
import pathlib

import pytest
import requests
from fake_nautobot_rest import TOKEN, URL, FakeNautobot

from railyard_sync.export import nautobot_rest
from railyard_sync.export.nautobot_document import NautobotSyncDocumentAdapter
from railyard_sync.export.nautobot_rest import (
    NautobotAuthError,
    NautobotClient,
    NautobotConnectionError,
    NautobotVersionError,
)
from railyard_sync.export.policy import DEFAULT_RAILYARD_URL, ownership_tag
from railyard_sync.export.run import DESIGN_TAG_DESCRIPTION, SyncRefused, sync_to_nautobot
from railyard_sync.export.sync_document import SyncDocumentError

SYNC = pathlib.Path(__file__).parent.parent / "fixtures" / "sync"
DOCS = {name: json.loads((SYNC / f"nautobot-sync-{name}.json").read_text()) for name in ("cabled", "example")}

OBJECT_COUNTS = {
    "dcim/location-types": 3,
    "dcim/manufacturers": 1,
    "dcim/device-types": 4,
    "extras/roles": 4,
    "dcim/locations": 3,
    "dcim/racks": 1,
    "dcim/devices": 4,
    "dcim/interfaces": 2,
    "dcim/rear-ports": 2,
    "dcim/front-ports": 2,
    "dcim/power-outlets": 2,
    "dcim/power-ports": 1,
    "dcim/cables": 3,
}
UNTAGGED = ("dcim/location-types", "dcim/manufacturers", "extras/roles", "extras/statuses")


def document(name: str = "cabled") -> dict:
    return copy.deepcopy(DOCS[name])


def sync(nb: FakeNautobot, doc: dict | None = None, **kw):
    kw.setdefault("dry_run", False)
    return sync_to_nautobot(doc or document(), nb.URL, nb.token, session=nb, **kw)


def spec(doc: dict | None = None):
    doc = doc or document()
    return ownership_tag(DEFAULT_RAILYARD_URL, doc["project"]["id"], doc["project"]["name"])


def owned(nb: FakeNautobot, endpoint: str) -> list[dict]:
    if endpoint in UNTAGGED:
        return [o for o in nb.objects[endpoint].values() if o["custom_fields"].get("railyard_owner") == spec().slug]
    tag = nb.tag(spec().name)
    return nb.tagged(endpoint, tag["id"]) if tag else []


def drop_device(doc: dict, name: str) -> dict:
    """``doc`` without a device and everything on it (as Railyard would export it)."""
    objects = doc["objects"]
    objects["devices"] = [r for r in objects["devices"] if r["name"] != name]
    for kind in ("interfaces", "rear-ports", "front-ports", "power-outlets", "power-ports"):
        objects[kind] = [r for r in objects[kind] if r["device__name"] != name]
    objects["cables"] = [c for c in objects["cables"] if name not in (c["a"]["device"], c["b"]["device"])]
    return doc


def rename_device(doc: dict, old: str, new: str) -> dict:
    for row in doc["objects"]["devices"]:
        if row["name"] == old:
            row["name"] = new
    for kind in ("interfaces", "rear-ports", "front-ports", "power-outlets", "power-ports"):
        for row in doc["objects"][kind]:
            if row["device__name"] == old:
                row["device__name"] = new
            if row.get("rear_port__device__name") == old:
                row["rear_port__device__name"] = new
    for row in doc["objects"]["cables"]:
        for side in ("a", "b"):
            if row[side]["device"] == old:
                row[side]["device"] = new
    return doc


def seed_tree(nb: FakeNautobot) -> dict[str, str]:
    """The design's location types and locations, made by an operator before the first sync."""
    dc = nb.add("dcim/location-types", name="Data centre", parent=None, content_types=["dcim.rack", "dcim.device"])
    hall = nb.add("dcim/location-types", name="Hall", parent=dc, content_types=["dcim.rack", "dcim.device"])
    row = nb.add("dcim/location-types", name="Row", parent=hall, content_types=["dcim.rack", "dcim.device"])
    active = nb.status_id("Active")
    dc1 = nb.add("dcim/locations", name="DC1", location_type=dc, parent=None, status=active)
    hall1 = nb.add("dcim/locations", name="Hall 1", location_type=hall, parent=dc1, status=active)
    a = nb.add("dcim/locations", name="A", location_type=row, parent=hall1, status=active)
    return {"DC1": dc1, "Hall 1": hall1, "A": a}


# ---- the document ------------------------------------------------------------------------------------------


def test_the_document_reads_into_the_canonical_models():
    source = NautobotSyncDocumentAdapter(document(), name="railyard")
    source.load()
    assert source.warnings == []
    assert (source.project_id, source.project_name) == ("prj_sync_fixture", "Sync fixture")
    dc = source.get("location_type", "Data centre")
    assert (dc.parent, dc.content_types, dc.nestable) == ("", ["dcim.device", "dcim.rack"], False)
    assert source.get("location", "DC1").parent == ""  # NoObject
    assert source.get("location", "Hall 1").parent == "DC1"
    rack = source.get("rack", {"location": "A", "name": "R1"})
    assert (rack.status, rack.role, rack.type, rack.width, rack.tags) == (
        "Planned",
        "",
        "4-post-cabinet",
        23,
        ["Build 1"],
    )
    pdu = source.get("device", "PDU-1")
    assert (pdu.position, pdu.face, pdu.rack, pdu.railyard_id) == (None, "", "R1", "p_pdu")
    srv = source.get("device", "SRV-1")
    assert (srv.position, srv.face, srv.comments) == (1, "rear", "Database host")
    assert source.get("front_port", {"device": "PP-1", "name": "1"}).rear_port == "1"
    assert source.get("interface", {"device": "SW-1", "name": "Eth1"}).status == "Active"
    [tag] = source.tags
    assert (tag.name, tag.content_types) == ("Build 1", ("dcim.device", "dcim.rack"))
    power = next(c for c in source.get_all("cable") if c.is_power)
    assert (power.a_type, power.b_type, power.type, power.status) == (
        "dcim.powerport",
        "dcim.poweroutlet",
        "power",
        "Connected",
    )
    assert source.get("device_type", {"manufacturer": "Acme", "model": "SW1"}).library_slug == "sw"


def test_a_netbox_document_is_refused():
    with pytest.raises(SyncDocumentError, match="Not a Railyard Nautobot sync document"):
        NautobotSyncDocumentAdapter({"format": "railyard-netbox-sync", "version": 1, "project": {"id": "x"}})


def test_a_bad_row_is_skipped_with_a_warning():
    doc = document()
    doc["objects"]["racks"].append({"name": "R9", "location__name": "NoObject"})
    doc["objects"]["cables"].append({"a": {"device": "SW-1"}, "b": {}})
    source = NautobotSyncDocumentAdapter(doc, name="railyard")
    source.load()
    assert "racks row 2 skipped: no location__name" in source.warnings
    assert any(w.startswith("cables row 4 skipped") for w in source.warnings)


# ---- first export, re-run, dry run -------------------------------------------------------------------------


def test_first_export_creates_everything_owned():
    nb = FakeNautobot()
    result = sync(nb)
    assert result.ok, result.errors
    assert result.diff["create"] == result.created == sum(OBJECT_COUNTS.values())
    for endpoint, n in OBJECT_COUNTS.items():
        assert len(owned(nb, endpoint)) == len(nb.objects[endpoint]) == n, endpoint

    tag = nb.tag(spec().name)
    assert tag["description"].startswith("Managed by the Railyard sync. railyard-sync:v1 project=prj_sync_fixture")
    assert set(tag["content_types"]) == set(nautobot_rest.TAG_CONTENT_TYPES)
    fields = {f["key"]: f for f in nb.objects["extras/custom-fields"].values()}
    assert fields["railyard_id"]["content_types"] == ["dcim.device"]
    assert set(fields["railyard_owner"]["content_types"]) == set(nautobot_rest.OWNER_CONTENT_TYPES)
    assert fields["railyard_owner"]["filter_logic"] == "exact"

    row_type = nb.one("dcim/location-types", name="Row")
    assert nb.objects["dcim/location-types"][row_type["parent"]]["name"] == "Hall"
    a = nb.one("dcim/locations", name="A")
    assert nb.objects["dcim/locations"][a["parent"]]["name"] == "Hall 1"
    rack = nb.one("dcim/racks", name="R1")
    assert (rack["location"], rack["type"], rack["width"], rack["comments"]) == (
        a["id"],
        "4-post-cabinet",
        23,
        "Cold aisle A",
    )
    assert rack["status"] == nb.status_id("Planned")
    build1 = nb.tag("Build 1")
    assert build1["description"] == DESIGN_TAG_DESCRIPTION and set(build1["content_types"]) == {
        "dcim.rack",
        "dcim.device",
    }
    assert build1["id"] in rack["tags"]

    pdu = nb.one("dcim/devices", name="PDU-1")
    assert (pdu["position"], pdu["face"], pdu["rack"], pdu["custom_fields"]) == (
        None,
        "",
        rack["id"],
        {"railyard_id": "p_pdu"},
    )
    srv = nb.one("dcim/devices", name="SRV-1")
    assert (srv["position"], srv["face"], srv["comments"]) == (1, "rear", "Database host")
    front = nb.one("dcim/front-ports", name="1")
    assert nb.objects["dcim/rear-ports"][front["rear_port"]]["name"] == "1"
    iface = nb.one("dcim/interfaces", name="Eth1")
    assert (iface["type"], iface["status"]) == ("10gbase-x-sfpp", nb.status_id("Active"))
    power = next(c for c in nb.objects["dcim/cables"].values() if c["type"] == "power")
    assert power["status"] == nb.status_id("Connected")
    labelled = {c["label"]: c for c in nb.objects["dcim/cables"].values()}
    assert labelled["L1"]["color"] == "ff0000"


def test_a_second_run_changes_nothing():
    nb = FakeNautobot()
    sync(nb)
    writes = len(nb.writes)
    result = sync(nb)
    assert result.ok and result.diff == {
        "create": 0,
        "update": 0,
        "delete": 0,
        "no-change": sum(OBJECT_COUNTS.values()),
    }
    assert len(nb.writes) == writes


def test_a_dry_run_writes_nothing_and_plans_the_first_run():
    nb = FakeNautobot()
    result = sync(nb, dry_run=True)
    assert nb.writes == []
    assert result.dry_run and result.diff["create"] == sum(OBJECT_COUNTS.values())
    assert result.planned["create"]["device"] == 4
    assert any(w.startswith(f"Ownership tag {spec().name!r} doesn't exist yet") for w in result.warnings)
    assert any("'railyard_owner' custom field doesn't exist yet" in w for w in result.warnings)
    assert any(line.startswith("create: device [name=SW-1]") for line in result.changes)


def test_an_update_in_railyard_updates_only_what_changed():
    nb = FakeNautobot()
    sync(nb)
    doc = document()
    rack = doc["objects"]["racks"][0]
    rack["status__name"], rack["comments"] = "Active", "Hot aisle A"
    doc["objects"]["devices"][2]["position"] = "3"
    result = sync(nb, doc)
    assert result.ok, result.errors
    assert result.planned["update"] == {"rack": 1, "device": 1}
    assert nb.one("dcim/racks", name="R1")["comments"] == "Hot aisle A"
    assert nb.one("dcim/racks", name="R1")["status"] == nb.status_id("Active")
    assert nb.one("dcim/devices", name="SRV-1")["position"] == 3


# ---- shared objects and conflicts --------------------------------------------------------------------------


def test_existing_shared_objects_are_used_as_they_are():
    nb = FakeNautobot()
    locations = seed_tree(nb)
    mfr = nb.add("dcim/manufacturers", name="Acme")
    rack = nb.add(
        "dcim/racks", name="R1", location=locations["A"], status=nb.status_id("Active"), width=19, u_height=48, tags=[]
    )
    result = sync(nb)
    assert result.ok, result.errors
    referenced = "\n".join(result.referenced)
    for what in ("location type Row", "location DC1", "location A", "manufacturer Acme", "rack A/R1"):
        assert f"{what} (exists; used as-is" in referenced
    # Used, never changed or marked: the operator's rack keeps its size and status, and holds the sync's devices.
    assert nb.objects["dcim/racks"][rack]["u_height"] == 48 and nb.objects["dcim/racks"][rack]["tags"] == []
    assert nb.objects["dcim/manufacturers"][mfr]["custom_fields"] == {}
    assert nb.one("dcim/devices", name="SW-1")["rack"] == rack
    assert owned(nb, "dcim/locations") == [] and owned(nb, "dcim/location-types") == []


def test_a_role_not_enabled_for_devices_skips_the_devices_that_need_it():
    nb = FakeNautobot()
    nb.add("extras/roles", name="leaf", content_types=["dcim.rack"], color="aaaaaa")
    result = sync(nb)
    assert result.ok, result.errors
    assert any("role leaf exists but isn't enabled for dcim.device" in c for c in result.conflicts)
    assert any(c == "device SW-1: skipped because its role leaf can't be used" for c in result.conflicts)
    assert result.dependents_skipped == 2  # SW-1's interface and its cable
    assert nb.all("dcim/devices", name="SW-1") == []
    assert len(nb.objects["dcim/devices"]) == 3
    assert nb.one("extras/roles", name="leaf")["content_types"] == ["dcim.rack"]  # not changed


def test_a_location_type_that_cant_hold_racks_skips_them():
    nb = FakeNautobot()
    nb.add("dcim/location-types", name="Row", parent=None, content_types=[])
    doc = document()
    result = sync(nb, doc)
    assert any(
        "rack A/R1: skipped because its location's type Row isn't enabled for dcim.rack" in c for c in result.conflicts
    )
    assert nb.objects["dcim/racks"] == {} and nb.objects["dcim/devices"] == {}


def test_an_operators_device_of_the_same_name_is_a_conflict_with_its_dependents():
    nb = FakeNautobot()
    locations = seed_tree(nb)
    mfr = nb.add("dcim/manufacturers", name="OperatorCo")
    dtype = nb.add("dcim/device-types", manufacturer=mfr, model="OP1", u_height=1)
    role = nb.add("extras/roles", name="operator", content_types=["dcim.device"])
    theirs = nb.add(
        "dcim/devices",
        name="SW-1",
        location=locations["A"],
        device_type=dtype,
        role=role,
        status=nb.status_id("Active"),
    )
    result = sync(nb)
    assert result.ok, result.errors
    assert "device SW-1 in location A already exists there and is not managed by this sync" in result.conflicts
    assert result.dependents_skipped == 2
    assert nb.objects["dcim/devices"][theirs]["device_type"] == dtype  # untouched
    assert nb.all("dcim/interfaces", name="Eth1") == []


def test_a_cabled_port_is_a_conflict():
    nb = FakeNautobot()
    sync(nb)
    doc = document()
    cable = next(c for c in doc["objects"]["cables"] if c.get("label") == "L1")
    doc["objects"]["cables"].remove(cable)
    assert sync(nb, doc, allow_deletes=True).deleted == 1
    # An operator patches SW-1 Eth1 meanwhile; Railyard's cable comes back.
    eth1 = nb.one("dcim/interfaces", name="Eth1")
    other = nb.add("dcim/interfaces", device=eth1["device"], name="Eth9", type="other", status=nb.status_id("Active"))
    nb._create_cable(
        {
            "termination_a_type": "dcim.interface",
            "termination_a_id": eth1["id"],
            "termination_b_type": "dcim.interface",
            "termination_b_id": other,
            "status": nb.status_id("Connected"),
        }
    )
    result = sync(nb)
    assert any(c.startswith("cable L1: SW-1:Eth1 is already cabled") for c in result.conflicts)


def test_components_from_device_type_templates_are_adopted():
    nb = FakeNautobot()
    mfr = nb.add("dcim/manufacturers", name="Acme")
    dtype = nb.add("dcim/device-types", manufacturer=mfr, model="SW1", u_height=1, is_full_depth=True)
    nb.add("dcim/interface-templates", device_type=dtype, name="Eth1", type="1000base-t")
    nb.add("dcim/interface-templates", device_type=dtype, name="Mgmt", type="1000base-t")
    result = sync(nb)
    assert result.ok, result.errors
    assert "interface SW-1:Eth1" in result.adopted
    eth1 = nb.one("dcim/interfaces", name="Eth1")
    assert eth1["type"] == "10gbase-x-sfpp" and nb.tag(spec().name)["id"] in eth1["tags"]
    assert nb.one("dcim/interfaces", name="Mgmt")["tags"] == []  # not Railyard's: left alone
    assert sync(nb).diff["create"] == 0


# ---- renames and deletes -----------------------------------------------------------------------------------


def test_a_device_renamed_in_railyard_is_renamed_in_place():
    nb = FakeNautobot()
    sync(nb)
    before = nb.one("dcim/devices", name="SW-1")
    result = sync(nb, rename_device(document(), "SW-1", "SW-01"))
    assert result.ok, result.errors
    assert result.renamed == ["device SW-1 → SW-01"]
    assert nb.objects["dcim/devices"][before["id"]]["name"] == "SW-01"
    assert result.diff["create"] == 0
    assert len(nb.objects["dcim/cables"]) == 3


def test_deletes_are_opt_in_and_never_reach_what_the_sync_does_not_own():
    nb = FakeNautobot()
    sync(nb)
    doc = drop_device(document(), "PP-1")
    kept = sync(nb, doc)
    assert any(s.startswith("device [PP-1]") for s in kept.stale)
    assert nb.all("dcim/devices", name="PP-1")

    # An operator's IP address on SW-1's interface keeps SW-1 (its unassignment would modify the IP).
    eth1 = nb.one("dcim/interfaces", name="Eth1")
    ip = nb.add("ipam/ip-addresses", address="10.0.0.1/24")
    nb.add("ipam/ip-address-to-interface", ip_address=ip, interface=eth1["id"])
    doc = drop_device(doc, "SW-1")
    preview = sync(nb, doc, dry_run=True, allow_deletes=True)
    assert any("interface SW-1__Eth1: would modify 1 IP address assignment" in k for k in preview.kept)
    result = sync(nb, doc, allow_deletes=True)
    assert nb.all("dcim/devices", name="PP-1") == []
    assert nb.all("dcim/interfaces", name="Eth1")  # kept, with its device
    assert any(k.startswith("interface SW-1__Eth1") for k in result.kept)
    assert result.deleted >= 4


def test_a_location_with_an_operators_rack_is_not_deleted():
    nb = FakeNautobot()
    sync(nb)
    a = nb.one("dcim/locations", name="A")
    nb.add("dcim/racks", name="OPS", location=a["id"], status=nb.status_id("Active"), tags=[])
    doc = document()
    for kind in (
        "devices",
        "racks",
        "interfaces",
        "rear-ports",
        "front-ports",
        "power-outlets",
        "power-ports",
        "cables",
    ):
        doc["objects"][kind] = []
    doc["objects"]["locations"] = [r for r in doc["objects"]["locations"] if r["name"] != "A"]
    result = sync(nb, doc, allow_deletes=True)
    assert any(k.startswith("location A: still in use by 1 rack") for k in result.kept)
    assert nb.all("dcim/locations", name="A")
    assert nb.all("dcim/racks", name="R1") == []


def test_a_custom_status_is_created_owned_and_deleted_with_deletes_on():
    nb = FakeNautobot()
    doc = document()
    doc["objects"]["statuses"] = [
        {"name": "Commissioning", "content_types": "dcim.rack", "railyard": {"kind": "status"}}
    ]
    doc["objects"]["racks"][0]["status__name"] = "Commissioning"
    result = sync(nb, doc)
    assert result.ok, result.errors
    status = nb.one("extras/statuses", name="Commissioning")
    assert status["content_types"] == ["dcim.rack"]
    assert status["custom_fields"] == {"railyard_owner": spec().slug}
    assert nb.one("dcim/racks", name="R1")["status"] == status["id"]
    result = sync(nb, document(), allow_deletes=True)
    assert result.ok, result.errors
    assert nb.all("extras/statuses", name="Commissioning") == []


def test_an_unknown_status_is_reported():
    nb = FakeNautobot()
    doc = document()
    doc["objects"]["racks"][0]["status__name"] = "Commissioning"  # not in the statuses section, not in Nautobot
    result = sync(nb, doc)
    assert "status Commissioning doesn't exist in Nautobot; the objects that use it are skipped" in result.conflicts
    assert nb.objects["dcim/racks"] == {}


# ---- tags and custom fields --------------------------------------------------------------------------------


def test_an_operators_tag_not_enabled_for_racks_is_left_off_them():
    nb = FakeNautobot()
    nb.add("extras/tags", name="Build 1", content_types=["dcim.device"], color="ff0000", description="theirs")
    result = sync(nb)
    assert result.ok, result.errors
    assert any("Tag 'Build 1' exists in Nautobot but isn't enabled for dcim.rack" in w for w in result.warnings)
    build1 = nb.tag("Build 1")
    assert build1["content_types"] == ["dcim.device"]  # theirs: unchanged
    assert build1["id"] not in nb.one("dcim/racks", name="R1")["tags"]
    assert build1["id"] in nb.one("dcim/devices", name="SW-1")["tags"]
    assert sync(nb).diff["update"] == 0


def test_without_custom_field_permission_ids_are_not_stamped_and_org_objects_not_owned():
    nb = FakeNautobot()
    nb.forbid.add(("POST", "extras/custom-fields"))
    result = sync(nb)
    assert result.ok, result.errors
    assert any("'railyard_id' custom field doesn't exist and couldn't be created" in w for w in result.warnings)
    assert any(
        "'railyard_owner' custom field doesn't exist" in w and "never update or delete" in w for w in result.warnings
    )
    assert nb.one("dcim/devices", name="SW-1")["custom_fields"] == {}
    assert owned(nb, "extras/roles") == []
    second = sync(nb)
    assert second.diff["create"] == 0 and any(r.startswith("role leaf (exists") for r in second.referenced)


def test_an_ownership_tag_with_another_description_is_not_trusted():
    nb = FakeNautobot()
    nb.add("extras/tags", name=spec().name, content_types=[], description="someone else's")
    with pytest.raises(SyncRefused, match="A different Nautobot tag is already named"):
        sync(nb)


def test_the_ownership_tag_follows_a_project_rename_and_gains_content_types():
    nb = FakeNautobot()
    sync(nb)
    tag = nb.tag(spec().name)
    nb.objects["extras/tags"][tag["id"]]["content_types"] = ["dcim.device"]
    doc = document()
    doc["project"]["name"] = "Renamed fixture"
    result = sync(nb, doc)
    assert result.ok, result.errors
    renamed = nb.objects["extras/tags"][tag["id"]]
    assert renamed["name"] == spec(doc).name and set(renamed["content_types"]) == set(nautobot_rest.TAG_CONTENT_TYPES)
    assert result.diff["create"] == 0


# ---- the client --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("version", "ok"), [("1.6.20", False), ("2.0.0", True), ("2.4.43", True), ("4.0.0", False)])
def test_versions(version, ok):
    nb = FakeNautobot(version=version)
    if ok:
        assert sync(nb, dry_run=True).ok
    else:
        with pytest.raises(NautobotVersionError):
            sync(nb, dry_run=True)


def test_a_newer_release_is_synced_with_a_warning():
    result = sync(FakeNautobot(version="3.2.6"), dry_run=True)
    assert any("Nautobot 3.2.6 is newer than this railyard-sync was tested with" in w for w in result.warnings)


def test_a_refused_token():
    nb = FakeNautobot()
    with pytest.raises(NautobotAuthError) as err:
        sync_to_nautobot(document(), URL, "a" * 40, session=nb)
    assert "Nautobot did not accept the API token (HTTP 403)" in str(err.value) and "a" * 40 not in str(err.value)


def test_an_unreachable_nautobot():
    class Down:
        def request(self, *a, **kw):
            raise requests.ConnectionError(f"refused with {TOKEN}")

    with pytest.raises(NautobotConnectionError) as err:
        sync_to_nautobot(document(), URL, TOKEN, session=Down())
    assert TOKEN not in str(err.value) and "--nautobot-url" in str(err.value)


def test_ids_must_be_uuids():
    client = NautobotClient(URL, TOKEN, session=FakeNautobot())
    with pytest.raises(ValueError):
        client.get("dcim/devices", "../../users")


def test_a_ready_client_is_used_as_it_is():
    nb = FakeNautobot()
    result = sync_to_nautobot(document(), client=NautobotClient(URL, TOKEN, session=nb), dry_run=False)
    assert result.ok and result.nautobot_url == URL and result.target_name == "Nautobot"
    assert result.as_dict()["nautobot_version"] == "2.4.43"


def test_plain_http_warns_that_the_token_travels_in_clear():
    nb = FakeNautobot()
    nb.URL = "http://nautobot.example.com"
    result = sync_to_nautobot(document(), nb.URL, nb.token, session=nb, dry_run=True)
    assert any("not https" in w for w in result.warnings)


def test_front_ports_swapped_in_railyard_are_remapped_in_two_steps():
    nb = FakeNautobot()
    assert sync(nb).ok
    doc = document()
    for row in doc["objects"]["front-ports"]:
        row["rear_port__name"] = {"1": "2", "2": "1"}[row["rear_port__name"]]
    result = sync(nb, doc)
    assert result.ok, result.errors
    assert result.planned["update"] == {"front_port": 2}
    rear = {r["id"]: r["name"] for r in nb.objects["dcim/rear-ports"].values()}
    assert {f["name"]: rear[f["rear_port"]] for f in nb.objects["dcim/front-ports"].values()} == {"1": "2", "2": "1"}
    assert {r["positions"] for r in nb.objects["dcim/rear-ports"].values()} == {1}  # nothing left parked
    assert sync(nb, doc).diff["update"] == 0


def test_a_0u_device_type_is_created_0u():
    nb = FakeNautobot()
    assert sync(nb).ok
    assert nb.one("dcim/device-types", model="PDU1")["u_height"] == 0
    assert sync(nb).diff["update"] == 0

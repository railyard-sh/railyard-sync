"""sync_to_netbox against an in-memory NetBox (``fake_netbox_rest.py``): the plugin's ownership rules, over REST.

The documents are what Railyard's own exporter writes for a small cabled estate (a switch, a patch
panel, a server and a 0U PDU in one rack); see ``tests/fixtures/sync/make_documents.py``.
"""

import copy
import json
import pathlib

import pytest
from fake_netbox_rest import FakeNetBox, Response

from railyard_sync.export.netbox_rest import ENDPOINT, NetBoxAuthError, NetBoxClient, NetBoxConnectionError, NetBoxError
from railyard_sync.export.policy import DEFAULT_RAILYARD_URL, ownership_tag
from railyard_sync.export.run import SyncRefused, sync_to_netbox

SYNC = pathlib.Path(__file__).parent.parent / "fixtures" / "sync"
DOCS = {name: json.loads((SYNC / f"netbox-sync-{name}.json").read_text()) for name in ("cabled", "cabled-4.4")}

OBJECT_COUNTS = {
    "dcim/manufacturers": 1,
    "dcim/device-types": 4,
    "dcim/device-roles": 4,
    "dcim/sites": 1,
    "dcim/locations": 2,
    "dcim/racks": 1,
    "dcim/devices": 4,
    "dcim/interfaces": 2,
    "dcim/rear-ports": 2,
    "dcim/front-ports": 2,
    "dcim/power-outlets": 2,
    "dcim/power-ports": 1,
    "dcim/cables": 3,
}


def document(name: str = "cabled") -> dict:
    return copy.deepcopy(DOCS[name])


def sync(nb: FakeNetBox, doc: dict | None = None, **kw):
    kw.setdefault("dry_run", False)
    return sync_to_netbox(doc or document(), nb.URL, nb.token, session=nb, **kw)


def owner(doc: dict | None = None) -> str:
    doc = doc or document()
    return ownership_tag(DEFAULT_RAILYARD_URL, doc["project"]["id"], doc["project"]["name"]).slug


def drop_device(doc: dict, name: str) -> dict:
    """``doc`` without a device and everything on it (as Railyard would export it)."""
    objects = doc["objects"]
    objects["devices"] = [r for r in objects["devices"] if r["name"] != name]
    for kind in ("interfaces", "rear-ports", "front-ports", "power-outlets", "power-ports"):
        objects[kind] = [r for r in objects[kind] if r["device"] != name]
    objects["cables"] = [c for c in objects["cables"] if name not in (c["side_a_device"], c["side_b_device"])]
    return doc


def rename_device(doc: dict, old: str, new: str) -> dict:
    for kind in ("devices",):
        for row in doc["objects"][kind]:
            if row["name"] == old:
                row["name"] = new
    for kind in ("interfaces", "rear-ports", "front-ports", "power-outlets", "power-ports"):
        for row in doc["objects"][kind]:
            if row["device"] == old:
                row["device"] = new
    for row in doc["objects"]["cables"]:
        for side in ("side_a_device", "side_b_device"):
            if row[side] == old:
                row[side] = new
    return doc


def operator_device(nb: FakeNetBox, name: str, site: int, *, rack: int | None = None, **fields) -> int:
    mfr = (
        nb.add("dcim/manufacturers", name="OperatorCo", slug="operatorco")
        if not nb.all("dcim/manufacturers", slug="operatorco")
        else nb.one("dcim/manufacturers", slug="operatorco")["id"]
    )
    dtype = nb.all("dcim/device-types", model="OP1") or [
        {"id": nb.add("dcim/device-types", manufacturer=mfr, model="OP1", slug="op1", u_height=1)}
    ]
    role = nb.all("dcim/device-roles", slug="operator") or [
        {"id": nb.add("dcim/device-roles", name="operator", slug="operator", color="aaaaaa")}
    ]
    return nb.add(
        "dcim/devices",
        name=name,
        site=site,
        rack=rack,
        device_type=dtype[0]["id"],
        role=role[0]["id"],
        status="active",
        **fields,
    )


# ---- first export, re-run, dry run -----------------------------------------------------------------


def test_first_export_creates_everything_with_the_ownership_tag():
    nb = FakeNetBox()
    result = sync(nb)
    assert result.ok, result.errors
    assert result.diff["create"] == result.created == sum(OBJECT_COUNTS.values())
    slug = owner()
    for endpoint, n in OBJECT_COUNTS.items():
        assert len(nb.tagged(endpoint, slug)) == len(nb.objects[endpoint]) == n, endpoint

    tag = nb.one("extras/tags", slug=slug)
    assert tag["description"].startswith("Managed by the Railyard sync. railyard-sync:v1 project=prj_sync_fixture")
    assert nb.one("extras/custom-fields", name="railyard_id")["object_types"] == ["dcim.device"]

    site = nb.one("dcim/sites", name="DC1")
    hall = nb.one("dcim/locations", name="Hall 1")
    row = nb.one("dcim/locations", name="A")
    assert row["parent"] == hall["id"] and row["site"] == site["id"]
    rack = nb.one("dcim/racks", name="R1")
    assert (rack["status"], rack["width"], rack["location"], rack["comments"]) == (
        "planned",
        23,
        row["id"],
        "Cold aisle A",
    )
    assert set(rack["tags"]) == {tag["id"], nb.tag_id("build-1")}  # the design's tag, created on the way

    srv = nb.one("dcim/devices", name="SRV-1")
    assert (srv["serial"], srv["comments"], srv["face"], srv["position"]) == ("SN-0001", "Database host", "rear", 1)
    assert srv["custom_fields"] == {"railyard_id": "p_srv"}
    pdu = nb.one("dcim/devices", name="PDU-1")
    assert pdu["position"] is None and "face" not in pdu  # 0U: no position, no face

    l1 = nb.one("dcim/cables", label="L1")
    assert l1["color"] == "ff0000" and l1["status"] == "connected"
    assert nb.one("dcim/cables", type="power")["label"] == ""


def test_rerun_is_a_no_op():
    nb = FakeNetBox()
    sync(nb)
    writes = len(nb.writes)
    again = sync(nb)
    assert again.diff == {"create": 0, "update": 0, "delete": 0, "no-change": sum(OBJECT_COUNTS.values())}
    assert (again.created, again.updated, again.deleted) == (0, 0, 0)
    assert again.changes == [] and again.ok
    assert len(nb.writes) == writes  # nothing written at all


def test_dry_run_makes_no_writes():
    nb = FakeNetBox()
    first = sync(nb, dry_run=True)
    assert nb.writes == []
    assert first.diff["create"] == sum(OBJECT_COUNTS.values()) and first.created == 0
    by_type = {model: OBJECT_COUNTS[endpoint] for model, endpoint in ENDPOINT.items()}
    assert first.planned == {"create": by_type, "update": {}, "delete": {}}
    assert any("doesn't exist yet" in w for w in first.warnings)

    sync(nb)
    writes = len(nb.writes)
    doc = drop_device(rename_device(document(), "SW-1", "SW-1A"), "SRV-1")
    preview = sync(nb, doc, dry_run=True, allow_deletes=True)
    assert len(nb.writes) == writes
    assert preview.renamed == ["device SW-1 → SW-1A"]
    assert any(line.startswith("delete: device [SRV-1]") for line in preview.changes)
    assert preview.diff["delete"] > 0 and preview.deleted == 0
    assert preview.planned["update"]["device"] == 1 and preview.planned["delete"]["device"] == 1
    assert sum(preview.planned["delete"].values()) == preview.diff["delete"]
    assert nb.one("dcim/devices", name="SW-1")  # still under its old name


def test_token_auth_scheme_follows_the_token_version():
    v1_key = "0123456789abcdef" * 2 + "01234567"  # a v1 key: 40 hex digits, no nbt_ prefix
    v1 = FakeNetBox(token=v1_key)
    sync(v1, dry_run=True)
    assert v1.calls[0]["headers"]["Authorization"] == f"Token {v1_key}"
    v2 = FakeNetBox()
    sync(v2, dry_run=True)
    assert v2.calls[0]["headers"]["Authorization"] == f"Bearer {v2.token}"


# ---- updates and renames ---------------------------------------------------------------------------


def test_changed_attributes_update_the_owned_objects():
    nb = FakeNetBox()
    sync(nb)
    doc = document()
    srv = next(r for r in doc["objects"]["devices"] if r["name"] == "SRV-1")
    srv.update(position="5", serial="SN-0002", tags="build-1")
    next(c for c in doc["objects"]["cables"] if c["label"] == "L2")["color"] = "00ff00"
    result = sync(nb, doc)
    assert result.ok, result.errors
    assert result.diff["update"] == result.updated == 2
    dev = nb.one("dcim/devices", name="SRV-1")
    assert (dev["position"], dev["serial"]) == (5, "SN-0002")
    assert nb.tag_id("build-1") in dev["tags"] and nb.tag_id(owner()) in dev["tags"]
    assert nb.one("dcim/cables", label="L2")["color"] == "00ff00"
    assert sync(nb, doc).diff["update"] == 0


def test_a_renamed_device_is_renamed_in_place():
    nb = FakeNetBox()
    sync(nb)
    before = nb.one("dcim/devices", name="SW-1")
    eth1 = nb.one("dcim/interfaces", device=before["id"], name="Eth1")
    cable = eth1["cable"]

    doc = rename_device(document(), "SW-1", "SW-1A")
    result = sync(nb, doc, allow_deletes=True)
    assert result.ok, result.errors
    assert result.renamed == ["device SW-1 → SW-1A"]
    assert (result.diff["create"], result.diff["delete"], result.updated) == (0, 0, 1)
    after = nb.one("dcim/devices", name="SW-1A")
    assert after["id"] == before["id"]  # the same NetBox device: history, components and cables kept
    assert nb.one("dcim/interfaces", name="Eth1", device=after["id"])["cable"] == cable
    assert not nb.all("dcim/devices", name="SW-1")
    assert sync(nb, doc).diff == {"create": 0, "update": 0, "delete": 0, "no-change": sum(OBJECT_COUNTS.values())}


# ---- shared objects and conflicts ------------------------------------------------------------------


def test_existing_shared_objects_are_used_but_never_tagged_or_changed():
    nb = FakeNetBox()
    site = nb.add("dcim/sites", name="DC1", slug="dc1", status="planned", facility="")
    rack = nb.add("dcim/racks", name="R1", site=site, location=None, status="active", width=19, u_height=48)
    mfr = nb.add("dcim/manufacturers", name="Acme", slug="acme", description="operator's")
    nb.add("dcim/device-types", manufacturer=mfr, model="SW1", slug="acme-sw1", u_height=1)

    result = sync(nb)
    assert result.ok, result.errors
    referenced = "\n".join(result.referenced)
    for what in ("site DC1", "rack DC1/R1", "manufacturer Acme", "device type Acme SW1"):
        assert what in referenced
    for endpoint, obj_id in (("dcim/sites", site), ("dcim/racks", rack), ("dcim/manufacturers", mfr)):
        assert nb.objects[endpoint][obj_id]["tags"] == []
    assert nb.objects["dcim/sites"][site]["status"] == "planned"  # Railyard says active: not changed
    assert nb.objects["dcim/racks"][rack]["u_height"] == 48  # Railyard says 42U: not changed
    # the sync's own devices went into the operator's rack, and follow its (lack of) location
    ours = nb.tagged("dcim/devices", owner())
    assert len(ours) == 4 and {d["rack"] for d in ours} == {rack} and {d["location"] for d in ours} == {None}

    again = sync(nb, allow_deletes=True)
    assert (again.diff["create"], again.diff["update"], again.diff["delete"]) == (0, 0, 0)
    assert nb.objects["dcim/sites"][site]["status"] == "planned"


def test_same_named_unowned_device_is_a_conflict_and_untouched():
    nb = FakeNetBox()
    site = nb.add("dcim/sites", name="DC1", slug="dc1", status="active")
    theirs = operator_device(nb, "sw-1", site, serial="KEEP-ME")  # NetBox names are unique ignoring case

    result = sync(nb)
    assert result.ok, result.errors
    assert any("device SW-1 in site DC1 already exists" in c for c in result.conflicts)
    assert result.dependents_skipped == 2  # its interface and cable L1
    obj = nb.objects["dcim/devices"][theirs]
    assert (obj["tags"], obj["serial"], obj["name"]) == ([], "KEEP-ME", "sw-1")
    assert not nb.all("dcim/interfaces", device=theirs)
    assert len(nb.tagged("dcim/devices", owner())) == 3
    assert {c["label"] for c in nb.objects["dcim/cables"].values()} == {"L2", ""}


def test_device_type_whose_slug_is_taken_is_a_conflict():
    nb = FakeNetBox()
    mfr = nb.add("dcim/manufacturers", name="Acme", slug="acme")
    nb.add("dcim/device-types", manufacturer=mfr, model="Something else", slug="acme-srv1", u_height=1)
    result = sync(nb)
    assert any("slug 'acme-srv1' is used by another type" in c for c in result.conflicts)
    assert any("device SRV-1: skipped because its device type was skipped" in c for c in result.conflicts)
    assert not nb.all("dcim/devices", name="SRV-1")
    assert result.ok, result.errors  # skipped up front, so nothing failed


def test_a_port_already_cabled_by_an_operator_is_skipped():
    nb = FakeNetBox()
    sync(nb)
    l1 = nb.one("dcim/cables", label="L1")
    nb._delete("dcim/cables", l1["id"])  # someone re-patches SW-1:Eth1 by hand
    sw = nb.one("dcim/devices", name="SW-1")
    eth1 = nb.one("dcim/interfaces", device=sw["id"], name="Eth1")
    other = operator_device(nb, "OPS-1", sw["site"])
    port = nb.add("dcim/interfaces", device=other, name="eth0", type="1000base-t", cable=None)
    theirs = nb._create_cable(
        {
            "a_terminations": [{"object_type": "dcim.interface", "object_id": eth1["id"]}],
            "b_terminations": [{"object_type": "dcim.interface", "object_id": port}],
            "label": "operator",
            "tags": [],
        }
    )

    result = sync(nb, allow_deletes=True)
    assert any("SW-1:Eth1 is already cabled" in c for c in result.conflicts)
    assert nb.objects["dcim/cables"][theirs["id"]]["label"] == "operator"
    assert not nb.all("dcim/cables", label="L1")
    assert result.ok, result.errors


def test_template_components_on_an_owned_device_are_adopted():
    nb = FakeNetBox()
    mfr = nb.add("dcim/manufacturers", name="Acme", slug="acme")
    dt = nb.add("dcim/device-types", manufacturer=mfr, model="SW1", slug="acme-sw1", u_height=1)
    nb.add("dcim/interface-templates", device_type=dt, name="Eth1", type="10gbase-x-sfpp")
    nb.add("dcim/interface-templates", device_type=dt, name="Eth48", type="10gbase-x-sfpp")

    result = sync(nb)
    assert result.ok, result.errors
    assert result.adopted == ["interface SW-1:Eth1"]
    sw = nb.one("dcim/devices", name="SW-1")
    assert nb.tag_id(owner()) in nb.one("dcim/interfaces", device=sw["id"], name="Eth1")["tags"]
    assert nb.one("dcim/interfaces", device=sw["id"], name="Eth48")["tags"] == []
    sync(nb, allow_deletes=True)  # a mirror leaves the template interface Railyard doesn't mention alone
    assert nb.all("dcim/interfaces", device=sw["id"], name="Eth48")


# ---- deletes ---------------------------------------------------------------------------------------


def test_deletes_need_allow_deletes():
    nb = FakeNetBox()
    sync(nb)
    doc = drop_device(document(), "SRV-1")
    kept = sync(nb, doc)
    assert kept.deleted == 0 and kept.diff["delete"] == 0
    assert "device [SRV-1]" in kept.stale and any(s.startswith("cable [") for s in kept.stale)
    assert nb.all("dcim/devices", name="SRV-1")

    gone = sync(nb, doc, allow_deletes=True)
    assert gone.ok, gone.errors
    assert not nb.all("dcim/devices", name="SRV-1")
    assert {c["label"] for c in nb.objects["dcim/cables"].values()} == {"L1"}
    assert gone.applied["delete"]["device"] == 1 and gone.kept == []


def test_a_delete_that_would_reach_unowned_objects_is_refused():
    nb = FakeNetBox()
    sync(nb)
    rack = nb.one("dcim/racks", name="R1")
    theirs = operator_device(nb, "OPS-1", rack["site"], rack=rack["id"])
    doc = document()
    for kind in doc["objects"]:
        if kind not in ("tags", "manufacturers", "device-types", "device-roles", "sites", "locations"):
            doc["objects"][kind] = []

    result = sync(nb, doc, allow_deletes=True)
    assert result.ok, result.errors
    assert theirs in nb.objects["dcim/devices"]
    assert rack["id"] in nb.objects["dcim/racks"]  # kept: it holds an operator device
    assert any("rack DC1__R1: still in use by 1 device this sync doesn't delete" in k for k in result.kept)
    assert nb.tagged("dcim/devices", owner()) == []  # the sync's own devices went


def test_a_device_with_an_operator_cable_or_ip_address_is_kept():
    nb = FakeNetBox()
    sync(nb)
    srv = nb.one("dcim/devices", name="SRV-1")
    mine = nb.add("dcim/interfaces", device=srv["id"], name="mgmt0", type="1000base-t", cable=None)
    other = operator_device(nb, "OPS-1", srv["site"])
    theirs = nb.add("dcim/interfaces", device=other, name="eth0", type="1000base-t", cable=None)
    cable = nb._create_cable(
        {
            "a_terminations": [{"object_type": "dcim.interface", "object_id": mine}],
            "b_terminations": [{"object_type": "dcim.interface", "object_id": theirs}],
            "tags": [],
        }
    )
    iface1 = nb.one("dcim/interfaces", device=srv["id"], name="iface1")
    ip = nb.add(
        "ipam/ip-addresses",
        address="10.0.0.5/24",
        assigned_object_type="dcim.interface",
        assigned_object_id=iface1["id"],
    )

    result = sync(nb, drop_device(document(), "SRV-1"), allow_deletes=True)
    assert srv["id"] in nb.objects["dcim/devices"]
    assert cable["id"] in nb.objects["dcim/cables"] and ip in nb.objects["ipam/ip-addresses"]
    kept = "\n".join(result.kept)
    assert "device SRV-1: connected by 1 cable not managed by this sync" in kept
    assert "would also delete 1 IP address not managed by this sync" in kept
    assert "interface SRV-1__iface1: would also delete 1 IP address" in kept


def test_untagged_objects_are_never_deleted():
    nb = FakeNetBox()
    sync(nb)
    pdu = nb.one("dcim/devices", name="PDU-1")
    pdu["tags"] = []  # an operator takes it over by removing the tag
    result = sync(nb, drop_device(document(), "PDU-1"), allow_deletes=True)
    assert pdu["id"] in nb.objects["dcim/devices"]
    assert not any("PDU-1" in line for line in result.changes if line.startswith("delete: device"))


def test_dry_run_previews_refused_deletes():
    nb = FakeNetBox()
    sync(nb)
    rack = nb.one("dcim/racks", name="R1")
    operator_device(nb, "OPS-1", rack["site"], rack=rack["id"])
    doc = document()
    for kind in (
        "racks",
        "devices",
        "interfaces",
        "rear-ports",
        "front-ports",
        "power-outlets",
        "power-ports",
        "cables",
    ):
        doc["objects"][kind] = []
    writes = len(nb.writes)
    preview = sync(nb, doc, dry_run=True, allow_deletes=True)
    assert len(nb.writes) == writes
    assert any(k.startswith("rack DC1__R1: still in use by 1 device") for k in preview.kept)
    assert not any("device" in k.split(":")[0] and "SW-1" in k for k in preview.kept)  # this run deletes those


# ---- patch panels: front/rear mapping in both API shapes -------------------------------------------


@pytest.mark.parametrize("version, doc_name", [("4.4.9", "cabled-4.4"), ("4.5.0", "cabled"), ("4.6.0", "cabled")])
def test_patch_panel_front_ports_map_onto_rear_ports(version, doc_name):
    nb = FakeNetBox(version=version)
    result = sync(nb, document(doc_name))
    assert result.ok, result.errors
    rears = {r["name"]: r["id"] for r in nb.all("dcim/rear-ports")}
    for front in nb.all("dcim/front-ports"):
        if nb.mappings:
            assert "rear_port" not in front and front["positions"] == 1
            assert front["rear_ports"] == [{"position": 1, "rear_port": rears[front["name"]], "rear_port_position": 1}]
        else:
            assert "rear_ports" not in front
            assert (front["rear_port"], front["rear_port_position"]) == (rears[front["name"]], 1)
    assert sync(nb, document(doc_name)).diff["update"] == 0  # read back in the same shape

    doc = document(doc_name)
    next(f for f in doc["objects"]["front-ports"] if f["name"] == "2")["rear_port"] = "1"
    moved = sync(nb, doc)
    assert moved.ok and moved.updated == 1, moved.errors
    front2 = nb.one("dcim/front-ports", name="2")
    target = front2["rear_ports"][0]["rear_port"] if nb.mappings else front2["rear_port"]
    assert target == rears["1"]


# ---- prerequisites ---------------------------------------------------------------------------------


def test_custom_field_creation_denied_syncs_without_railyard_ids():
    nb = FakeNetBox()
    nb.forbid.add(("POST", "extras/custom-fields"))
    result = sync(nb)
    assert result.ok, result.errors
    assert any("couldn't be created (the token may not create custom fields)" in w for w in result.warnings)
    assert all("custom_fields" not in d for d in nb.all("dcim/devices"))
    assert sync(nb).diff["update"] == 0


def test_a_tag_with_our_slug_but_another_description_is_refused():
    nb = FakeNetBox()
    nb.add("extras/tags", name="Operator tag", slug=owner(), description="mine", color="ff0000")
    with pytest.raises(SyncRefused, match="is not this project's Railyard ownership tag"):
        sync(nb)
    assert nb.writes == []


def test_unsupported_netbox_version_is_refused():
    nb = FakeNetBox(version="3.7.8")
    with pytest.raises(NetBoxError, match="not supported"):
        sync(nb, dry_run=True)


def test_component_templates_from_the_devicetype_library():
    from railyard_sync.export.devicetype_library import DeviceTypeLibrary

    yaml = "manufacturer: Acme\nmodel: PP24\nslug: acme-pp24\nu_height: 1\n" + (
        "rear-ports:\n  - {name: '1', type: lc, positions: 1}\n  - {name: '2', type: lc, positions: 1}\n"
        "front-ports:\n  - {name: '1', type: lc, rear_port: '1'}\n  - {name: '2', type: lc, rear_port: '2'}\n"
    )
    library = DeviceTypeLibrary(fetcher=lambda url: yaml if url.endswith("/PP24.yaml") else None)
    nb = FakeNetBox()
    result = sync(nb, import_components=True, devicetype_library=library)
    assert result.ok, result.errors
    pp = nb.one("dcim/device-types", model="PP24")
    assert {t["name"] for t in nb.all("dcim/front-port-templates", device_type=pp["id"])} == {"1", "2"}
    # NetBox made PP-1's ports from those templates; the sync adopted them and mapped the fronts
    assert sorted(result.adopted) == ["front port PP-1:1", "front port PP-1:2", "rear port PP-1:1", "rear port PP-1:2"]
    rears = {r["name"]: r["id"] for r in nb.tagged("dcim/rear-ports", owner())}
    for front in nb.tagged("dcim/front-ports", owner()):
        assert front["rear_ports"][0]["rear_port"] == rears[front["name"]]
    assert sync(nb, import_components=True, devicetype_library=library).diff["update"] == 0


# ---- the token never leaks ---------------------------------------------------------------------------


def test_a_rejected_token_is_not_echoed():
    nb = FakeNetBox()
    with pytest.raises(NetBoxAuthError) as exc:
        sync_to_netbox(document(), nb.URL, "nbt_wrong.guess-me", session=nb)
    assert "guess-me" not in str(exc.value) and "nbt_wrong" not in str(exc.value)


def test_the_token_is_scrubbed_from_netbox_errors_and_connection_errors():
    nb = FakeNetBox()

    class Echo:
        def request(self, method, url, **kw):
            if url.endswith("/api/status/"):
                return nb.request(method, url, **kw)
            return Response(500, {"detail": f"boom: {kw['headers']['Authorization']}"})

    with pytest.raises(NetBoxError) as exc:
        sync_to_netbox(document(), nb.URL, nb.token, session=Echo())
    assert nb.token not in str(exc.value) and "s3cr3t" not in str(exc.value) and "***" in str(exc.value)

    class Down:
        def request(self, method, url, **kw):
            raise ConnectionError(f"proxy said no to {kw['headers']['Authorization']}")

    with pytest.raises(NetBoxConnectionError) as exc:
        sync_to_netbox(document(), nb.URL, nb.token, session=Down())
    assert nb.token not in str(exc.value) and "s3cr3t" not in str(exc.value)


def test_the_token_is_not_in_the_client_repr_or_the_result():
    nb = FakeNetBox()
    client = NetBoxClient(nb.URL, nb.token, session=nb)
    assert nb.token not in repr(client)
    result = sync(nb)
    assert nb.token not in json.dumps(result.as_dict())


def test_failed_writes_are_reported_and_the_sync_carries_on():
    nb = FakeNetBox()
    nb.forbid.add(("POST", "dcim/power-outlets"))
    result = sync(nb)
    assert not result.ok
    assert any(e.startswith("create power outlet PDU-1__OUT1: NetBox refused POST") for e in result.errors)
    assert any("create cable" in e for e in result.errors)  # the power cable needs that outlet
    assert nb.all("dcim/devices", name="PDU-1") and nb.one("dcim/cables", label="L1")
    assert all(nb.token not in e for e in result.errors)

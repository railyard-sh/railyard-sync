"""End to end: the real NetBox loader, builder and merge behind the CLI, against a fake NetBox and a fake
Railyard. A first import creates the estate; a refresh that NetBox did not change saves nothing; a NetBox
rename updates the estate in place while a device designed in Railyard survives.

The round trip back out (with ``RAILYARD_BIN``, a built Railyard CLI, which writes the NetBox sync document
the fake Railyard serves for the saved estate): ``export netbox --apply`` into an empty NetBox (the export's
own fake) creates the estate's racks, devices and cables under the project's ownership tag."""

from __future__ import annotations

import io
import json
import os
import subprocess

import pytest
from conftest import FakeResponse
from dcim.fake_netbox import BASE as NETBOX, V2_TOKEN, FakeNetBox
from export.fake_netbox_rest import FakeNetBox as ExportNetBox
from test_cli import ORGS, PAT, FakeRailyard

from railyard_sync import cli, client as client_module
from railyard_sync.dcim.netbox import load_netbox_snapshot
from railyard_sync.export import netbox_rest
from railyard_sync.export.policy import ownership_tag

ARGS = ["import", "netbox", "--netbox-url", NETBOX, "--site", "ldn1", "--railyard-url", "https://railyard.sh"]
ARGS += ["--org", ORGS[0]["slug"]]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("RAILYARD_TOKEN", PAT)
    monkeypatch.setenv("NETBOX_TOKEN", V2_TOKEN)
    netbox, railyard = FakeNetBox(), FakeRailyard()

    def load(url, token, sites, *, verify=True):
        return load_netbox_snapshot(url, token, sites, session=netbox.session, verify=verify)

    monkeypatch.setattr(cli, "load_netbox_snapshot", load)
    monkeypatch.setattr(cli, "make_catalogue", lambda client: None)  # every type built from NetBox
    monkeypatch.setattr(client_module, "_default_session", lambda: railyard.session)
    monkeypatch.setattr(cli, "today", lambda: "2026-10-06")
    return netbox, railyard


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    text = out.getvalue() + err.getvalue()
    assert PAT not in text and V2_TOKEN not in text
    return code, text


def placements(project):
    return {p["id"]: p for rack in project["racks"] for p in rack["placements"]}


def test_import_then_refresh_round_trip(world, tmp_path):
    netbox, railyard = world

    code, text = run([*ARGS, "--name", "LDN1 baseline"])
    assert code == 0, text
    first = railyard.project
    assert first["name"] == "LDN1 baseline"
    assert first["racks"] and all(r["id"].startswith("nb-rack-") for r in first["racks"])
    devices = placements(first)
    assert any(pid.startswith("nb-dev-") for pid in devices)
    assert first["meta"]["railyardSync"]["source"] == "netbox"
    puts = len(railyard.calls("PUT"))

    # A refresh NetBox did not change saves nothing.
    code, text = run([*ARGS, "--project", first["id"]])
    assert code == 0, text
    assert len(railyard.calls("PUT")) == puts

    # Design something in Railyard: a device in an imported rack, clear of NetBox's devices.
    rack = first["racks"][0]
    used = {
        u
        for p in rack["placements"]
        if p.get("mount") != "zeroU"
        for u in range(p["startU"], p["startU"] + p["heightU"])
    }
    free = next(u for u in range(1, rack["uHeight"] + 1) if u not in used)
    rack["placements"].append({"id": "my-server", "startU": free, "heightU": 1, "face": "front", "label": "planned"})
    railyard.revision += 1

    # NetBox renames a device; the refresh updates it in place and keeps the Railyard design.
    leaf = netbox.item("devices", 40)
    leaf["name"] = "ldn1-leaf1-renamed"
    code, text = run([*ARGS, "--project", first["id"]])
    assert code == 0, text
    saved = railyard.project
    assert railyard.calls("PUT")[-1]["headers"].get("If-Match")
    assert placements(saved)["nb-dev-40"]["label"] == "ldn1-leaf1-renamed"
    assert "my-server" in placements(saved)

    # Railyard's own loader accepts what was saved, when a Railyard CLI is available.
    binary = os.environ.get("RAILYARD_BIN")
    if binary:
        path = tmp_path / "project.json"
        path.write_text(json.dumps(saved))
        result = subprocess.run(
            [binary, "export", "--format", "json", str(path), "-o", str(tmp_path / "out")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def railyard_deliverable(binary: str, tmp_path, served: list[dict]):
    """The fake Railyard's netbox-sync deliverable: the saved estate exported by the real Railyard CLI."""

    def deliverable(project, body):
        version = (body.get("options") or {}).get("netboxVersion") or ""
        path, out = tmp_path / "estate.json", tmp_path / "netbox-sync.json"
        path.write_text(json.dumps(project))
        cmd = [binary, "export", "--format", "netbox-sync", "-o", str(out)]
        cmd += ["--netbox-version", version] if version else []
        result = subprocess.run([*cmd, str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        doc = json.loads(out.read_text())
        doc["project"].setdefault("revision", 1)  # the server adds it; an offline export cannot know it
        served.append(doc)
        return FakeResponse(200, doc)

    return deliverable


def test_import_then_export_round_trip(world, tmp_path, monkeypatch):
    binary = os.environ.get("RAILYARD_BIN")
    if not binary:
        pytest.skip("set RAILYARD_BIN to a built Railyard CLI to write the NetBox sync document")
    _, railyard = world
    code, text = run([*ARGS, "--name", "LDN1 baseline"])
    assert code == 0, text
    project = railyard.project

    served: list[dict] = []
    railyard.deliverable = railyard_deliverable(binary, tmp_path, served)
    target = ExportNetBox(version="4.5.0", token=V2_TOKEN)
    monkeypatch.setattr(netbox_rest, "_default_session", lambda: target)
    export = ["export", "netbox", "--railyard-url", "https://railyard.sh", "--org", ORGS[0]["slug"]]
    export += ["--project", project["id"], "--netbox-url", NETBOX]

    code, text = run([*export, "--apply"])
    assert code == 0, text
    (doc,) = served
    assert doc["netboxVersion"] == "4.5"  # what the target NetBox reported
    slug = ownership_tag("https://railyard.sh", project["id"]).slug
    for endpoint, kind in (("dcim/racks", "racks"), ("dcim/devices", "devices"), ("dcim/cables", "cables")):
        assert doc["objects"][kind], kind
        assert len(target.tagged(endpoint, slug)) == len(target.objects[endpoint]) == len(doc["objects"][kind])
    racks = {r["name"] for r in target.objects["dcim/racks"].values()}
    assert racks == {r["name"] for r in project["racks"]}
    devices = {d["name"] for d in target.objects["dcim/devices"].values()}
    assert {p["label"] for p in placements(project).values() if p.get("label")} <= devices

    writes = len(target.writes)
    code, text = run([*export, "--apply"])
    assert code == 0, text
    assert len(target.writes) == writes  # the estate is already in NetBox: nothing to write

"""``railyard-sync import nautobot`` and ``export nautobot``: the NetBox commands' flags, tokens, preflight, logging and
exit codes, for Nautobot. End to end against the fake Nautobot of each side (the loader's, serving what a real
Nautobot 2.4 returned, and the export's in-memory one) and the fake Railyard; with ``RAILYARD_BIN`` the estate an
import saved is exported back out through the real ``nautobot-sync`` document."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess

import pytest
from conftest import FIXTURES, FakeResponse
from dcim.fake_nautobot import BASE as NAUTOBOT, HALL_1, LDN1, FakeNautobot
from export.fake_nautobot_rest import FakeNautobot as ExportNautobot
from test_cli import ORGS, PAT, FakeRailyard

from railyard_sync import cli, client as client_module, dcim_http
from railyard_sync.dcim.nautobot import load_nautobot_snapshot
from railyard_sync.dcim.snapshot import Site, Snapshot
from railyard_sync.export.policy import ownership_tag

RAILYARD = "https://railyard.sh"
IMPORT = ["import", "nautobot", "--nautobot-url", NAUTOBOT, "--railyard-url", RAILYARD, "--org", ORGS[0]["slug"]]
EXPORT = ["export", "nautobot", "--railyard-url", RAILYARD, "--org", ORGS[0]["slug"], "--project", "cabled"]
EXPORT += ["--nautobot-url", ExportNautobot.URL]
DOC = json.loads((FIXTURES / "sync" / "nautobot-sync-cabled.json").read_text())


@pytest.fixture
def world(monkeypatch, tmp_path):
    """The loader's fake Nautobot (for imports), the export's (for exports) and a fake Railyard."""
    monkeypatch.chdir(tmp_path)
    source, target, railyard = FakeNautobot(), ExportNautobot(), FakeRailyard()
    monkeypatch.setenv("RAILYARD_TOKEN", PAT)
    monkeypatch.setenv("NAUTOBOT_TOKEN", source.token)
    loads: list[dict] = []

    def load(url, token, locations, *, verify=True):
        loads.append({"url": url, "locations": locations, "verify": verify})
        return load_nautobot_snapshot(url, token, locations, session=source.session, verify=verify)

    monkeypatch.setattr(cli, "load_nautobot_snapshot", load)
    monkeypatch.setattr(cli, "make_catalogue", lambda client: None)  # every type built from Nautobot
    monkeypatch.setattr(client_module, "_default_session", lambda: railyard.session)
    monkeypatch.setattr(dcim_http, "default_session", lambda: target)
    monkeypatch.setattr(cli, "today", lambda: "2026-10-07")
    railyard.deliverable = lambda project, body: FakeResponse(200, copy.deepcopy(DOC))
    return source, target, railyard, loads


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    text = out.getvalue() + err.getvalue()
    assert PAT not in text and FakeNautobot().token not in text  # never echo a token
    return code, out.getvalue(), err.getvalue()


def placements(project: dict) -> dict[str, dict]:
    return {p["id"]: p for rack in project["racks"] for p in rack["placements"]}


# ---- import -------------------------------------------------------------------------------------------


def test_import_creates_an_estate_from_a_location(world):
    _, _, railyard, loads = world
    code, out, err = run([*IMPORT, "--location", "LDN1", "--name", "LDN1 baseline", "--name-version"])
    assert code == cli.EXIT_OK, err
    assert loads == [{"url": NAUTOBOT, "locations": ["LDN1"], "verify": True}]
    project = railyard.project
    assert project["name"] == "LDN1 baseline"
    assert all(r["id"].startswith("nbt-rack-") for r in project["racks"])
    assert any(c["id"] == f"nbt-site-{LDN1}" and c["type"] == "Site" for c in project["containers"])
    assert project["meta"]["railyardSync"]["source"] == "nautobot"
    assert "Read Nautobot (LDN1): 3 racks" in out
    assert "Named the version 'Nautobot import 2026-10-07'." in out
    [version] = [c for c in railyard.session.calls if c["url"].endswith("/versions")]
    assert version["json"]["title"] == "Nautobot import 2026-10-07"


def test_a_refresh_nautobot_did_not_change_saves_nothing_and_a_change_merges(world):
    source, _, railyard, _ = world
    assert run([*IMPORT, "--location", "LDN1", "--name", "LDN1"])[0] == 0
    first = railyard.project
    puts = len(railyard.calls("PUT"))
    code, out, _ = run([*IMPORT, "--location", "LDN1", "--project", first["id"]])
    assert code == 0 and "is already up to date with Nautobot; nothing was saved." in out
    assert len(railyard.calls("PUT")) == puts

    source.named("devices", "ldn1-leaf1")["name"] = "ldn1-leaf1-renamed"
    code, out, err = run([*IMPORT, "--location", "LDN1", "--project", first["id"]])
    assert code == 0, err
    assert "Refreshed 'LDN1'" in out
    leaf = source.named("devices", "ldn1-leaf1-renamed")["id"]
    assert placements(railyard.project)[f"nbt-dev-{leaf}"]["label"] == "ldn1-leaf1-renamed"
    assert railyard.calls("PUT")[-1]["headers"].get("If-Match")


def test_all_locations_and_its_usage_errors(world):
    _, _, railyard, loads = world
    code, _, err = run([*IMPORT, "--all-locations", "--name", "Everything", "--dry-run"])
    assert code == 0, err
    assert loads[-1]["locations"] is None
    code, _, err = run([*IMPORT, "--all-locations", "--location", "LDN1", "--name", "x"])
    assert code == cli.EXIT_USAGE and "use --location or --all-locations, not both" in err
    code, _, err = run([*IMPORT, "--name", "x"])
    assert code == cli.EXIT_USAGE and "name at least one location with --location" in err
    code, _, err = run(["import", "nautobot", "--location", "LDN1", "--org", "o", "--name", "x"])
    assert code == cli.EXIT_USAGE and "--nautobot-url is required" in err
    code, _, err = run([*IMPORT, "--site", "ldn1", "--name", "x"])
    assert code == cli.EXIT_USAGE  # NetBox's flag


def test_the_nautobot_token_comes_from_the_environment(world, monkeypatch):
    monkeypatch.delenv("NAUTOBOT_TOKEN")
    code, _, err = run([*IMPORT, "--location", "LDN1", "--name", "x"])
    assert code == cli.EXIT_USAGE and "set NAUTOBOT_TOKEN to a Nautobot API token" in err
    code, _, err = run([*IMPORT, "--location", "LDN1", "--name", "x", "--nautobot-token", "abc"])
    assert code == cli.EXIT_USAGE and "NAUTOBOT_TOKEN" in err and "never from the command line" in err


def test_a_refresh_from_fewer_locations_is_refused_naming_them(world):
    _, _, railyard, _ = world
    assert run([*IMPORT, "--location", "LDN1", "--location", "MAN1", "--name", "Two"])[0] == 0
    code, _, err = run([*IMPORT, "--location", "LDN1", "--project", railyard.project["id"]])
    assert code == cli.EXIT_ERROR
    assert "imported from location(s) LDN1, MAN1; import all of them again (--location LDN1 --location MAN1)" in err
    assert "Nothing was saved to Railyard." in err


def test_a_location_inside_the_imported_one_counts_as_imported(world):
    _, _, railyard, _ = world
    assert run([*IMPORT, "--location", "LDN1", "--name", "One"])[0] == 0
    code, _, err = run([*IMPORT, "--location", LDN1, "--project", railyard.project["id"]])  # by its id
    assert code == 0, err


def test_an_unknown_location_is_an_error_with_how_far_it_got(world):
    code, _, err = run([*IMPORT, "--location", "Nowhere", "--name", "x"])
    assert code == cli.EXIT_ERROR
    assert "No Nautobot location is named 'Nowhere'" in err


def test_a_netbox_snapshot_is_not_replayed_as_nautobot(world, tmp_path):
    path = tmp_path / "netbox.json"
    path.write_text(json.dumps(Snapshot(source="netbox", sites=[Site(id="1", name="A", slug="a")]).to_dict()))
    code, _, err = run(["import", "nautobot", "--from-snapshot", str(path), "--org", "acme", "--name", "x"])
    assert code == cli.EXIT_ERROR
    assert "was read from NetBox, not Nautobot: replay it with 'railyard-sync import netbox'" in err


def test_a_snapshot_round_trips_through_a_file(world, tmp_path):
    _, _, railyard, loads = world
    path = tmp_path / "ldn1.snapshot.json"
    assert run([*IMPORT, "--location", "LDN1", "--name", "LDN1", "--snapshot-out", str(path), "--dry-run"])[0] == 0
    code, out, err = run(["import", "nautobot", "--from-snapshot", str(path), "--org", "acme", "--name", "LDN1"])
    assert code == 0, err
    assert len(loads) == 1 and railyard.project["racks"]


def test_conflicts_stop_the_save(world):
    source, _, railyard, _ = world
    assert run([*IMPORT, "--location", "LDN1", "--name", "LDN1"])[0] == 0
    estate = railyard.project
    # In Railyard, someone puts a device where Nautobot's server is about to move.
    rack = next(r for r in estate["racks"] if r["name"] == "A01")
    rack["placements"].append({"id": "mine", "startU": 20, "heightU": 1, "face": "front", "label": "mine"})
    railyard.revision += 1
    source.named("devices", "ldn1-srv1")["position"] = 20
    puts = len(railyard.calls("PUT"))
    code, out, err = run([*IMPORT, "--location", "LDN1", "--project", estate["id"]])
    assert code == cli.EXIT_ERROR
    assert "conflict(s) between Railyard's design and Nautobot" in err
    assert len(railyard.calls("PUT")) == puts


def test_preflight_problems_name_nautobot(world):
    _, _, railyard, _ = world
    railyard.validate = FakeResponse(
        200, {"problems": [{"severity": "error", "code": "placement.overlap", "message": "overlaps", "rackId": "x"}]}
    )
    code, _, err = run([*IMPORT, "--location", "LDN1", "--name", "x"])
    assert code == cli.EXIT_ERROR
    assert "Fix them in Nautobot (or in Railyard, for objects designed there)" in err
    assert railyard.calls("PUT") == []


def test_a_plan_limit_names_locations(world):
    _, _, railyard, _ = world
    railyard.put_response = FakeResponse(
        402,
        {
            "error": "rack limit",
            "code": "plan_limit",
            "plan": "community",
            "resource": "racks",
            "limit": 2,
            "current": 3,
            "scope": "estate",
            "requiredPlans": ["pro"],
            "projectPass": True,
        },
    )
    code, _, err = run([*IMPORT, "--location", "LDN1", "--name", "x"])
    assert code == cli.EXIT_PLAN
    assert "This import has 3 racks; the Community plan allows 2 per estate." in err
    assert "or import fewer locations. Nothing was saved." in err


# ---- export -------------------------------------------------------------------------------------------


def test_export_is_a_dry_run_by_default(world):
    _, target, railyard, _ = world
    code, out, err = run(EXPORT)
    assert code == cli.EXIT_OK, err
    assert target.writes == []
    assert "→ Nautobot 2.4.43 at https://nautobot.example.com" in out
    assert "Dry run, planned: 32 to create" in out
    assert "  location type: 3 to create" in out.replace("location_type", "location type")
    assert "Dry run: nothing was written to Nautobot. Re-run with --apply" in out
    [call] = [c for c in railyard.session.calls if "/deliverables/" in c["url"]]
    assert call["url"].endswith("/api/projects/cabled/deliverables/nautobot-sync") and call["json"] == {}


def test_apply_creates_everything_and_a_second_apply_writes_nothing(world):
    _, target, _, _ = world
    code, out, err = run([*EXPORT, "--apply"])
    assert code == cli.EXIT_OK, err
    assert "Applied: 32 created, 0 updated, 0 deleted" in out and "Nautobot is in step with the Railyard estate." in out
    tag = target.tag(ownership_tag(RAILYARD, DOC["project"]["id"], DOC["project"]["name"]).name)
    assert len(target.tagged("dcim/devices", tag["id"])) == 4
    writes = len(target.writes)
    code, out, _ = run([*EXPORT, "--apply"])
    assert code == 0 and len(target.writes) == writes and "Applied: 0 created" in out


def test_json_output_is_the_result(world):
    code, out, _ = run([*EXPORT, "--json", "--change-request", "cr_9"])
    assert code == 0
    result = json.loads(out)
    assert result["dry_run"] is True and result["nautobot_url"] == ExportNautobot.URL and result["ok"] is True
    assert result["diff"]["create"] == 32


def test_conflicts_exit_1(world):
    _, target, _, _ = world
    target.add("extras/roles", name="leaf", content_types=["dcim.rack"])
    code, out, _ = run([*EXPORT, "--apply"])
    assert code == cli.EXIT_ERROR
    assert "Conflicts (2)" in out and "role leaf exists but isn't enabled for dcim.device" in out


def test_a_plan_without_deliverables_exits_3(world):
    _, target, railyard, _ = world
    railyard.deliverable = lambda project, body: FakeResponse(
        402,
        {
            "error": "no deliverables",
            "code": "plan_required",
            "feature": "deliverables",
            "deliverable": "nautobot-sync",
            "plan": "community",
            "requiredPlans": ["pro", "team"],
            "projectPass": True,
        },
    )
    code, _, err = run([*EXPORT, "--apply"])
    assert code == cli.EXIT_PLAN
    assert "Exporting to Nautobot needs a plan with deliverables: the Community plan does not include them." in err
    assert "Upgrade to Pro or Team, or buy a Project Pass for this estate. Nothing was written to Nautobot." in err
    assert target.writes == []


def test_netbox_only_flags_are_refused(world):
    for extra in (["--netbox-version", "4.5"], ["--import-components"]):
        code, _, _ = run([*EXPORT, *extra])
        assert code == cli.EXIT_USAGE


def test_an_unsupported_nautobot_stops_before_railyard(world):
    _, target, railyard, _ = world
    target.version = "1.6.20"
    code, _, err = run(EXPORT)
    assert code == cli.EXIT_ERROR and "Nautobot 1.6.20 is not supported" in err
    assert not [c for c in railyard.session.calls if "/deliverables/" in c["url"]]


def test_help(capsys):
    assert cli.main(["export", "nautobot", "--help"]) == 0
    text = capsys.readouterr().out
    assert "--nautobot-url" in text and "--apply" in text and "--netbox-version" not in text
    assert cli.main(["import", "nautobot", "--help"]) == 0
    text = capsys.readouterr().out
    assert "--location" in text and "--all-locations" in text


# ---- round trip with Railyard's own exporter ----------------------------------------------------------


def test_import_then_export_round_trip(world, tmp_path):
    binary = os.environ.get("RAILYARD_BIN")
    if not binary:
        pytest.skip("set RAILYARD_BIN to a built Railyard CLI to write the Nautobot sync document")
    _, target, railyard, _ = world
    code, _, err = run([*IMPORT, "--location", "LDN1", "--name", "LDN1 baseline"])
    assert code == 0, err
    project = railyard.project
    served: list[dict] = []

    def deliverable(estate, body):
        path, out = tmp_path / "estate.json", tmp_path / "nautobot-sync.json"
        path.write_text(json.dumps(estate))
        result = subprocess.run(
            [binary, "export", "--format", "nautobot-sync", "-o", str(out), str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        doc = json.loads(out.read_text())
        served.append(doc)
        return FakeResponse(200, doc)

    railyard.deliverable = deliverable
    export = ["export", "nautobot", "--railyard-url", RAILYARD, "--org", ORGS[0]["slug"], "--project", project["id"]]
    export += ["--nautobot-url", ExportNautobot.URL]
    code, out, err = run([*export, "--apply"])
    assert code == 0, out + err
    (doc,) = served
    tag = target.tag(ownership_tag(RAILYARD, project["id"], project["name"]).name)
    for endpoint, kind in (("dcim/racks", "racks"), ("dcim/devices", "devices"), ("dcim/cables", "cables")):
        assert doc["objects"][kind], kind
        assert len(target.tagged(endpoint, tag["id"])) == len(doc["objects"][kind]), kind
    # The location tree comes back typed as Nautobot had it.
    hall = target.one("dcim/locations", name="Data Hall 1")
    assert target.objects["dcim/location-types"][hall["location_type"]]["name"] == "Room"
    assert HALL_1 not in target.objects["dcim/locations"]  # a new Nautobot, new ids
    writes = len(target.writes)
    assert run([*export, "--apply"])[0] == 0 and len(target.writes) == writes

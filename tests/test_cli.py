"""The railyard-sync CLI: argument handling, tokens from the environment only, and the import flow
(create, refresh with If-Match, dry run, plan limit) against a fake Railyard and stubbed loader/builder."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeResponse, FakeSession

from railyard_sync import cli, client as client_module
from railyard_sync.dcim.snapshot import Rack, Site, Snapshot

PAT = "ry_" + "c3" * 12  # a fake Railyard personal access token
NB_PAT = "nb" + "0f" * 16  # a fake NetBox token
NETBOX = "https://netbox.example.com"
ORGS = [{"id": "org_acme", "name": "Acme", "slug": "acme"}]
BASE = ["import", "netbox", "--netbox-url", NETBOX, "--site", "ldn1", "--railyard-url", "https://railyard.sh"]
BASE += ["--org", "acme"]


def snapshot() -> Snapshot:
    return Snapshot(
        source="netbox",
        source_url=NETBOX,
        sites=[Site(id="1", name="LDN1", slug="ldn1")],
        racks=[Rack(id="100", name="A01", site_id="1")],
    )


def imported_doc(project_id: str, name: str) -> dict:
    return {
        "schemaVersion": "1",
        "id": project_id,
        "name": name,
        "containers": [{"id": "nb-site-1", "name": "LDN1", "type": "Site", "layout": "floor", "exportSite": True}],
        "dataCentres": [{"id": "nb-site-1", "name": "LDN1"}],
        "racks": [
            {
                "id": "nb-rack-100",
                "name": "A01",
                "uHeight": 42,
                "containerId": "nb-site-1",
                "dcId": "nb-site-1",
                "indexInRow": 0,
                "placements": [{"id": "nb-dev-1", "startU": 10, "heightU": 1, "face": "front", "label": "sw1"}],
            }
        ],
        "cables": [],
        "powerLinks": [],
        "meta": {"railyardSync": {"source": {"kind": "netbox", "url": NETBOX, "sites": ["ldn1"]}}},
    }


class Report:
    def summary(self) -> str:
        return "Skipped 2 unracked devices."


class FakeRailyard:
    """Just enough of the Railyard API: orgs, one project with a revision, saves, named versions and, when
    ``deliverable`` is set, deliverables (``deliverable(project, request body)`` answers each POST)."""

    def __init__(self, project: dict | None = None, revision: int = 7):
        self.project = project
        self.revision = revision
        self.put_response: FakeResponse | None = None
        self.version_response: FakeResponse | None = None
        self.deliverable = None
        # /api/validate's answer for a document: a FakeResponse, or a callable taking the document.
        self.validate: Any = None
        self.session = FakeSession(self.handle)

    def handle(self, method, url, headers, params):
        path = url.removeprefix("https://railyard.sh")
        if path == "/api/orgs":
            return FakeResponse(200, ORGS)
        if path == "/api/validate" and method == "POST":
            answer = self.validate
            if callable(answer):
                answer = answer(self.session.calls[-1]["json"])
            return answer or FakeResponse(200, {"problems": []})
        if "/deliverables/" in path and method == "POST":
            if self.deliverable is None:
                return FakeResponse(404, {"error": "unknown deliverable"})
            return self.deliverable(self.project, self.session.calls[-1]["json"])
        if path.endswith("/versions") and method == "POST":
            return self.version_response or FakeResponse(201, {"id": "v1"})
        if path.startswith("/api/projects/") and method == "GET":
            if self.project is None:
                return FakeResponse(404, {"error": "project not found"})
            return FakeResponse(200, self.project, headers={"ETag": f'"{self.revision}"'})
        if path.startswith("/api/projects/") and method == "PUT":
            if self.put_response is not None:
                return self.put_response
            self.project = self.session.calls[-1]["json"]
            self.revision += 1
            return FakeResponse(200, {"id": self.project["id"]}, headers={"ETag": f'"{self.revision}"'})
        return FakeResponse(404, {"error": "no route"})

    def calls(self, method: str) -> list[dict]:
        """The calls with ``method``, leaving out the org lookup and the (read-only) document check."""
        return [
            c
            for c in self.session.calls
            if c["method"] == method and "/api/orgs" not in c["url"] and not c["url"].endswith("/api/validate")
        ]

    def validations(self) -> list[dict]:
        return [c for c in self.session.calls if c["url"].endswith("/api/validate")]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RAILYARD_TOKEN", PAT)
    monkeypatch.setenv("NETBOX_TOKEN", NB_PAT)


@pytest.fixture
def stubs(monkeypatch, env, tmp_path):
    """Stub the loader and builder; returns a namespace recording their calls and the fake Railyard. Runs in a
    temporary directory, where a refused save keeps its document."""
    monkeypatch.chdir(tmp_path)
    state = SimpleNamespace(loads=[], builds=[], railyard=FakeRailyard(), catalogue_clients=[])

    def load(url, token, sites, *, verify=True):
        state.loads.append({"url": url, "token": token, "sites": sites, "verify": verify})
        return snapshot()

    def make_catalogue(client):
        state.catalogue_clients.append(client)
        return "catalogue"

    def build(snap, *, project_id, name, catalogue, prefix):
        state.builds.append({"project_id": project_id, "name": name, "catalogue": catalogue, "prefix": prefix})
        return SimpleNamespace(project=imported_doc(project_id, name), report=Report())

    monkeypatch.setattr(cli, "load_netbox_snapshot", load)
    monkeypatch.setattr(cli, "make_catalogue", make_catalogue)
    monkeypatch.setattr(cli, "build_project", build)
    monkeypatch.setattr(client_module, "_default_session", lambda: state.railyard.session)
    monkeypatch.setattr(cli, "today", lambda: "2026-10-06")
    return state


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    text = out.getvalue() + err.getvalue()
    assert PAT not in text and NB_PAT not in text  # never echo a token
    return code, out.getvalue(), err.getvalue()


# ---- arguments and tokens -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [["--railyard-token", PAT], ["=".join(("--token", NB_PAT))], ["--netbox-token", NB_PAT], ["--org", PAT]],
)
def test_a_token_on_the_command_line_is_refused(stubs, extra):
    code, _, err = run([*BASE, "--name", "x", *extra])
    assert code == cli.EXIT_USAGE
    assert "RAILYARD_TOKEN" in err and "never from the command line" in err
    assert stubs.loads == [] and stubs.railyard.session.calls == []


def test_missing_railyard_token_is_a_clear_usage_error(stubs, monkeypatch):
    monkeypatch.delenv("RAILYARD_TOKEN")
    code, _, err = run([*BASE, "--name", "x"])
    assert code == cli.EXIT_USAGE
    assert "set RAILYARD_TOKEN" in err


def test_missing_netbox_token_is_a_clear_usage_error(stubs, monkeypatch):
    monkeypatch.delenv("NETBOX_TOKEN")
    code, _, err = run([*BASE, "--name", "x"])
    assert code == cli.EXIT_USAGE
    assert "set NETBOX_TOKEN" in err
    assert stubs.loads == []


@pytest.mark.parametrize(
    "argv",
    [
        BASE,  # neither --project nor --name
        [*BASE, "--project", "p", "--name", "n"],
        ["import", "netbox", "--netbox-url", NETBOX, "--railyard-url", "https://r", "--org", "o", "--name", "x"],
        ["import", "netbox", "--site", "a", "--railyard-url", "https://r", "--org", "o", "--name", "x"],
        [*BASE, "--name", "  "],
        ["import"],
        [],
    ],
)
def test_usage_errors_exit_2(stubs, argv):
    code, _, _ = run(argv)
    assert code == cli.EXIT_USAGE
    assert stubs.railyard.session.calls == []


def test_help_exits_0(capsys):
    assert cli.main(["import", "netbox", "--help"]) == 0
    assert "--allow-deletes" in capsys.readouterr().out


# ---- create -----------------------------------------------------------------------------------------------


def test_first_import_creates_a_new_estate(stubs):
    code, out, _ = run([*BASE, "--name", "LDN1 baseline"])
    assert code == cli.EXIT_OK
    (load,) = stubs.loads
    assert load == {"url": NETBOX, "token": NB_PAT, "sites": ["ldn1"], "verify": True}
    (build,) = stubs.builds
    assert build["name"] == "LDN1 baseline" and build["prefix"] == "nb" and build["catalogue"] == "catalogue"
    assert build["project_id"].startswith("ry-") and len(build["project_id"]) == 23
    assert stubs.catalogue_clients and stubs.catalogue_clients[0].base_url == "https://railyard.sh"
    (put,) = stubs.railyard.calls("PUT")
    assert "If-Match" not in put["headers"]
    assert put["headers"]["X-Org-Id"] == "org_acme"
    assert put["json"]["id"] == build["project_id"]
    assert "Read NetBox (ldn1): 1 rack, 1 device, 0 cables, 0 power links." in out
    assert "Skipped 2 unracked devices." in out
    assert f"Created 'LDN1 baseline' ({build['project_id']}) at revision 8." in out


def test_insecure_turns_off_tls_verification_with_a_warning(stubs):
    code, _, err = run([*BASE, "--name", "x", "--insecure"])
    assert code == 0
    assert stubs.loads[0]["verify"] is False
    assert "not verifying" in err


def test_name_taken_explains_what_to_do(stubs):
    stubs.railyard.put_response = FakeResponse(409, {"error": "name taken", "code": "name_taken"})
    code, _, err = run([*BASE, "--name", "LDN1 baseline"])
    assert code == cli.EXIT_ERROR
    assert "already exists" in err and "--project" in err


# ---- refresh ---------------------------------------------------------------------------------------------


def existing_estate() -> dict:
    doc = imported_doc("ry-existing0000000000", "LDN1 baseline")
    doc["racks"][0]["placements"][0]["colour"] = "#ff0000"  # set in Railyard
    doc["racks"][0]["placements"][0]["label"] = "old-name"  # renamed in NetBox since
    return doc


def test_reimport_merges_and_saves_with_if_match(stubs):
    stubs.railyard.project = existing_estate()
    code, out, _ = run([*BASE, "--project", "ldn1-baseline"])
    assert code == cli.EXIT_OK
    (get,) = stubs.railyard.calls("GET")
    assert get["url"].endswith("/api/projects/ldn1-baseline")
    (build,) = stubs.builds
    assert (build["project_id"], build["name"]) == ("ry-existing0000000000", "LDN1 baseline")
    (put,) = stubs.railyard.calls("PUT")
    assert put["headers"]["If-Match"] == '"7"'
    assert put["url"].endswith("/api/projects/ry-existing0000000000")
    device = put["json"]["racks"][0]["placements"][0]
    assert (device["label"], device["colour"]) == ("sw1", "#ff0000")  # NetBox's name, Railyard's colour
    assert "devices: 1 updated" in out
    assert "Refreshed 'LDN1 baseline'" in out


def test_reimport_that_changes_nothing_saves_nothing(stubs):
    stubs.railyard.project = imported_doc("ry-existing0000000000", "LDN1 baseline")
    code, out, _ = run([*BASE, "--project", "ry-existing0000000000"])
    assert code == cli.EXIT_OK
    assert stubs.railyard.calls("PUT") == []
    assert "already up to date" in out


def test_a_change_in_railyard_meanwhile_is_not_overwritten(stubs):
    stubs.railyard.project = existing_estate()
    stubs.railyard.put_response = FakeResponse(412, {"error": "revision conflict"})
    code, _, err = run([*BASE, "--project", "ldn1-baseline"])
    assert code == cli.EXIT_ERROR
    assert "changed in Railyard while the import ran" in err


def test_conflicts_stop_the_save(stubs):
    estate = existing_estate()
    estate["racks"][0]["placements"].append(
        {"id": "pl_mine", "startU": 10, "heightU": 2, "face": "front", "label": "planned"}
    )
    stubs.railyard.project = estate
    code, out, err = run([*BASE, "--project", "ldn1-baseline"])
    assert code == cli.EXIT_ERROR
    assert stubs.railyard.calls("PUT") == []
    assert "Railyard-designed device 'planned'" in out
    assert "Nothing was saved" in err


def test_stale_objects_are_kept_unless_allow_deletes(stubs):
    estate = existing_estate()
    estate["racks"][0]["placements"].append({"id": "nb-dev-2", "startU": 20, "heightU": 1, "face": "front"})
    stubs.railyard.project = estate
    code, out, _ = run([*BASE, "--project", "p", "--dry-run"])
    assert code == 0 and "--allow-deletes" in out and "devices: nb-dev-2" in out
    code, out, _ = run([*BASE, "--project", "p", "--allow-deletes"])
    assert code == 0
    saved = stubs.railyard.calls("PUT")[-1]["json"]
    assert [pl["id"] for pl in saved["racks"][0]["placements"]] == ["nb-dev-1"]


def test_refusing_a_different_netbox(stubs):
    estate = existing_estate()
    estate["meta"]["railyardSync"]["source"]["url"] = "https://other-netbox.example.com/"
    stubs.railyard.project = estate
    code, _, err = run([*BASE, "--project", "p"])
    assert code == cli.EXIT_ERROR
    assert "would collide" in err
    assert stubs.builds == []


def test_refusing_a_subset_of_the_imported_sites(stubs):
    estate = existing_estate()
    estate["meta"]["railyardSync"]["source"]["sites"] = ["ldn1", "ldn2"]
    stubs.railyard.project = estate
    code, _, err = run([*BASE, "--project", "p"])
    assert code == cli.EXIT_ERROR
    assert "--site ldn1 --site ldn2" in err


# ---- dry run, files and versions ---------------------------------------------------------------------


def test_dry_run_makes_no_put_and_writes_out(stubs, tmp_path):
    out_file = tmp_path / "project.json"
    code, out, _ = run([*BASE, "--name", "x", "--dry-run", "--out", str(out_file)])
    assert code == cli.EXIT_OK
    assert stubs.railyard.calls("PUT") == [] and stubs.railyard.calls("POST") == []
    assert json.loads(out_file.read_text())["racks"][0]["id"] == "nb-rack-100"
    assert "Dry run" in out and "Nothing was saved" in out


def test_dry_run_reimport_shows_the_diff_without_saving(stubs):
    stubs.railyard.project = existing_estate()
    code, out, _ = run([*BASE, "--project", "p", "--dry-run"])
    assert code == 0
    assert stubs.railyard.calls("PUT") == []
    assert "Re-import changes" in out


def test_snapshot_out_then_from_snapshot_replays_without_netbox(stubs, tmp_path, monkeypatch):
    snap_file = tmp_path / "ldn1.snapshot.json"
    code, _, _ = run([*BASE, "--name", "x", "--dry-run", "--snapshot-out", str(snap_file)])
    assert code == 0
    assert Snapshot.from_dict(json.loads(snap_file.read_text())) == snapshot()

    monkeypatch.delenv("NETBOX_TOKEN")
    argv = ["import", "netbox", "--from-snapshot", str(snap_file), "--railyard-url", "https://railyard.sh"]
    code, _, _ = run([*argv, "--org", "acme", "--name", "replayed"])
    assert code == 0
    assert len(stubs.loads) == 1  # NetBox was read once, by the first run only
    assert stubs.builds[-1]["name"] == "replayed"


def test_name_version_records_the_saved_revision(stubs):
    code, out, _ = run([*BASE, "--name", "x", "--name-version"])
    assert code == 0
    (post,) = stubs.railyard.calls("POST")
    assert post["json"] == {"title": "NetBox import 2026-10-06", "expectedCurrentRevision": 8}
    assert "Named the version 'NetBox import 2026-10-06'" in out


def test_a_failed_version_name_is_only_a_warning(stubs):
    stubs.railyard.version_response = FakeResponse(409, {"error": "off", "code": "version_control_disabled"})
    code, _, err = run([*BASE, "--name", "x", "--name-version"])
    assert code == 0
    assert "naming the version failed" in err and "version control" in err


# ---- plan limit and other failures ---------------------------------------------------------------------


PLAN_LIMIT = {
    "error": "the Community plan includes up to 25 racks per estate; this change would use 140; upgrade to continue",
    "code": "plan_limit",
    "plan": "community",
    "resource": "racks",
    "limit": 25,
    "current": 140,
    "scope": "estate",
    "requiredPlans": ["team", "partner"],
    "projectPass": False,
}


def test_plan_limit_exits_3_with_an_upgrade_message(stubs):
    stubs.railyard.put_response = FakeResponse(402, PLAN_LIMIT)
    code, _, err = run([*BASE, "--name", "x"])
    assert code == cli.EXIT_PLAN
    assert (
        "This import has 140 racks; the Community plan allows 25 per estate. "
        "Upgrade to Team or Partner, or import fewer sites. Nothing was saved."
    ) in err


def test_plan_limit_on_a_refresh_mentions_the_project_pass(stubs):
    stubs.railyard.project = existing_estate()
    stubs.railyard.put_response = FakeResponse(
        402, PLAN_LIMIT | {"requiredPlans": ["pro", "team"], "projectPass": True}
    )
    code, _, err = run([*BASE, "--project", "p"])
    assert code == cli.EXIT_PLAN
    assert "After this import the estate would have 140 racks" in err
    assert "Upgrade to Pro or Team, buy a Project Pass for this estate, or import fewer sites." in err


def test_unknown_org_is_an_error(stubs):
    code, _, err = run([*BASE[:-1], "nope", "--name", "x"])
    assert code == cli.EXIT_ERROR
    assert "No org matched" in err


def test_unknown_project_is_an_error(stubs):
    code, _, err = run([*BASE, "--project", "ghost"])
    assert code == cli.EXIT_ERROR
    assert "Not found" in err


def test_a_dcim_error_is_reported(stubs, monkeypatch):
    class FakeDCIMError(Exception):
        pass

    def fail(*a, **k):
        raise FakeDCIMError("NetBox rejected the token (HTTP 403)")

    monkeypatch.setattr(cli, "dcim_errors", lambda: (FakeDCIMError,))
    monkeypatch.setattr(cli, "load_netbox_snapshot", fail)
    code, _, err = run([*BASE, "--name", "x"])
    assert code == cli.EXIT_ERROR
    assert "NetBox rejected the token" in err


def test_unreadable_snapshot_file_is_an_error(stubs, tmp_path):
    argv = ["import", "netbox", "--from-snapshot", str(tmp_path / "missing.json")]
    code, _, err = run([*argv, "--railyard-url", "https://railyard.sh", "--org", "acme", "--name", "x"])
    assert code == cli.EXIT_ERROR
    assert "missing.json" in err


def test_plan_limit_message_without_upgrade_paths():
    from railyard_sync.errors import RailyardPlanLimitError

    e = RailyardPlanLimitError("x", plan="partner", resource="racks", limit=1000, current=1200, scope="estate")
    message = cli.plan_limit_message(e)
    assert message.startswith("This import has 1200 racks; the Partner plan allows 1000 per estate.")
    assert "Contact Railyard about an Enterprise plan, or import fewer sites." in message


def test_railyard_url_defaults_to_railyard_sh():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["import", "netbox", "--netbox-url", NETBOX, "--site", "ldn1", "--org", "acme", "--name", "x"]
    )
    assert args.railyard_url == "https://railyard.sh"
    args = parser.parse_args(
        [*BASE[:6], "--railyard-url", "https://railyard.example.com", "--org", "acme", "--name", "x"]
    )
    assert args.railyard_url == "https://railyard.example.com"
    args = parser.parse_args(["export", "netbox", "--org", "acme", "--project", "p", "--netbox-url", NETBOX])
    assert args.railyard_url == "https://railyard.sh"


def test_all_sites_loads_every_site(stubs):
    argv = [a for a in BASE if a not in ("--site", "ldn1")] + ["--all-sites", "--name", "Everything"]
    code, _, err = run(argv)
    assert code == 0, err
    assert stubs.loads[-1]["sites"] is None


def test_site_and_all_sites_together_is_a_usage_error(stubs):
    code, _, err = run([*BASE, "--all-sites", "--name", "x"])
    assert code == 2 and "--site or --all-sites" in err

"""``railyard-sync export netbox``: argument handling, tokens from the environment only, and the flow — the
NetBox sync document from a fake Railyard (the fixtures in ``tests/fixtures/sync/``), synced into the
export's in-memory NetBox (``tests/export/fake_netbox_rest.py``). A dry run writes nothing, ``--apply``
creates, a second ``--apply`` is a no-op, and a plan refusal exits 3 with an upgrade message."""

from __future__ import annotations

import copy
import io
import json

import pytest
import structlog
from conftest import FIXTURES, FakeResponse
from export.fake_netbox_rest import FakeNetBox
from test_cli import ORGS, PAT, FakeRailyard

from railyard_sync import cli, client as client_module, dcim_http
from railyard_sync.export.policy import ownership_tag

RAILYARD = "https://railyard.sh"
DOCS = {v: json.loads((FIXTURES / "sync" / f"netbox-sync-cabled{s}.json").read_text()) for v, s in [("4.5", "")]}
DOCS["4.4"] = json.loads((FIXTURES / "sync" / "netbox-sync-cabled-4.4.json").read_text())
PROJECT_ID = DOCS["4.5"]["project"]["id"]
OBJECTS = 29  # every object in the cabled fixture (tests/export/test_netbox_export.py OBJECT_COUNTS)

ARGS = ["export", "netbox", "--railyard-url", RAILYARD, "--org", ORGS[0]["slug"], "--project", "cabled"]
ARGS += ["--netbox-url", FakeNetBox.URL]

PLAN_REQUIRED = {
    "error": "the Community plan does not include the NetBox sync document; upgrade to continue, or buy a "
    "Project Pass for this estate",
    "code": "plan_required",
    "feature": "deliverables",
    "deliverable": "netbox-sync",
    "plan": "community",
    "requiredPlans": ["pro", "team"],
    "projectPass": True,
}
PLAN_LIMIT = {
    "error": "the Pro plan exports deliverables for estates of up to 100 racks; this estate has 120, so remove "
    "racks or upgrade to export it",
    "code": "plan_limit",
    "plan": "pro",
    "resource": "racks",
    "limit": 100,
    "current": 120,
    "scope": "estate",
    "requiredPlans": ["team"],
    "projectPass": False,
}


def serve_document(project, body):
    """Railyard's deliverable: the fixture written for the NetBox release asked for (4.5 by default)."""
    version = (body.get("options") or {}).get("netboxVersion") or "4.5"
    doc = copy.deepcopy(DOCS["4.4" if version == "4.4" else "4.5"])
    doc["netboxVersion"] = version
    return FakeResponse(200, doc)


@pytest.fixture
def world(monkeypatch):
    netbox, railyard = FakeNetBox(version="4.5.3"), FakeRailyard()
    railyard.deliverable = serve_document
    monkeypatch.setenv("RAILYARD_TOKEN", PAT)
    monkeypatch.setenv("NETBOX_TOKEN", netbox.token)
    monkeypatch.setattr(client_module, "_default_session", lambda: railyard.session)
    monkeypatch.setattr(dcim_http, "default_session", lambda: netbox)
    return netbox, railyard


def run(argv: list[str], netbox: FakeNetBox | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    text = out.getvalue() + err.getvalue()
    assert PAT not in text
    if netbox is not None:
        assert all(part not in text for part in netbox.token.split("."))  # nor any part of a v2 token
    return code, out.getvalue(), err.getvalue()


def deliverable_calls(railyard: FakeRailyard) -> list[dict]:
    return [c for c in railyard.session.calls if "/deliverables/" in c["url"]]


def owner_slug() -> str:
    return ownership_tag(RAILYARD, PROJECT_ID).slug


# ---- arguments and tokens -----------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [["--netbox-token", "nbt_x.y"], ["--railyard-token=" + PAT], ["--org", PAT]])
def test_a_token_on_the_command_line_is_refused(world, extra):
    netbox, railyard = world
    code, _, err = run([*ARGS, *extra])
    assert code == cli.EXIT_USAGE
    assert "never from the command line" in err
    assert netbox.calls == [] and railyard.session.calls == []


@pytest.mark.parametrize("missing", ["RAILYARD_TOKEN", "NETBOX_TOKEN"])
def test_a_missing_token_is_a_usage_error(world, monkeypatch, missing):
    netbox, railyard = world
    monkeypatch.delenv(missing)
    code, _, err = run(ARGS)
    assert code == cli.EXIT_USAGE
    assert f"set {missing}" in err
    assert netbox.calls == [] and railyard.session.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ARGS[:-2],  # no --netbox-url
        [a for a in ARGS if a not in ("--project", "cabled")],
        [*ARGS, "--netbox-version", "3.7"],
        [*ARGS, "--netbox-version", "latest"],
        [*ARGS, "--change-request", " "],
        [*ARGS[:-1], "ftp://netbox.example.com"],
        ["export"],
        ["export", "nautobot", *ARGS[2:]],
    ],
)
def test_usage_errors_exit_2(world, argv):
    netbox, railyard = world
    code, _, _ = run(argv)
    assert code == cli.EXIT_USAGE
    assert netbox.calls == [] and railyard.session.calls == []


def test_help_describes_the_dry_run(capsys):
    assert cli.main(["export", "netbox", "--help"]) == 0
    text = capsys.readouterr().out
    assert "--apply" in text and "dry run" in text and "--netbox-version" in text


# ---- dry run, apply, re-apply ---------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_writes_nothing(world):
    netbox, railyard = world
    code, out, _ = run(ARGS, netbox)
    assert code == cli.EXIT_OK, out
    assert netbox.writes == []
    (call,) = deliverable_calls(railyard)
    assert call["url"] == f"{RAILYARD}/api/projects/cabled/deliverables/netbox-sync"
    assert call["json"] == {"options": {"netboxVersion": "4.5"}}  # read from NetBox's /api/status/
    assert call["headers"]["X-Org-Id"] == ORGS[0]["id"]
    assert f"Dry run, planned: {OBJECTS} to create, 0 to update, 0 to delete, 0 unchanged." in out
    assert "  device: 4 to create" in out and "  cable: 3 to create" in out
    assert "create: device [name=SRV-1" in out
    assert "doesn't exist yet: this sync owns nothing in NetBox" in out  # a first run's warning
    assert "nothing was written to NetBox" in out


def test_apply_creates_everything_and_a_second_apply_is_a_no_op(world):
    netbox, _ = world
    code, out, _ = run([*ARGS, "--apply"], netbox)
    assert code == cli.EXIT_OK, out
    assert f"Applied: {OBJECTS} created, 0 updated, 0 deleted, 0 unchanged." in out
    assert "  device: 4 created" in out and "  rack: 1 created" in out
    for endpoint in ("dcim/racks", "dcim/devices", "dcim/cables"):
        assert netbox.objects[endpoint] and len(netbox.tagged(endpoint, owner_slug())) == len(netbox.objects[endpoint])
    assert "NetBox is in step with the Railyard estate." in out

    writes = len(netbox.writes)
    code, out, _ = run([*ARGS, "--apply"], netbox)
    assert code == cli.EXIT_OK, out
    assert len(netbox.writes) == writes
    assert f"Applied: 0 created, 0 updated, 0 deleted, {OBJECTS} unchanged." in out


def test_change_request_and_netbox_version_are_sent_and_a_mismatch_warned(world):
    netbox, railyard = world
    code, out, _ = run([*ARGS, "--change-request", "cr_9", "--netbox-version", "4.4"], netbox)
    assert code == cli.EXIT_OK, out
    (call,) = deliverable_calls(railyard)
    assert call["json"] == {"changeRequestId": "cr_9", "options": {"netboxVersion": "4.4"}}
    assert "written for NetBox 4.4" in out and "runs NetBox 4.5.3" in out


def test_json_output_is_the_sync_result(world, capsys):
    netbox, _ = world
    structlog.reset_defaults()  # diffsync's structlog prints to stdout unless the CLI routes it elsewhere
    code, out, _ = run([*ARGS, "--apply", "--json"], netbox)
    assert code == cli.EXIT_OK
    result = json.loads(out)
    assert (result["ok"], result["dry_run"], result["created"]) == (True, False, OBJECTS)
    assert result["project_id"] == PROJECT_ID and result["tag_slug"] == owner_slug()
    assert result["applied"]["create"]["device"] == 4
    assert capsys.readouterr().out == ""  # no library logging reached the real stdout


def test_progress_and_verbose_requests_go_to_stderr_and_json_stays_clean(world, tmp_path):
    netbox, _ = world
    log_file = tmp_path / "export.log"
    code, out, err = run([*ARGS, "--apply", "--json", "-v", "--log-file", str(log_file)], netbox)
    assert code == cli.EXIT_OK
    assert json.loads(out)["created"] == OBJECTS
    assert f"Reading NetBox {FakeNetBox.URL}…" in err
    assert "Fetching the NetBox sync document for cabled from Railyard…" in err
    assert "Writing to NetBox…" in err and f"{OBJECTS} created" in err
    assert "  NetBox GET /api/status/ -> 200 in " in err and "  NetBox POST /api/dcim/devices/ -> 201 in " in err
    assert "Railyard POST /api/projects/cabled/deliverables/netbox-sync -> 200 in " in err
    text = log_file.read_text()
    assert "NetBox POST /api/dcim/devices/" in text
    for part in [PAT, *netbox.token.split(".")]:
        assert part not in text


def test_a_netbox_failure_says_how_far_the_export_got(world):
    netbox, railyard = world
    railyard.deliverable = lambda project, body: FakeResponse(500, {"error": "internal server error"})
    code, _, err = run(ARGS, netbox)
    assert code == cli.EXIT_ERROR
    assert "Railyard failed while generating the netbox-sync deliverable (HTTP 500" in err
    assert "Before the failure, railyard-sync had:" in err and f"- read NetBox {FakeNetBox.URL}" in err
    assert "Quote request id rys-" in err


def test_netbox_write_errors_exit_1(world):
    netbox, _ = world
    netbox.forbid.add(("POST", "dcim/cables"))
    code, out, _ = run([*ARGS, "--apply"], netbox)
    assert code == cli.EXIT_ERROR
    assert "Errors (3)" in out and "Finished with errors or conflicts" in out


def test_conflicts_exit_1(world):
    netbox, _ = world
    site = netbox.add("dcim/sites", name="DC1", slug="dc1", status="active")
    mfr = netbox.add("dcim/manufacturers", name="OperatorCo", slug="operatorco")
    dtype = netbox.add("dcim/device-types", manufacturer=mfr, model="OP1", slug="op1", u_height=1)
    role = netbox.add("dcim/device-roles", name="operator", slug="operator", color="aaaaaa")
    netbox.add("dcim/devices", name="SRV-1", site=site, device_type=dtype, role=role, status="active")
    code, out, _ = run(ARGS, netbox)
    assert code == cli.EXIT_ERROR
    assert "Conflicts (" in out and "SRV-1" in out
    assert netbox.writes == []


# ---- refusals ---------------------------------------------------------------------------------------------


def test_plan_required_exits_3_naming_the_plans_and_the_project_pass(world):
    netbox, railyard = world
    railyard.deliverable = lambda project, body: FakeResponse(402, PLAN_REQUIRED)
    code, _, err = run([*ARGS, "--apply"], netbox)
    assert code == cli.EXIT_PLAN
    assert (
        "Exporting to NetBox needs a plan with deliverables: the Community plan does not include them. "
        "Upgrade to Pro or Team, or buy a Project Pass for this estate. Nothing was written to NetBox."
    ) in err
    assert netbox.writes == []


def test_plan_limit_exits_3_with_the_estate_size(world):
    netbox, railyard = world
    railyard.deliverable = lambda project, body: FakeResponse(402, PLAN_LIMIT)
    code, _, err = run([*ARGS, "--apply"], netbox)
    assert code == cli.EXIT_PLAN
    assert (
        "This estate has 120 racks; the Pro plan exports deliverables for estates of up to 100 racks. "
        "Upgrade to Team, or remove racks. Nothing was written to NetBox."
    ) in err
    assert netbox.writes == []


def test_an_unknown_project_is_an_error(world):
    netbox, railyard = world
    railyard.deliverable = lambda project, body: FakeResponse(404, {"error": "project not found"})
    code, _, err = run(ARGS, netbox)
    assert code == cli.EXIT_ERROR
    assert "Not found" in err and "project not found" in err


def test_a_netbox_token_netbox_refuses_stops_before_railyard(world, monkeypatch):
    netbox, railyard = world
    monkeypatch.setenv("NETBOX_TOKEN", "nbt_wrong.token")
    code, _, err = run(ARGS, netbox)
    assert code == cli.EXIT_ERROR
    assert "did not accept the API token" in err
    assert deliverable_calls(railyard) == []


def test_a_document_railyard_cannot_have_sent_is_an_error(world):
    netbox, railyard = world
    railyard.deliverable = lambda project, body: FakeResponse(200, {"format": "something-else"})
    code, _, err = run(ARGS, netbox)
    assert code == cli.EXIT_ERROR
    assert netbox.writes == []

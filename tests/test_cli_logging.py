"""The CLI's progress lines, logs and failure reports: progress on stderr (results stay on stdout), -v request
logs with request ids, -q, --log-file and --debug; no token or document body in any of them; the document of a
refused save kept with mode 0600; how far a failed run got; and the /api/validate preflight."""

from __future__ import annotations

import json
import os
import stat

import pytest
from conftest import FakeResponse
from test_cli import BASE, NB_PAT, PAT, env, existing_estate, run, stubs  # noqa: F401 - env and stubs are fixtures

from railyard_sync import cli, client as client_module

RID = "req_4765ead6c0ffee1234"
CREATED = "ry-0123456789abcdef0123"


@pytest.fixture
def world(stubs):  # noqa: F811 - test_cli's fixture
    """test_cli's stubbed loader and builder and its fake Railyard, run in a temporary directory."""
    return stubs


@pytest.fixture
def created_id(monkeypatch):
    monkeypatch.setattr(cli, "new_project_id", lambda: CREATED)
    return CREATED


def no_secrets(*texts: str) -> None:
    for text in texts:
        assert PAT not in text and NB_PAT not in text
        assert '"placements"' not in text and '"schemaVersion"' not in text  # no document body


def problem(severity, code, message, **where):
    return {"severity": severity, "code": code, "message": message, **where}


# ---- progress and logs ------------------------------------------------------------------------------------


def test_progress_goes_to_stderr_and_results_to_stdout(world, created_id):
    code, out, err = run([*BASE, "--name", "LDN1 baseline"])
    assert code == 0
    for line in [
        "Read 1 rack, 0 devices, 0 ports, 0 cables in ",
        "Building the Railyard project…",
        "Built 1 rack, 1 device, 0 cables, 0 power links in ",
        "Checking the document with Railyard…",
        "Railyard's check found 0 errors and 0 warnings in ",
        "Saving to Railyard (",
        "Saved revision 8 in ",
    ]:
        assert line in err, line
    assert "Saving to Railyard" not in out and "Created 'LDN1 baseline'" in out
    assert "Railyard PUT" not in err  # request lines need -v
    no_secrets(out, err)


def test_a_refresh_reports_the_fetch_and_the_merge(world):
    world.railyard.project = existing_estate()
    code, _, err = run([*BASE, "--project", "ldn1-baseline"])
    assert code == 0
    assert "Fetching estate ldn1-baseline from Railyard…" in err
    assert "Fetched 'LDN1 baseline' (ry-existing0000000000) at revision 7: 1 rack, 1 device" in err
    assert "Merging into 'LDN1 baseline'…" in err and "0 added, 1 updated, 0 stale, 0 removed" in err


def test_verbose_logs_every_request_with_its_request_id(world):
    world.railyard.put_response = FakeResponse(200, {"id": "x"}, headers={"ETag": '"8"', "X-Request-ID": RID})
    code, _, err = run([*BASE, "--name", "x", "-v"])
    assert code == 0
    assert "  Railyard GET /api/orgs -> 200 in " in err
    assert "Railyard POST /api/validate -> 200 in " in err and "request id rys-" in err
    put = next(line for line in err.splitlines() if "Railyard PUT /api/projects/" in line)
    assert f"request id {RID}" in put and "sent " in put and 'ETag "8"' in put
    no_secrets(err)


def test_quiet_prints_nothing_but_errors(world):
    code, out, err = run([*BASE, "--name", "x", "-q", "--insecure"])
    assert code == 0 and err == "" and "Created" in out
    world.railyard.put_response = FakeResponse(500, {"error": "internal server error"})
    code, _, err = run([*BASE, "--name", "x", "-q"])
    assert code == 1 and err.startswith("railyard-sync: error: Railyard failed while saving")


def test_log_file_is_debug_level_private_and_free_of_secrets(world, tmp_path):
    log_file = tmp_path / "run.log"
    code, _, err = run([*BASE, "--name", "x", "--log-file", str(log_file)])
    assert code == 0
    text = log_file.read_text()
    assert "DEBUG   railyard_sync.client: Railyard PUT /api/projects/" in text
    assert "INFO    railyard_sync.cli: Saved revision 8" in text
    assert stat.S_IMODE(os.stat(log_file).st_mode) == 0o600
    assert "Railyard PUT" not in err  # the file is debug-level; stderr stays at progress
    no_secrets(text, err)


def test_a_token_a_server_echoes_is_redacted_everywhere(world, tmp_path):
    log_file = tmp_path / "run.log"
    echo = f"bad header Authorization: Bearer {PAT} (NetBox token {NB_PAT})"
    world.railyard.put_response = FakeResponse(502, text=echo)
    code, out, err = run([*BASE, "--name", "x", "--debug", "--log-file", str(log_file)])
    assert code == 1
    no_secrets(out, err, log_file.read_text())


def test_debug_adds_the_traceback_and_the_log_file_always_has_it(world, tmp_path):
    world.railyard.put_response = FakeResponse(500, {"error": "internal server error"})
    log_file = tmp_path / "run.log"
    code, _, err = run([*BASE, "--name", "x", "--log-file", str(log_file)])
    assert code == 1 and "Traceback" not in err
    assert "Traceback (most recent call last)" in log_file.read_text()
    code, _, err = run([*BASE, "--name", "x", "--debug"])
    assert code == 1 and "Traceback (most recent call last)" in err
    assert "Railyard PUT /api/projects/" in err  # --debug is verbose too


def test_an_unexpected_failure_is_reported_as_a_bug(world, monkeypatch):
    def broken(*a, **k):
        raise KeyError("racks")

    monkeypatch.setattr(cli, "build_project", broken)
    code, _, err = run([*BASE, "--name", "x"])
    assert code == 1
    assert "unexpected KeyError" in err and "railyard-sync bug" in err and "--debug" in err


# ---- failures: messages, evidence and how far it got ------------------------------------------------------


def test_a_500_on_save_keeps_the_document_and_says_how_far_it_got(world):
    world.railyard.project = existing_estate()
    world.railyard.put_response = FakeResponse(500, {"error": "internal server error"}, headers={"X-Request-ID": RID})
    code, _, err = run([*BASE, "--project", "ldn1-baseline"])
    assert code == cli.EXIT_ERROR
    assert (
        "railyard-sync: error: Railyard failed while saving the estate (HTTP 500 for PUT "
        "/api/projects/ry-existing0000000000): internal server error"
    ) in err
    assert "This is a Railyard bug, not a fault in your data" in err
    assert f"Quote request id {RID} when reporting this." in err
    (kept,) = [f for f in os.listdir(".") if f.startswith("railyard-sync-failed-ry-existing0000000000-")]
    assert kept.endswith("Z.json") and f"./{kept}" in err
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o600
    assert json.loads(open(kept).read()) == world.railyard.calls("PUT")[0]["json"]
    report = err[err.index("Before the failure") :]
    assert "- read NetBox https://netbox.example.com, 1 site: 1 rack" in report
    assert "- fetched 'LDN1 baseline' (ry-existing0000000000) at revision 7" in report
    assert "- built the Railyard project: 1 rack, 1 device" in report
    assert "- merged it into 'LDN1 baseline': 0 added, 1 updated" in report
    assert "- checked it with Railyard: 0 errors, 0 warnings" in report
    assert report.rstrip().endswith("The save did not complete (see above).")


def test_failed_dir_chooses_where_the_document_goes(world, tmp_path):
    target = tmp_path / "evidence"
    target.mkdir()
    world.railyard.put_response = FakeResponse(503, {"error": "busy"})
    code, _, err = run([*BASE, "--name", "x", "--failed-dir", str(target)])
    assert code == 1
    (kept,) = os.listdir(target)
    assert str(target / kept) in err and "Railyard is busy (HTTP 503)" in err
    assert "Nothing was saved to Railyard" not in err  # a 503 refusal is a 5xx: uncertain


def test_a_400_on_save_lists_the_problems_and_keeps_the_document(world, created_id):
    body = {
        "error": 'rack name "A01" is already used in this space',
        "code": "rack.name-duplicate",
        "problems": [problem("error", "rack.name-duplicate", "already used", rackId="nb-rack-100")],
    }
    world.railyard.put_response = FakeResponse(400, body, headers={"X-Request-ID": RID})
    code, _, err = run([*BASE, "--name", "LDN1 baseline"])
    assert code == 1
    assert f"Railyard refused the document (HTTP 400, rack.name-duplicate) for PUT /api/projects/{CREATED}" in err
    assert "- rack 'A01' (nb-rack-100): already used [rack.name-duplicate]" in err
    assert f"railyard-sync-failed-{CREATED}-" in err and RID in err
    assert err.rstrip().endswith("Nothing was saved to Railyard.")


def test_a_412_and_a_name_taken_keep_their_short_explanations_and_the_evidence(world):
    world.railyard.project = existing_estate()
    world.railyard.put_response = FakeResponse(412, {"error": "revision conflict"}, headers={"X-Request-ID": RID})
    code, _, err = run([*BASE, "--project", "p"])
    assert code == 1 and "changed in Railyard while the import ran" in err and "merges onto the latest" in err
    assert "railyard-sync-failed-" in err and RID in err


def test_a_busy_railyard_is_retried(world, monkeypatch):
    sleeps = []
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    world.railyard.project = existing_estate()
    answers = [FakeResponse(503, {"error": "busy"}, headers={"Retry-After": "3"})]
    original = world.railyard.handle

    def handle(method, url, headers, params):
        if method == "PUT" and answers:
            return answers.pop(0)
        return original(method, url, headers, params)

    world.railyard.session.handler = handle
    code, out, err = run([*BASE, "--project", "p"])
    assert code == 0, err
    assert sleeps == [3.0]
    assert "warning: Railyard is busy (HTTP 503 for PUT /api/projects/ry-existing0000000000); retrying in 3 s" in err
    assert "Refreshed" in out


def test_save_document_keeps_what_was_sent_on_success(world, tmp_path):
    copy = tmp_path / "sent.json"
    code, _, err = run([*BASE, "--name", "x", "--save-document", str(copy)])
    assert code == 0
    assert json.loads(copy.read_text()) == world.railyard.calls("PUT")[0]["json"]
    assert stat.S_IMODE(os.stat(copy).st_mode) == 0o600
    assert f"Kept a copy of the document to send in {copy}" in err


# ---- preflight ----------------------------------------------------------------------------------------------


def test_preflight_errors_stop_the_save_and_name_racks_and_devices(world):
    world.railyard.validate = FakeResponse(
        200,
        {"problems": [problem("error", "placement.overlap", "overlaps", rackId="nb-rack-100", placementId="nb-dev-1")]},
    )
    code, _, err = run([*BASE, "--name", "x"])
    assert code == 1
    assert world.railyard.calls("PUT") == []
    assert "Railyard's check found 1 problem(s) in the document, so it was not saved:" in err
    assert "- device 'sw1' (nb-dev-1), in rack 'A01' (nb-rack-100): overlaps [placement.overlap]" in err
    assert "--no-validate" in err and "Nothing was saved to Railyard." in err


def test_preflight_warnings_are_reported_and_the_save_goes_ahead(world):
    world.railyard.validate = FakeResponse(
        200, {"problems": [problem("warning", "cable.port-missing", "no port", rackId="nb-rack-100")]}
    )
    code, _, err = run([*BASE, "--name", "x"])
    assert code == 0 and len(world.railyard.calls("PUT")) == 1
    assert "warning: Railyard's check found 1 warning; they do not stop the import:" in err
    assert "- rack 'A01' (nb-rack-100): no port [cable.port-missing]" in err


def test_no_validate_skips_the_check(world):
    world.railyard.validate = FakeResponse(200, {"problems": [problem("error", "x", "bad")]})
    code, _, err = run([*BASE, "--name", "x", "--no-validate"])
    assert code == 0 and world.railyard.validations() == []
    assert "--no-validate" in err


def test_rack_name_rules_do_not_stop_a_new_estate(world):
    world.railyard.validate = FakeResponse(
        200, {"problems": [problem("error", "rack.name-duplicate", "dup", rackId="nb-rack-100")]}
    )
    code, _, err = run([*BASE, "--name", "x"])
    assert code == 0 and len(world.railyard.calls("PUT")) == 1
    assert "a new estate is not held to these rules" in err


def test_a_refresh_is_not_stopped_by_errors_the_estate_already_had(world):
    world.railyard.project = existing_estate()
    legacy = problem("error", "cable.invalid", "a Railyard cable on a missing port")
    new = problem("error", "placement.overlap", "overlaps", rackId="nb-rack-100", placementId="nb-dev-1")

    def validate(document):
        label = document["racks"][0]["placements"][0]["label"]
        return FakeResponse(200, {"problems": [legacy] if label == "old-name" else [legacy, new]})

    world.railyard.validate = validate
    code, _, err = run([*BASE, "--project", "p"])
    assert code == 1 and world.railyard.calls("PUT") == []
    assert len(world.railyard.validations()) == 2  # the document, then the estate as it was
    assert "overlaps [placement.overlap]" in err.split("not saved:")[1]
    assert "the estate already had them" in err and "a Railyard cable on a missing port" in err

    world.railyard.validate = lambda document: FakeResponse(200, {"problems": [legacy]})
    code, _, err = run([*BASE, "--project", "p"])
    assert code == 0 and len(world.railyard.calls("PUT")) == 1


def test_a_document_validate_cannot_load_stops_a_create_but_not_a_refresh(world):
    refusal = FakeResponse(400, {"error": "rack r1: name is longer than 100 characters"})
    world.railyard.validate = refusal
    code, _, err = run([*BASE, "--name", "x"])
    assert code == 1 and "Railyard cannot load the document: rack r1: name is longer" in err
    world.railyard.project = existing_estate()
    code, _, err = run([*BASE, "--project", "p"])
    assert code == 0 and "could not load the document on its own" in err


@pytest.mark.parametrize("status", [404, 413, 500, 503])
def test_a_railyard_that_cannot_check_is_a_warning(world, monkeypatch, status):
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    world.railyard.validate = FakeResponse(status, {"error": "nope"})
    code, _, err = run([*BASE, "--name", "x"])
    assert code == 0 and len(world.railyard.calls("PUT")) == 1
    assert "warning:" in err and "saving without" in err


def test_a_dry_run_reports_what_the_check_would_stop_for(world):
    world.railyard.validate = FakeResponse(200, {"problems": [problem("error", "placement.overlap", "overlaps")]})
    code, out, _ = run([*BASE, "--name", "x", "--dry-run"])
    assert code == 1 and world.railyard.calls("PUT") == []
    assert "Railyard's check found 1 problem(s) a real import would stop for:" in out
    assert "Dry run: would create" in out

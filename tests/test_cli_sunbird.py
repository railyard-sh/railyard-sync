"""``railyard-sync export sunbird``: arguments, secrets from the environment or a file only, the flow (the Sunbird sync
document from a fake Railyard, matched against the in-memory dcTrack in ``tests/export/fake_dctrack_rest.py``), the
report, ``--json``, ``--import-file``, exit codes and plan refusals, and that no secret reaches any output."""

from __future__ import annotations

import base64
import copy
import io
import json
import os

import pytest
from conftest import FakeResponse
from export.fake_dctrack_rest import FakeDcTrack
from export.sunbird_docs import device, document
from test_cli import ORGS, PAT, FakeRailyard
from test_cli_export import PLAN_REQUIRED

from railyard_sync import cli, client as client_module, dcim_http

RAILYARD = "https://railyard.sh"
ARGS = ["export", "sunbird", "--railyard-url", RAILYARD, "--org", ORGS[0]["slug"], "--project", "lon1"]
ARGS += ["--dctrack-url", FakeDcTrack.URL, "--dctrack-user", FakeDcTrack.USER, "--location", "LON1"]

DOC = document(
    [
        device("SRV-01", 1, serial="ABC123"),
        device("SRV-02", 2, serial="SAME"),
        device("SRV-03", 3, serial=""),
    ],
    skipped=[{"cabinet": "R01", "name": "PDU-A", "reason": "zero-u", "railyard": {"kind": "device", "id": "pl-9"}}],
    warnings=["1 device has no device type"],
)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    dctrack, railyard = FakeDcTrack(), FakeRailyard()
    dctrack.add_location("LON1")
    cab = dctrack.add_cabinet("LON1", "R01")
    dctrack.items_by_u = {
        u: dctrack.add_item(cab, f"lon1-srv-{u}", u, serial=s) for u, s in ((1, ""), (2, "SAME"), (3, "X"))
    }
    railyard.sunbird = copy.deepcopy(DOC)
    railyard.deliverable = lambda project, body: FakeResponse(200, copy.deepcopy(railyard.sunbird))
    monkeypatch.setenv("RAILYARD_TOKEN", PAT)
    monkeypatch.setenv("DCTRACK_PASSWORD", dctrack.PASSWORD)
    monkeypatch.delenv("DCTRACK_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(client_module, "_default_session", lambda: railyard.session)
    monkeypatch.setattr(dcim_http, "default_session", lambda: dctrack)
    return dctrack, railyard


SECRETS = [
    PAT,
    FakeDcTrack.PASSWORD,
    base64.b64encode(f"{FakeDcTrack.USER}:{FakeDcTrack.PASSWORD}".encode()).decode(),
    FakeDcTrack.CLIENT_SECRET,
    FakeDcTrack.ACCESS_TOKEN,
]


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err)
    for secret in SECRETS:
        assert secret not in out.getvalue() + err.getvalue()
    return code, out.getvalue(), err.getvalue()


def deliverable_calls(railyard: FakeRailyard) -> list[dict]:
    return [c for c in railyard.session.calls if "/deliverables/" in c["url"]]


# ---- arguments and secrets --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [["--dctrack-password", "hunter2"], ["--dctrack-password=hunter2"], ["--dctrack-client-secret", "s"]],
)
def test_a_password_on_the_command_line_is_refused(world, extra):
    dctrack, railyard = world
    code, _, err = run([*ARGS, *extra])
    assert code == cli.EXIT_USAGE
    assert "never from the command line" in err and "hunter2" not in err
    assert dctrack.calls == [] and railyard.session.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        [a for a in ARGS if a not in ("--location", "LON1")],  # no location
        [*ARGS, "--location-map", "Hall A"],
        [*ARGS, "--dctrack-client-id", "c1"],  # a user and a client
        [a for a in ARGS if a not in ("--dctrack-user", FakeDcTrack.USER)],  # neither
        [*ARGS, "--change-request", "cr1", "--version", "v1"],
        [*ARGS, "--container", "c1", "--rack", "r1"],
        [*ARGS, "--ca-bundle", "ca.pem", "--no-verify-tls"],
        [*ARGS, "--match-u", "top"],
        [*ARGS, "--rack", " "],
        [*ARGS[:9], "http://dctrack.example.com", *ARGS[10:]],  # http without --insecure-http
        [*ARGS, "--ca-bundle", "missing.pem"],
    ],
)
def test_usage_errors_exit_2(world, argv):
    dctrack, railyard = world
    code, _, _ = run(argv)
    assert code == cli.EXIT_USAGE
    assert dctrack.calls == [] and railyard.session.calls == []


def test_a_missing_password_is_a_usage_error(world, monkeypatch):
    monkeypatch.delenv("DCTRACK_PASSWORD")
    code, _, err = run(ARGS)
    assert code == cli.EXIT_USAGE and "set DCTRACK_PASSWORD" in err


def test_help_names_the_import_template_caveat(capsys):
    assert cli.main(["export", "sunbird", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "--apply" in text and "dry run" in text
    assert "column headings must match your dcTrack import template" in text


# ---- the flow ----------------------------------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_writes_nothing(world):
    dctrack, railyard = world
    code, out, err = run(ARGS)
    assert code == cli.EXIT_OK, out + err
    assert dctrack.writes == []
    (call,) = deliverable_calls(railyard)
    assert call["url"] == f"{RAILYARD}/api/projects/lon1/deliverables/sunbird-sync"
    assert call["json"] == {}
    assert "Railyard estate 'LON1 build' (p1) → dcTrack at https://dctrack.example.com" in out
    assert "Dry run, planned: 1 update, 1 unchanged, 1 no-serial." in out
    assert "Updates (1): to write:\n  - R01 U1 SRV-01 → dcTrack LON1-SRV-1: (blank) → ABC123" in out
    assert "Skipped by Railyard (1):\n  - R01 PDU-A: zero-u" in out
    assert "Warnings (1):\n  - 1 device has no device type" in out
    assert "Dry run: nothing was written to dcTrack." in out
    assert "Fetching the Sunbird sync document for lon1 from Railyard…" in err


def test_apply_writes_the_serials_and_a_second_run_is_a_no_op(world):
    dctrack, _ = world
    code, out, _ = run([*ARGS, "--apply"])
    assert code == cli.EXIT_OK, out
    assert dctrack.serial(dctrack.items_by_u[1]) == "ABC123"
    assert dctrack.serial(dctrack.items_by_u[3]) == "X"  # SRV-03 has no serial in Railyard: dcTrack's is kept
    assert "Wrote 1 serial to dcTrack." in out

    code, out, _ = run([*ARGS, "--apply"])
    assert code == cli.EXIT_OK and "Devices: 2 unchanged, 1 no-serial." in out
    assert len(dctrack.writes) == 1


def test_scope_change_request_and_version_are_sent(world):
    _, railyard = world
    run([*ARGS, "--container", "hall-a", "--change-request", "cr_9"])
    run([*ARGS, "--rack", "r1", "--rack", "r2", "--version", "v_7"])
    first, second = deliverable_calls(railyard)
    assert first["json"] == {"changeRequestId": "cr_9", "options": {"scope": {"kind": "container", "id": "hall-a"}}}
    assert second["json"] == {"versionId": "v_7", "options": {"rackIds": ["r1", "r2"]}}


def test_conflicts_exit_1_and_apply_still_writes_the_clean_updates(world):
    dctrack, railyard = world
    railyard.sunbird["devices"][2]["serial"] = "Y"  # dcTrack has X at U3
    code, out, _ = run(ARGS)
    assert code == cli.EXIT_ERROR
    assert "Conflicts (1):" in out and "R01 U3 SRV-03 → dcTrack LON1-SRV-3: X → Y [conflict, serial-differs" in out
    assert dctrack.writes == []

    code, out, _ = run([*ARGS, "--apply"])
    assert code == cli.EXIT_ERROR
    assert dctrack.serial(dctrack.items_by_u[1]) == "ABC123" and dctrack.serial(dctrack.items_by_u[3]) == "X"
    assert "Finished with errors, conflicts or ambiguities" in out

    code, out, _ = run([*ARGS, "--apply", "--overwrite"])
    assert code == cli.EXIT_OK and dctrack.serial(dctrack.items_by_u[3]) == "Y"


def test_refused_rows_exit_1_with_dctracks_words(world):
    dctrack, _ = world
    dctrack.refuse[dctrack.items_by_u[1]] = "Serial Number ABC123 is a duplicate"
    code, out, _ = run([*ARGS, "--apply"])
    assert code == cli.EXIT_ERROR
    assert "Errors (1): updates dcTrack refused" in out
    assert "dcTrack refused it: Serial Number ABC123 is a duplicate" in out


def test_json_output(world):
    code, out, _ = run([*ARGS, "--json"])
    assert code == cli.EXIT_OK
    result = json.loads(out)
    assert (result["dry_run"], result["ok"], result["project_id"]) == (True, True, "p1")
    assert result["counts"]["update"] == 1 and result["counts"]["no-serial"] == 1
    (update,) = [d for d in result["devices"] if d["outcome"] == "update"]
    assert (update["cabinet"], update["u"], update["item_name"], update["serial"]) == ("R01", 1, "LON1-SRV-1", "ABC123")


def test_the_import_file_in_a_dry_run(world, tmp_path):
    dctrack, _ = world
    path = tmp_path / "serials.csv"
    code, out, _ = run([*ARGS, "--import-file", str(path)])
    assert code == cli.EXIT_OK
    assert path.read_text(encoding="utf-8").splitlines() == [
        "# Operation,Object,Location,Name,Serial Number",
        "EDIT,Device,LON1,LON1-SRV-1,ABC123",
    ]
    assert f"Wrote 1 row to the dcTrack import file {path}" in out and dctrack.writes == []


def test_a_password_file_and_oauth2(world, monkeypatch, tmp_path):
    dctrack, _ = world
    monkeypatch.delenv("DCTRACK_PASSWORD")
    secret = tmp_path / "dctrack.pw"
    secret.write_text(dctrack.PASSWORD + "\n")
    os.chmod(secret, 0o644)
    code, _, err = run([*ARGS, "--dctrack-password-file", str(secret)])
    assert code == cli.EXIT_OK and "can be read by other users" in err

    monkeypatch.setenv("DCTRACK_CLIENT_SECRET", dctrack.CLIENT_SECRET)
    argv = [a for a in ARGS if a not in ("--dctrack-user", dctrack.USER)] + ["--dctrack-client-id", dctrack.CLIENT_ID]
    code, out, _ = run([*argv, "--apply", "-v"])
    assert code == cli.EXIT_OK, out
    assert dctrack.serial(dctrack.items_by_u[1]) == "ABC123"


def test_no_secret_in_any_output_or_log(world, tmp_path):
    dctrack, _ = world
    log_file = tmp_path / "sunbird.log"
    code, out, err = run([*ARGS, "--apply", "--debug", "--log-file", str(log_file)])
    assert code == cli.EXIT_OK
    assert "  dcTrack POST /api/v2/dcimoperations/items/bulk -> 200 in " in err
    assert "Railyard POST /api/projects/lon1/deliverables/sunbird-sync -> 200 in " in err
    text = log_file.read_text()
    assert "dcTrack GET /api/v1/locations" in text
    for secret in SECRETS:
        assert secret not in text
    assert "ABC123" not in err and "ABC123" not in text  # serials (bodies) are never logged


def test_a_refused_password_exits_1_saying_what_to_check(world, monkeypatch):
    dctrack, _ = world
    monkeypatch.setenv("DCTRACK_PASSWORD", "wrong-password")
    code, _, err = run(ARGS)
    assert code == cli.EXIT_ERROR
    assert "dcTrack did not accept the credentials (HTTP 401)" in err and "--dctrack-user" in err
    assert "wrong-password" not in err and dctrack.writes == []


# ---- refusals -------------------------------------------------------------------------------------------------------


def test_a_plan_refusal_exits_3(world):
    dctrack, railyard = world
    railyard.deliverable = lambda project, body: FakeResponse(402, PLAN_REQUIRED)
    code, _, err = run([*ARGS, "--apply"])
    assert code == cli.EXIT_PLAN
    assert "Exporting to dcTrack needs a plan with deliverables" in err and "Nothing was written to dcTrack." in err
    assert dctrack.calls == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": "railyard-netbox-sync"}, "Not a Railyard Sunbird sync document"),
        ({"version": 2}, "version 2 is not supported by this railyard-sync"),
    ],
)
def test_a_document_of_the_wrong_format_or_version_is_an_error(world, change, message):
    dctrack, railyard = world
    railyard.sunbird.update(change)
    code, _, err = run([*ARGS, "--apply"])
    assert code == cli.EXIT_ERROR
    assert message in err and dctrack.writes == []

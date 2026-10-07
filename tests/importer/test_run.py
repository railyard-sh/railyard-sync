"""``ImportRun``: the import flow the CLI and the NetBox plugin share, against a fake Railyard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import FakeResponse
from test_cli import PAT, FakeRailyard as _FakeRailyard, Report, existing_estate, imported_doc, snapshot

from railyard_sync.client import RailyardClient
from railyard_sync.errors import RailyardPlanLimitError
from railyard_sync.importer.run import ImportRefused, ImportRun, new_project_id


def build(snap, *, project_id, name, catalogue, prefix):
    return SimpleNamespace(project=imported_doc(project_id, name), report=Report())


@pytest.fixture
def railyard():
    return _FakeRailyard()


def make(railyard, **kw) -> ImportRun:
    client = RailyardClient("https://railyard.sh", PAT, org="acme", session=railyard.session)
    steps: list[str] = []
    run = ImportRun(client, snapshot(), catalogue=None, build=build, step=steps.append, **kw)
    run.steps = steps
    return run


def test_either_a_name_or_an_estate():
    for kw in ({}, {"project": "x", "name": "y"}):
        with pytest.raises(ValueError):
            ImportRun(None, snapshot(), **kw)


def test_new_estate_ids():
    assert new_project_id().startswith("ry-") and len(new_project_id()) == 23


def test_create(railyard):
    run = make(railyard, name="LDN1 baseline", project_id="ry-new")
    outcome = run.run(dry_run=False)
    assert (outcome.status, outcome.refreshed, outcome.project_id, outcome.revision) == ("saved", False, "ry-new", 8)
    (put,) = railyard.calls("PUT")
    assert "If-Match" not in put["headers"]
    assert run.steps[0].startswith("built the Railyard project")


def test_dry_run_checks_and_saves_nothing(railyard):
    railyard.project = existing_estate()
    run = make(railyard, project="ldn1-baseline")
    outcome = run.run(dry_run=True)
    assert outcome.status == "dry-run" and outcome.refreshed
    assert run.diff is not None and run.diff["placements"].updated == ["nb-dev-1"]
    assert railyard.calls("PUT") == [] and len(railyard.validations()) == 1


def test_refresh_saves_with_if_match_and_an_unchanged_one_is_not_saved(railyard):
    railyard.project = existing_estate()
    assert make(railyard, project="ldn1-baseline").run(dry_run=False).status == "saved"
    assert railyard.calls("PUT")[0]["headers"]["If-Match"] == '"7"'
    assert make(railyard, project="ldn1-baseline").run(dry_run=False).status == "up-to-date"
    assert len(railyard.calls("PUT")) == 1


def test_conflicts_stop_the_save(railyard):
    estate = existing_estate()
    estate["racks"][0]["placements"].append(
        {"id": "pl_mine", "startU": 10, "heightU": 2, "face": "front", "label": "planned"}
    )
    railyard.project = estate
    with pytest.raises(ImportRefused, match="conflict"):
        make(railyard, project="ldn1-baseline").run(dry_run=False)
    assert railyard.calls("PUT") == []


def test_check_errors_stop_the_save_and_a_dry_run_returns_them(railyard):
    problem = {"severity": "error", "code": "placement.overlap", "message": "overlaps", "rackId": "nb-rack-100"}
    railyard.validate = FakeResponse(200, {"problems": [problem]})
    outcome = make(railyard, name="x", project_id="ry-new").run(dry_run=True)
    assert outcome.problems and "overlaps" in outcome.problems[0]
    with pytest.raises(ImportRefused, match="so it was not saved"):
        make(railyard, name="x", project_id="ry-new").run(dry_run=False)


def test_another_source_is_refused(railyard):
    estate = existing_estate()
    estate["meta"]["railyardSync"]["source"]["url"] = "https://other-netbox.example.com"
    railyard.project = estate
    with pytest.raises(ImportRefused, match="ids from two NetBox instances would collide"):
        make(railyard, project="ldn1-baseline").fetch()


def test_a_change_meanwhile_is_refused_with_what_to_do(railyard):
    railyard.project = existing_estate()
    railyard.put_response = FakeResponse(412, {"error": "revision conflict"})
    kept = []
    with pytest.raises(ImportRefused, match="changed in Railyard while the import ran"):
        make(railyard, project="ldn1-baseline").run(dry_run=False, on_refused=lambda doc, e: kept.append(doc) or [])
    assert kept and kept[0]["id"] == "ry-existing0000000000"


def test_the_rack_cap_is_railyards_typed_error(railyard):
    railyard.put_response = FakeResponse(
        402, {"error": "too many racks", "code": "plan_limit", "resource": "racks", "limit": 25, "current": 30}
    )
    with pytest.raises(RailyardPlanLimitError) as caught:
        make(railyard, name="x", project_id="ry-new").run(dry_run=False)
    assert (caught.value.limit, caught.value.current) == (25, 30)

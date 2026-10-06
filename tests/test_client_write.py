"""RailyardClient's write side: revisions from ETags, create vs replace with If-Match, named versions,
and the typed errors a refused save maps to (never carrying the token)."""

import pytest
from conftest import FakeResponse, FakeSession

from railyard_sync.client import RailyardClient
from railyard_sync.errors import (
    RailyardAPIError,
    RailyardConflictError,
    RailyardForbiddenError,
    RailyardPlanError,
    RailyardPlanLimitError,
    RailyardPlanRequiredError,
    RailyardPreconditionError,
    RailyardTermsError,
    RailyardTooLargeError,
)

PAT = "ry_" + "x1" * 12  # a fake personal access token
ORGS = [{"id": "org_acme", "name": "Acme Corp", "slug": "acme"}]
PROJECT = {"schemaVersion": "1", "id": "ry-0123456789abcdef0123", "name": "LDN1 baseline"}


def make_client(handler):
    session = FakeSession(
        lambda m, u, h, p: FakeResponse(200, ORGS) if u.endswith("/api/orgs") else handler(m, u, h, p)
    )
    return RailyardClient("https://railyard.sh", PAT, org="acme", session=session), session


def project_calls(session):
    return [c for c in session.calls if not c["url"].endswith("/api/orgs")]


def test_get_project_with_revision_reads_the_etag():
    client, _ = make_client(lambda *a: FakeResponse(200, PROJECT, headers={"ETag": '"42"'}))
    doc, revision = client.get_project_with_revision("ldn1-baseline")
    assert doc["id"] == PROJECT["id"]
    assert revision == 42


def test_etag_lookup_is_case_insensitive_and_accepts_weak_tags():
    client, _ = make_client(lambda *a: FakeResponse(200, PROJECT, headers={"etag": 'W/"7"'}))
    assert client.get_project_with_revision("x")[1] == 7


def test_missing_etag_is_an_error_not_a_guess():
    client, _ = make_client(lambda *a: FakeResponse(200, PROJECT))
    with pytest.raises(RailyardAPIError, match="ETag"):
        client.get_project_with_revision("x")


def test_put_project_creates_without_if_match():
    client, session = make_client(lambda *a: FakeResponse(200, {"id": PROJECT["id"]}, headers={"ETag": '"1"'}))
    assert client.put_project(PROJECT, if_match=None) == 1
    (call,) = project_calls(session)
    assert call["method"] == "PUT"
    assert call["url"] == f"https://railyard.sh/api/projects/{PROJECT['id']}"
    assert "If-Match" not in call["headers"]
    assert call["headers"]["X-Org-Id"] == "org_acme"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["json"] == PROJECT


def test_put_project_replaces_with_a_quoted_if_match():
    client, session = make_client(lambda *a: FakeResponse(200, {"id": PROJECT["id"]}, headers={"ETag": '"43"'}))
    assert client.put_project(PROJECT, if_match=42) == 43
    assert project_calls(session)[0]["headers"]["If-Match"] == '"42"'


def test_put_project_needs_an_id():
    client, _ = make_client(lambda *a: FakeResponse(200, {}))
    with pytest.raises(ValueError):
        client.put_project({"name": "x"}, if_match=None)


def test_name_version_posts_title_and_expected_revision():
    client, session = make_client(lambda *a: FakeResponse(201, {"id": "v1", "title": "NetBox import"}))
    assert client.name_version(PROJECT["id"], "NetBox import 2026-10-06", 43)["id"] == "v1"
    (call,) = project_calls(session)
    assert call["method"] == "POST"
    assert call["url"].endswith(f"/api/projects/{PROJECT['id']}/versions")
    assert call["json"] == {"title": "NetBox import 2026-10-06", "expectedCurrentRevision": 43}


def test_get_json_passes_params_and_org():
    client, session = make_client(lambda *a: FakeResponse(200, {"results": []}))
    assert client.get_json("/api/catalogue/search", {"q": "dell-r650"}) == {"results": []}
    (call,) = project_calls(session)
    assert call["params"] == {"q": "dell-r650"}
    assert call["headers"]["X-Org-Id"] == "org_acme"


@pytest.mark.parametrize("path", ["https://evil.example/api/x", "/oauth/token", "/api/../oauth"])
def test_get_json_only_reaches_the_api(path):
    client, _ = make_client(lambda *a: FakeResponse(200, {}))
    with pytest.raises(ValueError):
        client.get_json(path, None)


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


def test_402_plan_limit_carries_the_limit_details():
    client, _ = make_client(lambda *a: FakeResponse(402, PLAN_LIMIT))
    with pytest.raises(RailyardPlanLimitError) as exc:
        client.put_project(PROJECT, if_match=None)
    err = exc.value
    assert (err.plan, err.resource, err.limit, err.current, err.scope) == ("community", "racks", 25, 140, "estate")
    assert err.required_plans == ["team", "partner"]
    assert err.project_pass is False
    assert err.status == 402
    assert "25 racks per estate" in err.message
    assert isinstance(err, RailyardPlanError)


def test_402_plan_required_names_the_feature():
    body = {"error": "branches need Team", "code": "plan_required", "feature": "branches", "plan": "pro"}
    body |= {"requiredPlans": ["team"], "projectPass": False}
    client, _ = make_client(lambda *a: FakeResponse(402, body))
    with pytest.raises(RailyardPlanRequiredError) as exc:
        client.name_version(PROJECT["id"], "t", 1)
    assert exc.value.feature == "branches"
    assert exc.value.required_plans == ["team"]


@pytest.mark.parametrize("code", ["name_taken", "project_id_taken", "version_control_disabled"])
def test_409_carries_the_servers_code(code):
    client, _ = make_client(lambda *a: FakeResponse(409, {"error": "conflict", "code": code}))
    with pytest.raises(RailyardConflictError) as exc:
        client.put_project(PROJECT, if_match=None)
    assert exc.value.code == code
    assert exc.value.status == 409


@pytest.mark.parametrize("status", [412, 428])
def test_412_and_428_are_precondition_errors(status):
    client, _ = make_client(lambda *a: FakeResponse(status, {"error": "reload this project before saving it"}))
    with pytest.raises(RailyardPreconditionError) as exc:
        client.put_project(PROJECT, if_match=3)
    assert exc.value.status == status
    assert "reload this project" in str(exc.value)


def test_413_reports_size_and_limit():
    body = {"error": "too large", "code": "project_too_large", "limit": 1000, "size": 2000}
    client, _ = make_client(lambda *a: FakeResponse(413, body))
    with pytest.raises(RailyardTooLargeError) as exc:
        client.put_project(PROJECT, if_match=None)
    assert (exc.value.limit, exc.value.size) == (1000, 2000)


def test_403_terms_not_accepted_is_its_own_error():
    body = {"error": "accept the current Terms", "code": "terms_not_accepted", "termsVersion": "2026-10-06"}
    client, _ = make_client(lambda *a: FakeResponse(403, body))
    with pytest.raises(RailyardTermsError) as exc:
        client.put_project(PROJECT, if_match=None)
    assert "Terms of Service" in str(exc.value)
    assert isinstance(exc.value, RailyardForbiddenError)  # still a 403 to broad handlers


def test_403_on_a_write_says_change_and_includes_the_server_reason():
    client, _ = make_client(lambda *a: FakeResponse(403, {"error": "your role is read-only in this organisation"}))
    with pytest.raises(RailyardForbiddenError) as exc:
        client.put_project(PROJECT, if_match=None)
    assert not isinstance(exc.value, RailyardTermsError)
    assert "may not change" in str(exc.value) and "read-only" in str(exc.value)


@pytest.mark.parametrize("status", [400, 402, 403, 409, 412, 413, 500])
def test_errors_never_carry_the_token(status):
    echo = {"error": f"bad header Authorization: Bearer {PAT}", "code": "plan_limit"}
    client, _ = make_client(lambda *a: FakeResponse(status, echo))
    with pytest.raises(RailyardAPIError) as exc:
        client.put_project(PROJECT, if_match=1)
    assert PAT not in str(exc.value)
    assert PAT not in repr(exc.value.args)


def test_non_json_error_body_is_reported_without_the_token():
    client, _ = make_client(lambda *a: FakeResponse(502, None, text=f"<html>bad gateway {PAT}</html>"))
    with pytest.raises(RailyardAPIError) as exc:
        client.put_project(PROJECT, if_match=1)
    assert exc.value.status == 502
    assert "bad gateway" in str(exc.value) and PAT not in str(exc.value)

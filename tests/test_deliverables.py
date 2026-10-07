"""RailyardClient.deliverable_json POSTs to the deliverables route, and its plan refusals are the same typed
errors as a save's (one 402 family, parsed in one place)."""

import json

import pytest

from railyard_sync.client import RailyardClient
from railyard_sync.errors import (
    RailyardAPIError,
    RailyardNotFoundError,
    RailyardPlanError,
    RailyardPlanLimitError,
    RailyardPlanRequiredError,
    RailyardTokenRejectedError,
)

ORGS = [{"id": "org_acme", "name": "Acme Corp", "slug": "acme"}]


class Response:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class Session:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        if url.endswith("/api/orgs"):
            return Response(200, ORGS)
        return self.answer


def client(answer, **kw):
    session = Session(answer)
    return RailyardClient("https://railyard.sh", "ry_secret", session=session, **kw), session


def test_posts_options_and_change_request_with_the_org_header():
    doc = {"format": "railyard-netbox-sync", "version": 1}
    c, session = client(Response(200, doc), org="acme")
    got = c.deliverable_json("prj 1", "netbox-sync", options={"netboxVersion": "4.4"}, change_request_id="cr_1")
    assert got == doc
    call = session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "https://railyard.sh/api/projects/prj%201/deliverables/netbox-sync"
    assert call["json"] == {"changeRequestId": "cr_1", "options": {"netboxVersion": "4.4"}}
    assert call["headers"]["X-Org-Id"] == "org_acme"
    assert call["headers"]["Authorization"] == "Bearer ry_secret"


def test_empty_body_when_nothing_is_chosen():
    c, session = client(Response(200, {"format": "railyard-netbox-sync"}))
    c.netbox_sync_document("prj1")
    assert session.calls[-1]["json"] == {}


def test_plan_required_is_typed():
    body = {
        "error": "the Community plan does not include deliverable exports",
        "code": "plan_required",
        "feature": "deliverables",
        "deliverable": "netbox-sync",
        "plan": "community",
        "requiredPlans": ["pro", "team"],
        "projectPass": True,
    }
    c, _ = client(Response(402, body))
    with pytest.raises(RailyardPlanRequiredError) as exc:
        c.netbox_sync_document("prj1")
    err = exc.value
    assert (err.status, err.code, err.feature, err.deliverable) == (402, "plan_required", "deliverables", "netbox-sync")
    assert err.required_plans == ["pro", "team"] and err.project_pass and err.plan == "community"
    assert err.message == str(err) == "the Community plan does not include deliverable exports"
    assert isinstance(err, RailyardPlanError)


def test_plan_limit_is_typed():
    body = {
        "error": "the Pro plan exports deliverables for estates of up to 100 racks; this estate has 120",
        "code": "plan_limit",
        "plan": "pro",
        "resource": "racks",
        "limit": 100,
        "current": 120,
        "scope": "estate",
        "requiredPlans": ["team"],
        "projectPass": False,
    }
    c, _ = client(Response(402, body))
    with pytest.raises(RailyardPlanLimitError) as exc:
        c.netbox_sync_document("prj1")
    err = exc.value
    assert (err.code, err.resource, err.limit, err.current, err.scope) == ("plan_limit", "racks", 100, 120, "estate")
    assert (err.required_plans, err.project_pass) == (["team"], False)
    assert "up to 100 racks" in str(err)


def test_a_bare_402_is_still_a_plan_error():
    c, _ = client(Response(402, {"error": "payment required"}))
    with pytest.raises(RailyardPlanError) as exc:
        c.netbox_sync_document("prj1")
    assert not isinstance(exc.value, RailyardPlanLimitError | RailyardPlanRequiredError)
    assert exc.value.code == "" and "payment required" in str(exc.value)


def test_netbox_version_is_sent_as_an_option():
    c, session = client(Response(200, {"format": "railyard-netbox-sync"}))
    c.netbox_sync_document("prj1", netbox_version="4.4")
    assert session.calls[-1]["json"] == {"options": {"netboxVersion": "4.4"}}


@pytest.mark.parametrize("answer", [Response(200, ["not", "an", "object"]), Response(200, None, text="<html>")])
def test_a_document_that_is_not_a_json_object_is_refused(answer):
    c, _ = client(answer)
    with pytest.raises(RailyardAPIError, match="not"):
        c.netbox_sync_document("prj1")


@pytest.mark.parametrize(
    "answer, error",
    [
        (Response(401, {"error": "authentication required"}), RailyardTokenRejectedError),
        (Response(404, {"error": 'unknown deliverable "netbox-sync"'}), RailyardNotFoundError),
        (Response(500, None, text="<html>bad gateway</html>"), RailyardAPIError),
    ],
)
def test_other_failures_are_the_clients_errors(answer, error):
    c, _ = client(answer)
    with pytest.raises(error) as exc:
        c.netbox_sync_document("prj1")
    assert "ry_secret" not in str(exc.value)


def test_unknown_deliverable_message_is_kept():
    c, _ = client(Response(404, {"error": 'unknown deliverable "netbox-sync"; known: [cable-schedule]'}))
    with pytest.raises(RailyardNotFoundError, match="unknown deliverable"):
        c.netbox_sync_document("prj1")


def test_the_nautobot_sync_document_is_its_own_deliverable_without_options():
    doc = {"format": "railyard-nautobot-sync", "version": 1}
    c, session = client(Response(200, doc), org="acme")
    assert c.nautobot_sync_document("prj1", change_request_id="cr_2") == doc
    call = session.calls[-1]
    assert call["url"] == "https://railyard.sh/api/projects/prj1/deliverables/nautobot-sync"
    assert call["json"] == {"changeRequestId": "cr_2"}


def test_a_nautobot_sync_document_that_is_not_an_object_is_an_error():
    c, _ = client(Response(200, ["not", "a", "document"]))
    with pytest.raises(RailyardAPIError, match="Nautobot sync document that is not a JSON object"):
        c.nautobot_sync_document("prj1")


def test_the_nautobot_deliverable_is_refused_like_any_other():
    body = {"error": "deliverables need a paid plan", "code": "plan_required", "deliverable": "nautobot-sync"}
    c, _ = client(Response(402, body | {"requiredPlans": ["pro"], "projectPass": True, "plan": "community"}))
    with pytest.raises(RailyardPlanRequiredError) as exc:
        c.nautobot_sync_document("prj1")
    assert exc.value.deliverable == "nautobot-sync" and exc.value.required_plans == ["pro"]

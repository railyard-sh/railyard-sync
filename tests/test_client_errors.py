"""RailyardClient's failures: every status mapped to a message that says what happened and what to do, with
the method, path, the server's error and code, its details and the request id; bounded retries of busy
answers for requests that are safe to repeat; and DEBUG request logs that never carry a token or a body."""

from __future__ import annotations

import logging

import pytest
import requests
from conftest import FakeResponse, FakeSession

from railyard_sync.client import RailyardClient
from railyard_sync.errors import (
    RailyardAPIError,
    RailyardBadRequestError,
    RailyardBusyError,
    RailyardConflictError,
    RailyardConnectionError,
    RailyardForbiddenError,
    RailyardNotFoundError,
    RailyardPreconditionError,
    RailyardServerError,
    RailyardTermsError,
    RailyardTokenRejectedError,
    RailyardTooLargeError,
)

PAT = "ry_" + "e5" * 12
RID = "4765ead6c0ffee"
PROJECT = {
    "schemaVersion": "1",
    "id": "prj_muwxiax117y0fx3xz",
    "name": "LDN1",
    "racks": [
        {"id": "nb-rack-100", "name": "A01", "placements": [{"id": "nb-dev-1", "label": "sw1"}]},
        {"id": "nb-rack-101", "name": "A01", "placements": []},
    ],
}
PATH = "/api/projects/prj_muwxiax117y0fx3xz"


def make_client(handler, **kwargs):
    session = FakeSession(handler)
    sleeps: list[float] = []
    client = RailyardClient("https://railyard.sh", PAT, session=session, sleep=sleeps.append, **kwargs)
    return client, session, sleeps


def answer(status, payload=None, **headers):
    return lambda *a: FakeResponse(status, payload, headers={"X-Request-ID": RID, **headers})


def save(handler, if_match: int | None = 12):
    client, session, sleeps = make_client(handler)
    with pytest.raises(RailyardAPIError) as exc:
        client.put_project(PROJECT, if_match=if_match)
    return exc.value, session, sleeps


def text(err: RailyardAPIError) -> str:
    out = str(err)
    assert PAT not in out
    return out


# ---- each status -----------------------------------------------------------------------------------------


def test_every_error_names_the_request_the_status_and_the_request_id():
    err, _, _ = save(answer(500, {"error": "internal server error"}))
    assert (err.method, err.path, err.status, err.request_id) == ("PUT", PATH, 500, RID)
    assert err.server_message == "internal server error"
    out = text(err)
    assert f"PUT {PATH}" in out and "HTTP 500" in out and "internal server error" in out
    assert out.endswith(f"Quote request id {RID} when reporting this.")


def test_without_a_response_header_the_id_the_client_sent_is_quoted():
    client, session, _ = make_client(lambda *a: FakeResponse(500, {"error": "internal server error"}))
    with pytest.raises(RailyardServerError) as exc:
        client.get_project("x")
    sent = session.calls[-1]["headers"]["X-Request-ID"]
    assert sent.startswith("rys-") and exc.value.request_id == sent and sent in str(exc.value)


def test_500_on_a_save_is_a_railyard_bug_and_a_conditional_save_is_safe_to_repeat():
    err, _, _ = save(answer(500, {"error": "internal server error"}))
    assert isinstance(err, RailyardServerError)
    out = text(err)
    assert out.startswith(f"Railyard failed while saving the estate (HTTP 500 for PUT {PATH}): internal server error")
    assert "This is a Railyard bug, not a fault in your data" in out
    assert "safe" in out and "never overwrite" in out


def test_500_on_a_create_says_to_check_before_running_again():
    bad_gateway = FakeResponse(502, text="<html>Bad Gateway</html>", headers={"X-Request-ID": RID})
    err, _, _ = save(lambda *a: bad_gateway, if_match=None)
    assert isinstance(err, RailyardServerError)
    assert "check in Railyard whether the estate was created" in text(err)
    assert "Bad Gateway" in text(err)


def test_400_lists_the_problems_by_rack_and_device_name():
    body = {
        "error": 'rack name "A01" is already used in this space',
        "code": "rack.name-duplicate",
        "problems": [
            {"severity": "error", "code": "rack.name-duplicate", "message": "duplicate", "rackId": "nb-rack-101"},
            {"severity": "error", "code": "placement.out-of-bounds", "message": "out", "placementId": "nb-dev-1"},
        ],
    }
    err, _, _ = save(answer(400, body))
    assert isinstance(err, RailyardBadRequestError) and err.code == "rack.name-duplicate"
    assert len(err.problems) == 2
    out = text(err)
    assert f"Railyard refused the document (HTTP 400, rack.name-duplicate) for PUT {PATH}" in out
    assert "rack 'A01' (nb-rack-101): duplicate [rack.name-duplicate]" in out
    assert "device 'sw1' (nb-dev-1), in rack 'A01' (nb-rack-100): out [placement.out-of-bounds]" in out
    assert "fix what is named in NetBox" in out and RID in out


def test_400_names_a_field():
    err, _, _ = save(answer(400, {"error": 'unknown project field "x"', "field": "x"}))
    assert "Field: x" in text(err)


def test_details_the_message_does_not_render_are_listed():
    err, _, _ = save(answer(409, {"error": "conflict", "code": "history_quota", "quota": 500, "used": 500}))
    assert "Details: quota: 500; used: 500" in text(err)
    err, _, _ = save(answer(418, {"error": "teapot", "hint": "brew"}))
    assert text(err).startswith(f"Railyard API error (HTTP 418) for PUT {PATH}: teapot")
    assert 'Details: hint: "brew"' in text(err)


def test_401_says_how_to_replace_the_token():
    err, _, _ = save(answer(401, {"error": "authentication required"}))
    assert isinstance(err, RailyardTokenRejectedError)
    out = text(err)
    assert "User settings → API tokens" in out and "RAILYARD_TOKEN" in out and RID in out


@pytest.mark.parametrize(
    ("payload", "kind", "says"),
    [
        ({"error": "terms", "code": "terms_not_accepted"}, RailyardTermsError, "Terms of Service"),
        ({"error": "your role is read-only in this organisation"}, RailyardForbiddenError, "read-only role"),
        ({"error": "not a member of that organisation"}, RailyardForbiddenError, "not a member"),
        ({"error": "forbidden"}, RailyardForbiddenError, "may not change this estate"),
    ],
)
def test_403_says_which_permission(payload, kind, says):
    err, _, _ = save(answer(403, payload))
    assert type(err) is kind
    assert says in text(err) and RID in text(err)


def test_404_explains_the_project_reference():
    client, _, _ = make_client(answer(404, {"error": "project not found"}))
    with pytest.raises(RailyardNotFoundError) as exc:
        client.get_project("ghost")
    out = text(exc.value)
    assert "Not found (HTTP 404) for GET /api/projects/ghost: project not found" in out
    assert "Check --project" in out and RID in out


@pytest.mark.parametrize(
    ("code", "says"),
    [
        ("name_taken", "choose another --name"),
        ("project_id_taken", "a new estate gets a new id"),
        ("history_quota", "history is full"),
        ("version_control_disabled", "switch it on"),
    ],
)
def test_409_codes_say_what_to_do(code, says):
    err, _, _ = save(answer(409, {"error": "conflict", "code": code}))
    assert isinstance(err, RailyardConflictError) and err.code == code
    assert f"HTTP 409, {code}" in text(err) and says in text(err) and RID in text(err)


def test_412_says_to_run_again_to_merge_onto_the_latest():
    err, _, _ = save(answer(412, {"error": "revision conflict"}))
    assert isinstance(err, RailyardPreconditionError)
    out = text(err)
    assert "changed in Railyard after it was read" in out and "merges onto it" in out and RID in out


def test_428_says_to_refresh_with_project():
    err, _, _ = save(answer(428, {"error": "reload this project before saving it"}), if_match=None)
    assert isinstance(err, RailyardPreconditionError)
    assert "--project" in text(err) and "reload this project" in text(err)


def test_413_gives_the_size_and_the_limit_and_suggests_splitting():
    body = {"error": "too large", "code": "project_too_large", "limit": 8_000_000, "size": 9_500_000}
    err, _, _ = save(answer(413, body))
    assert isinstance(err, RailyardTooLargeError) and (err.size, err.limit) == (9_500_000, 8_000_000)
    out = text(err)
    assert "9.5 MB sent, the limit is 8.0 MB" in out and "separate estates" in out and RID in out


def test_413_without_details_reports_what_was_sent():
    err, _, _ = save(answer(413, {"error": "request body too large"}))
    assert err.size and "sent" in text(err)


# ---- busy answers and retries -----------------------------------------------------------------------------


def busy_then_ok(times: int, status: int = 503, retry_after: str = "2"):
    state = {"n": 0}

    def handler(method, url, headers, params):
        state["n"] += 1
        if state["n"] <= times:
            return FakeResponse(
                status, {"error": "server is busy; try again shortly"}, headers={"Retry-After": retry_after}
            )
        return FakeResponse(200, PROJECT, headers={"ETag": '"13"'})

    return handler


def test_a_get_is_retried_after_retry_after():
    client, session, sleeps = make_client(busy_then_ok(2))
    assert client.get_project("x")["id"] == PROJECT["id"]
    assert len(session.calls) == 3 and sleeps == [2.0, 2.0]
    ids = {c["headers"]["X-Request-ID"] for c in session.calls}
    assert len(ids) == 3  # each attempt is its own request


def test_retries_are_bounded():
    client, session, sleeps = make_client(busy_then_ok(99, status=429))
    with pytest.raises(RailyardBusyError) as exc:
        client.get_project("x")
    assert len(session.calls) == 3 and len(sleeps) == 2
    assert exc.value.retry_after == 2.0
    assert "Tried 3 times" in str(exc.value) and "Try again in 2 s" in str(exc.value)


def test_a_wait_longer_than_the_cap_is_not_slept():
    client, session, sleeps = make_client(busy_then_ok(1, retry_after="600"))
    with pytest.raises(RailyardBusyError) as exc:
        client.get_project("x")
    assert len(session.calls) == 1 and sleeps == []
    assert "Try again in 600 s" in str(exc.value)


def test_a_put_with_if_match_is_retried():
    client, session, sleeps = make_client(busy_then_ok(1))
    assert client.put_project(PROJECT, if_match=12) == 13
    assert len(session.calls) == 2 and sleeps == [2.0]


def test_a_put_without_if_match_is_never_retried():
    client, session, sleeps = make_client(busy_then_ok(1))
    with pytest.raises(RailyardBusyError):
        client.put_project(PROJECT, if_match=None)
    assert len(session.calls) == 1 and sleeps == []


def test_a_connection_failure_is_retried_for_a_get_but_not_a_put():
    def down(*a):
        raise requests.ConnectionError(f"refused (Authorization: Bearer {PAT})")

    client, session, sleeps = make_client(down)
    with pytest.raises(RailyardConnectionError) as exc:
        client.get_project("x")
    assert len(session.calls) == 3 and sleeps == [1.0, 2.0]
    assert "Could not reach Railyard at https://railyard.sh for GET /api/projects/x" in text(exc.value)

    client, session, sleeps = make_client(down)
    with pytest.raises(RailyardConnectionError):
        client.put_project(PROJECT, if_match=12)
    assert len(session.calls) == 1 and sleeps == []


def test_validate_posts_the_document_and_is_retried():
    def handler(method, url, headers, params):
        if len(session.calls) == 1:
            return FakeResponse(503, {"error": "busy"}, headers={"Retry-After": "1"})
        return FakeResponse(200, {"problems": [{"severity": "warning", "code": "c", "message": "m"}]})

    client, session, sleeps = make_client(handler)
    assert client.validate(PROJECT) == {
        "problems": [{"severity": "warning", "code": "c", "message": "m"}],
        "truncated": False,
    }
    assert [(c["method"], c["url"]) for c in session.calls] == [("POST", "https://railyard.sh/api/validate")] * 2
    assert session.calls[-1]["json"] == PROJECT


# ---- request logs -----------------------------------------------------------------------------------------


def test_requests_are_logged_with_status_time_size_and_request_id_but_no_token_or_body(caplog):
    caplog.set_level(logging.DEBUG, logger="railyard_sync")
    client, _, _ = make_client(lambda *a: FakeResponse(200, PROJECT, headers={"ETag": '"13"', "X-Request-ID": RID}))
    client.put_project(PROJECT, if_match=12)
    (line,) = [r.getMessage() for r in caplog.records if r.name == "railyard_sync.client"]
    assert line.startswith(f"Railyard PUT {PATH} -> 200 in ")
    assert f"request id {RID}" in line and "sent " in line and 'If-Match "12"' in line and 'ETag "13"' in line
    assert PAT not in caplog.text and "nb-rack-100" not in caplog.text and "Bearer" not in caplog.text

"""NetBox failures, for the import's loader and the export's REST client alike: the status, NetBox's own
``detail`` (or its per-field errors), the endpoint, NetBox's request id, and for a 403 the permission to
grant, named by object type and action. Request logs carry no token."""

from __future__ import annotations

import logging

import pytest
from conftest import FakeResponse, FakeSession

from railyard_sync import netbox_http
from railyard_sync.dcim.errors import DCIMAuthError, DCIMError, DCIMNotFoundError
from railyard_sync.dcim.netbox import NetBoxLoader
from railyard_sync.export.netbox_rest import NetBoxAuthError, NetBoxClient, NetBoxError, NetBoxPermissionError

URL = "https://netbox.example.com"
TOKEN = "0f" * 20  # a fake v1 NetBox token
RID = "a1b2c3d4-0000-4000-8000-000000000001"
DENIED = {"detail": "You do not have permission to perform this action."}


def respond(status, payload=None, text=""):
    return FakeSession(lambda *a: FakeResponse(status, payload, text=text, headers={"X-Request-ID": RID}))


def loader(session) -> NetBoxLoader:
    return NetBoxLoader(URL, TOKEN, session=session)


def client(session) -> NetBoxClient:
    return NetBoxClient(URL, TOKEN, session=session)


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/api/dcim/racks/", "dcim.rack"),
        ("/api/dcim/device-types/12/", "dcim.devicetype"),
        ("/api/dcim/front-port-templates/", "dcim.frontporttemplate"),
        ("/api/extras/custom-fields/", "extras.customfield"),
        ("/api/status/", ""),
    ],
)
def test_object_type_is_named_as_netbox_permissions_name_it(path, kind):
    assert netbox_http.object_type(path) == kind


def test_a_403_on_a_read_names_the_view_permission_and_the_request_id():
    with pytest.raises(DCIMAuthError) as exc:
        loader(respond(403, DENIED)).fetch("/api/dcim/racks/", [("site_id", 1)])
    message = str(exc.value)
    assert "NetBox refused GET /api/dcim/racks/ (HTTP 403): You do not have permission" in message
    assert "'view' permission on dcim.rack" in message and "Write enabled" not in message
    assert f"Quote NetBox request id {RID}" in message
    assert (exc.value.status, exc.value.request_id, exc.value.path) == (403, RID, "/api/dcim/racks/")


def test_a_403_on_a_write_names_the_add_permission_and_write_enabled_tokens():
    with pytest.raises(NetBoxPermissionError) as exc:
        client(respond(403, DENIED)).create("dcim/devices", {"name": "sw1"})
    message = str(exc.value)
    assert "NetBox refused POST /api/dcim/devices/ (HTTP 403)" in message
    assert "'add' permission on dcim.device" in message and "Write enabled" in message and RID in message


@pytest.mark.parametrize("make", [loader, client])
def test_401_says_to_replace_the_token(make):
    session = respond(401, {"detail": "Invalid token"})
    with pytest.raises((DCIMAuthError, NetBoxAuthError)) as exc:
        if make is loader:
            make(session).fetch("/api/status/")
        else:
            make(session).status()
    message = str(exc.value)
    assert "HTTP 401" in message and "Invalid token" in message and "NETBOX_TOKEN" in message and RID in message
    assert TOKEN not in message


def test_a_400_lists_netboxs_field_errors():
    payload = {"name": ["This field is required."], "site": ['Invalid pk "9" - object does not exist.']}
    with pytest.raises(NetBoxError) as exc:
        client(respond(400, payload)).create("dcim/racks", {})
    message = str(exc.value)
    assert "NetBox refused POST /api/dcim/racks/ (HTTP 400): name: This field is required.; site: Invalid pk" in message
    assert RID in message


def test_a_500_html_page_is_described_not_dumped():
    with pytest.raises(DCIMError) as exc:
        loader(respond(500, text="<html><body>Server Error</body></html>")).fetch("/api/dcim/sites/")
    message = str(exc.value)
    assert "NetBox failed (HTTP 500) for GET /api/dcim/sites/: an HTML page" in message and RID in message


def test_404_names_the_endpoint():
    with pytest.raises(DCIMNotFoundError) as exc:
        loader(respond(404, {"detail": "Not found."})).fetch("/api/dcim/rack-types/")
    assert "Not found in NetBox (HTTP 404): GET /api/dcim/rack-types/: Not found." in str(exc.value)


def test_requests_are_logged_with_the_request_id_and_never_the_token(caplog):
    caplog.set_level(logging.DEBUG, logger="railyard_sync")
    session = FakeSession(lambda *a: FakeResponse(201, {"id": 7}, headers={"X-Request-ID": RID}))
    client(session).create("dcim/sites", {"name": "LDN1 secret site", "slug": "ldn1"})
    (line,) = [r.getMessage() for r in caplog.records if r.name.endswith("netbox_rest")]
    assert line.startswith("NetBox POST /api/dcim/sites/ -> 201 in ") and f"request id {RID}" in line
    assert "sent " in line
    assert TOKEN not in caplog.text and "secret site" not in caplog.text

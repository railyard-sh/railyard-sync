"""Railyard deliverables over the REST API, and the plan errors they can be refused with.

A deliverable is generated server-side from a project's stored design, after the estate's plan has
been checked:

    POST /api/projects/{id}/deliverables/{kind}      body: {"changeRequestId"?, "options"?}

The NetBox sync document (kind ``netbox-sync``) is the one this package reads: it is the source of
``railyard-sync export netbox`` (see ``export.sync_document``).

A plan that does not include deliverables is refused with HTTP 402 ``{"code": "plan_required", …}``,
and an estate larger than the plan's rack cap with HTTP 402 ``{"code": "plan_limit", "resource":
"racks", "limit", "current", …}``. Both are raised as typed errors carrying those fields.

This is written as functions over a :class:`~railyard_sync.client.RailyardClient` (rather than
client methods) while ``client.py`` is changed elsewhere; ``deliverable_json`` is meant to become
``RailyardClient.deliverable_json`` and the 402 errors to move to ``errors.py``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .client import RailyardClient
from .errors import (
    RailyardAPIError,
    RailyardForbiddenError,
    RailyardNotFoundError,
    RailyardTokenRejectedError,
)

NETBOX_SYNC = "netbox-sync"


class RailyardPaymentRequiredError(RailyardAPIError):
    """402 — the estate's plan does not allow this. ``body`` is the server's JSON answer."""

    def __init__(self, message: str, *, status: int | None = 402, body: dict | None = None) -> None:
        super().__init__(message, status=status)
        self.body = body or {}
        self.code: str = str(self.body.get("code") or "")
        self.plan: str = str(self.body.get("plan") or "")
        self.required_plans: list[str] = list(self.body.get("requiredPlans") or [])
        self.project_pass: bool = bool(self.body.get("projectPass"))


class RailyardPlanRequiredError(RailyardPaymentRequiredError):
    """402 ``plan_required`` — the plan does not include this feature or deliverable."""

    @property
    def feature(self) -> str:
        return str(self.body.get("feature") or "")

    @property
    def deliverable(self) -> str:
        return str(self.body.get("deliverable") or "")


class RailyardPlanLimitError(RailyardPaymentRequiredError):
    """402 ``plan_limit`` — the estate is over one of the plan's limits (racks per estate)."""

    @property
    def resource(self) -> str:
        return str(self.body.get("resource") or "")

    @property
    def limit(self) -> int | None:
        value = self.body.get("limit")
        return value if isinstance(value, int) else None

    @property
    def current(self) -> int | None:
        value = self.body.get("current")
        return value if isinstance(value, int) else None


def _body(resp: Any) -> dict:
    try:
        data = resp.json()
    except Exception:  # not JSON (a proxy's error page, say)
        return {}
    return data if isinstance(data, dict) else {}


def _payment_required(body: dict, kind: str) -> RailyardPaymentRequiredError:
    server = str(body.get("error") or "").strip()
    code = body.get("code")
    upgrade = ""
    if body.get("requiredPlans"):
        upgrade = f" Plans that include it: {', '.join(map(str, body['requiredPlans']))}."
    if body.get("projectPass"):
        upgrade += " A Project Pass for this estate would also allow it."
    if code == "plan_limit":
        message = server or (
            f"The {kind} deliverable was refused: the estate has {body.get('current')} {body.get('resource')}, "
            f"over the plan's limit of {body.get('limit')}."
        )
        return RailyardPlanLimitError(f"{message} (HTTP 402){upgrade}", body=body)
    if code == "plan_required":
        message = server or f"The {kind} deliverable is not included in this estate's plan."
        return RailyardPlanRequiredError(f"{message} (HTTP 402){upgrade}", body=body)
    return RailyardPaymentRequiredError(f"{server or 'Railyard requires a paid plan for this'} (HTTP 402)", body=body)


def deliverable_json(
    client: RailyardClient,
    project_ref: str,
    kind: str,
    *,
    options: dict | None = None,
    change_request_id: str | None = None,
    org_id: str | None = None,
) -> Any:
    """Generate a JSON deliverable for a project (by id or URL slug) and return the decoded document.

    ``options`` are the generator's (for example ``{"netboxVersion": "4.4"}``); ``change_request_id``
    generates it from a merge request's draft instead of main. Raises ``RailyardPlanRequiredError`` or
    ``RailyardPlanLimitError`` when the plan refuses it, and the client's usual typed errors otherwise.
    """
    if not project_ref:
        raise ValueError("project ref is required")
    if not kind:
        raise ValueError("deliverable kind is required")
    body: dict[str, Any] = {}
    if change_request_id:
        body["changeRequestId"] = change_request_id
    if options:
        body["options"] = dict(options)

    path = f"/api/projects/{quote(str(project_ref), safe='')}/deliverables/{quote(kind, safe='')}"
    headers = client._headers(org_id or client.org_id())
    headers["Content-Type"] = "application/json"
    resp = client._session.request(
        "POST", f"{client.base_url}{path}", headers=headers, json=body, timeout=client._timeout
    )
    status = resp.status_code
    if status == 401:
        raise RailyardTokenRejectedError(
            "Railyard rejected the personal access token (HTTP 401): it is wrong, revoked or expired. "
            "Create a new token in Railyard (User settings → Personal access tokens).",
            status=status,
        )
    if status == 402:
        raise _payment_required(_body(resp), kind)
    if status == 403:
        raise RailyardForbiddenError(
            f"Railyard refused access (HTTP 403) for {path}: the token is valid, but its user may not read "
            "this project.",
            status=status,
        )
    if status == 404:
        message = str(_body(resp).get("error") or "") or "project or deliverable not found"
        raise RailyardNotFoundError(f"Not found (HTTP 404): {path}: {message}", status=status)
    if status < 200 or status >= 300:
        message = str(_body(resp).get("error") or "")
        if not message:
            try:
                message = resp.text[:400]
            except Exception:  # pragma: no cover - defensive
                message = ""
        raise RailyardAPIError(f"Railyard API error (HTTP {status}) for {path}: {message}", status=status)
    try:
        return resp.json()
    except ValueError:
        raise RailyardAPIError(f"Railyard returned a {kind} deliverable that is not JSON.", status=status) from None


def netbox_sync_document(
    client: RailyardClient,
    project_ref: str,
    *,
    netbox_version: str | None = None,
    change_request_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """The project's NetBox sync document (``POST …/deliverables/netbox-sync``), for ``sync_to_netbox``.

    ``netbox_version`` is the NetBox release the document is written for (Railyard's default otherwise).
    """
    options = {"netboxVersion": netbox_version} if netbox_version else None
    doc = deliverable_json(
        client, project_ref, NETBOX_SYNC, options=options, change_request_id=change_request_id, org_id=org_id
    )
    if not isinstance(doc, dict):
        raise RailyardAPIError("Railyard returned a NetBox sync document that is not a JSON object.")
    return doc

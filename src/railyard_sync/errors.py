"""Typed errors for the Railyard client, so callers can distinguish auth/not-found from
other failures without string-matching."""

from __future__ import annotations


class RailyardAPIError(Exception):
    """A Railyard API request failed. Carries the HTTP status when there was a response."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RailyardAuthError(RailyardAPIError):
    """401/403 — the personal access token is missing, wrong, expired, or lacks access."""


class RailyardTokenRejectedError(RailyardAuthError):
    """401 — Railyard did not accept the token at all: it is wrong, revoked or expired. Railyard
    personal access tokens expire, so this is the normal symptom of a token that needs rotating."""


class RailyardForbiddenError(RailyardAuthError):
    """403 — the token is valid but its user may not read this org or project."""


class RailyardNotFoundError(RailyardAPIError):
    """404 — the org or project reference did not resolve."""


class RailyardTermsError(RailyardForbiddenError):
    """403 ``terms_not_accepted`` — the token's user has yet to accept Railyard's current Terms of
    Service, so every project write is refused until they sign in to Railyard in a browser and do."""


class RailyardPlanError(RailyardAPIError):
    """402 — the estate's plan does not allow the request. The one family for every plan refusal,
    whether a project save past the rack cap or a paid deliverable; ``RailyardClient`` parses each 402
    into it in one place.

    Every field the server may send is carried (empty or ``None`` when it did not send it):

    - ``code``: ``plan_limit`` or ``plan_required`` (empty for a bare 402).
    - ``plan``: the plan the estate is on (``community``, ``pro``…, or ``project-pass``).
    - ``resource``, ``limit``, ``current``, ``scope``: for ``plan_limit``, the change would take
      ``resource`` (``racks``) to ``current`` where the plan allows ``limit`` per ``scope`` (``estate``).
    - ``feature``, ``deliverable``: for ``plan_required``, what the plan lacks (``deliverables``,
      ``branches``…) and, for a deliverable, which one (``netbox-sync``…).
    - ``required_plans``: the purchasable plans that would allow it, in catalogue order.
    - ``project_pass``: whether a Project Pass for this estate would also allow it.
    - ``message``: the server's own wording (also ``str(error)``).
    """

    code_name = ""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = 402,
        code: str | None = None,
        plan: str = "",
        resource: str = "",
        limit: int | None = None,
        current: int | None = None,
        scope: str = "",
        required_plans: list[str] | None = None,
        project_pass: bool = False,
        feature: str = "",
        deliverable: str = "",
    ) -> None:
        super().__init__(message, status=status)
        self.message = message
        self.code = self.code_name if code is None else code
        self.plan = plan
        self.resource = resource
        self.limit = limit
        self.current = current
        self.scope = scope
        self.required_plans = list(required_plans or [])
        self.project_pass = project_pass
        self.feature = feature
        self.deliverable = deliverable


class RailyardPlanLimitError(RailyardPlanError):
    """402 ``plan_limit`` — a scale limit, such as racks per estate: the change (or the estate a
    deliverable is generated for) takes ``resource`` to ``current`` where the plan allows ``limit``."""

    code_name = "plan_limit"


class RailyardPlanRequiredError(RailyardPlanError):
    """402 ``plan_required`` — the plan does not include a feature (``feature``), such as branches or
    deliverables (``deliverable`` names which one was asked for)."""

    code_name = "plan_required"


class RailyardConflictError(RailyardAPIError):
    """409 — the write conflicts with server state. ``code`` says which: ``name_taken`` (another
    project has the name), ``project_id_taken`` (the new id is in use anywhere on the platform),
    ``history_quota``, ``version_control_disabled``…; empty when the server sent none."""

    def __init__(self, message: str, *, status: int | None = 409, code: str = "") -> None:
        super().__init__(message, status=status)
        self.code = code


class RailyardPreconditionError(RailyardAPIError):
    """412/428 — the project changed since it was read (412), or a replacement was sent without the
    revision it replaces (428). Read it again and retry."""


class RailyardTooLargeError(RailyardAPIError):
    """413 — the document is larger than the server accepts. ``limit`` and ``size`` are bytes, when
    the server reported them."""

    def __init__(self, message: str, *, status: int | None = 413, limit: int | None = None, size: int | None = None):
        super().__init__(message, status=status)
        self.limit = limit
        self.size = size

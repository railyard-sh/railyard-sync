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
    """402 — the organisation's plan does not allow the change. Carries the plan the server reported
    and the purchasable plans that would (``required_plans``, in catalogue order)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = 402,
        plan: str = "",
        required_plans: list[str] | None = None,
        project_pass: bool = False,
    ) -> None:
        super().__init__(message, status=status)
        self.plan = plan
        self.required_plans = list(required_plans or [])
        self.project_pass = project_pass


class RailyardPlanLimitError(RailyardPlanError):
    """402 ``plan_limit`` — a scale limit, such as racks per estate: the change would take
    ``resource`` to ``current`` where the plan allows ``limit``. ``message`` is the server's own
    wording; ``project_pass`` reports that a Project Pass for this estate would also allow it."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = 402,
        plan: str = "",
        resource: str = "",
        limit: int | None = None,
        current: int | None = None,
        scope: str = "",
        required_plans: list[str] | None = None,
        project_pass: bool = False,
    ) -> None:
        super().__init__(message, status=status, plan=plan, required_plans=required_plans, project_pass=project_pass)
        self.resource = resource
        self.limit = limit
        self.current = current
        self.scope = scope

    @property
    def message(self) -> str:
        return str(self)


class RailyardPlanRequiredError(RailyardPlanError):
    """402 ``plan_required`` — the plan does not include a feature (``feature``), such as branches."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = 402,
        plan: str = "",
        feature: str = "",
        required_plans: list[str] | None = None,
        project_pass: bool = False,
    ) -> None:
        super().__init__(message, status=status, plan=plan, required_plans=required_plans, project_pass=project_pass)
        self.feature = feature


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

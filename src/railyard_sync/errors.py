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

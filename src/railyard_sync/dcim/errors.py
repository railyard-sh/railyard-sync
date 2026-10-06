"""Typed errors for the DCIM loaders, so callers can tell a bad token, an unknown site, an unreachable
server and an unsupported version apart without string-matching.

Messages never contain the API token: the loaders scrub anything that could echo it back.
"""

from __future__ import annotations


class DCIMError(Exception):
    """Reading from the DCIM failed. Carries the HTTP status when there was a response."""

    def __init__(
        self, message: str, *, status: int | None = None, request_id: str = "", method: str = "", path: str = ""
    ) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.method = method
        self.path = path


class DCIMAuthError(DCIMError):
    """401/403: the API token is missing, wrong, expired, or may not read what was asked for."""


class DCIMNotFoundError(DCIMError):
    """A requested site (or another object the load depends on) does not exist."""


class DCIMConnectionError(DCIMError):
    """The DCIM could not be reached: DNS, TLS, a refused connection or a timeout."""


class DCIMVersionError(DCIMError):
    """The DCIM reports a version the loader does not support."""

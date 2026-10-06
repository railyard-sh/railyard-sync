"""Logging for railyard-sync: the ``railyard_sync`` logger tree, secret redaction and small formatters.

Every module logs to a child of ``railyard_sync`` (``logging.getLogger(__name__)``). Nothing here prints:
a library user configures logging as it likes, and the CLI calls :func:`configure_cli` for its stderr
progress lines and its ``--log-file``.

What may be logged: methods, paths, query filters, statuses, timings, sizes and request ids. What never
is: tokens, ``Authorization`` headers and request or response bodies (a Project JSON document or a NetBox
object holds the customer's infrastructure). :class:`RedactingFilter` is a second line of defence that
removes registered secrets and anything shaped like a token from every record a CLI handler writes.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TextIO

ROOT = "railyard_sync"

#: Records with ``extra={FILE_ONLY: True}`` reach the log file but not the console (the CLI prints the
#: final error itself, so the file gets the record and its traceback without a duplicate on stderr).
FILE_ONLY = "railyard_file_only"

_HANDLER_MARK = "_railyard_sync_cli"

# Token shapes redacted even when not registered: Railyard PATs, NetBox v2 tokens, and the value of any
# Authorization-style header that reached a message.
_TOKEN_PATTERNS = (
    (re.compile(r"\bry_[A-Za-z0-9_\-]{6,}"), "***"),
    (re.compile(r"\bnbt_[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)?"), "***"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|token)\s+)?[^\s,;'\"}]+"), r"\1***"),
    (re.compile(r"(?i)\b(bearer|token)(\s+)(?=[A-Za-z_\-.]*\d)[A-Za-z0-9_\-.]{12,}"), r"\1\2***"),
)


class RedactingFilter(logging.Filter):
    """Replace registered secrets (and token-shaped text) in a record's message and traceback with ``***``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        """Register a secret, and each part of a v2 NetBox token (``nbt_<key>.<secret>``)."""
        if not secret:
            return
        for part in {secret, *secret.split(".")}:
            if len(part) >= 6:
                self._secrets.add(part)

    def scrub(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        for pattern, replacement in _TOKEN_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a malformed record: keep what we have
            message = str(record.msg)
        record.msg, record.args = self.scrub(message), ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        return True


class _ConsoleFormatter(logging.Formatter):
    """Progress lines as they are; warnings and errors prefixed; verbose detail indented. Tracebacks only
    with ``--debug``."""

    def __init__(self, tracebacks: bool) -> None:
        super().__init__()
        self.tracebacks = tracebacks

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno >= logging.ERROR:
            text = f"error: {message}"
        elif record.levelno >= logging.WARNING:
            text = f"warning: {message}"
        elif record.levelno >= logging.INFO:
            text = message
        else:
            text = f"  {message}"
        if self.tracebacks and record.exc_text:
            text += "\n" + record.exc_text
        return text


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        stamp = datetime.fromtimestamp(record.created, UTC)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


class _NotFileOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, FILE_ONLY, False)


class CLILogging:
    """The CLI's handlers on the ``railyard_sync`` logger, removed again by :meth:`close`."""

    def __init__(self, stream: TextIO, *, level: int, log_file: str | None, tracebacks: bool) -> None:
        self.redact = RedactingFilter()
        self.tracebacks = tracebacks
        self.logger = logging.getLogger(ROOT)
        self._saved = (self.logger.level, self.logger.propagate)
        self.handlers: list[logging.Handler] = []
        for handler in list(self.logger.handlers):  # an earlier run in this process that did not close
            if getattr(handler, _HANDLER_MARK, False):
                self.logger.removeHandler(handler)
                handler.close()

        console = logging.StreamHandler(stream)
        console.setLevel(level)
        console.addFilter(self.redact)
        console.addFilter(_NotFileOnly())
        console.setFormatter(_ConsoleFormatter(tracebacks))
        self._add(console)
        if log_file:
            self._add(_file_handler(log_file, self.redact))
        self.logger.setLevel(logging.DEBUG if log_file else min(level, logging.INFO))
        self.logger.propagate = False

    def _add(self, handler: logging.Handler) -> None:
        setattr(handler, _HANDLER_MARK, True)
        self.logger.addHandler(handler)
        self.handlers.append(handler)

    def add_secret(self, secret: str | None) -> None:
        self.redact.add(secret)

    def scrub(self, text: str) -> str:
        return self.redact.scrub(text)

    def close(self) -> None:
        for handler in self.handlers:
            self.logger.removeHandler(handler)
            handler.close()
        self.handlers = []
        self.logger.setLevel(self._saved[0])
        self.logger.propagate = self._saved[1]


def _file_handler(path: str, redact: RedactingFilter) -> logging.Handler:
    """A debug-level handler appending to ``path``, which only its owner may read (it names the estate's
    objects)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    stream = os.fdopen(fd, "a", encoding="utf-8")
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.addFilter(redact)
    handler.setFormatter(_UTCFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    return handler


def configure_cli(
    stream: TextIO, *, verbose: bool = False, quiet: bool = False, debug: bool = False, log_file: str | None = None
) -> CLILogging:
    """Send ``railyard_sync`` logs to ``stream`` (progress at INFO; ``verbose``/``debug`` add DEBUG; ``quiet``
    keeps only errors) and, with ``log_file``, everything at DEBUG to that file."""
    level = logging.ERROR if quiet else logging.DEBUG if (verbose or debug) else logging.INFO
    return CLILogging(stream, level=level, log_file=log_file, tracebacks=debug)


# ---- formatting helpers -------------------------------------------------------------------------------


def human_size(n: int | None) -> str:
    """``2_150_000`` -> ``"2.1 MB"`` (decimal units, as people read file sizes)."""
    if n is None:
        return "unknown size"
    if n < 1000:
        return f"{n} B"
    for unit in ("kB", "MB", "GB"):
        n /= 1000
        if n < 1000 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"  # pragma: no cover


def count(n: int, noun: str, plural: str | None = None) -> str:
    """``count(3343, "port")`` -> ``"3,343 ports"``."""
    return f"{n:,} {noun if n == 1 else plural or noun + 's'}"


def seconds(value: float) -> str:
    return f"{value:.1f}s" if value >= 0.1 else f"{value * 1000:.0f}ms"


def header(resp: Any, name: str) -> str:
    """A response header, case-insensitively, or ``""``."""
    headers = getattr(resp, "headers", None) or {}
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        value = next((v for k, v in headers.items() if str(k).lower() == lowered), None)
    return str(value or "")


def response_size(resp: Any) -> int | None:
    length = header(resp, "Content-Length")
    if length.isdigit():
        return int(length)
    content = getattr(resp, "content", None)
    if isinstance(content, bytes | bytearray):
        return len(content)
    try:
        return len((resp.text or "").encode("utf-8"))
    except Exception:  # pragma: no cover - defensive
        return None


def log_http(
    logger: logging.Logger,
    service: str,
    method: str,
    path: str,
    status: int | str,
    elapsed: float,
    *,
    request_id: str = "",
    sent: int | None = None,
    received: int | None = None,
    note: str = "",
) -> None:
    """One DEBUG line per HTTP exchange: never headers or bodies, only their sizes."""
    sizes = []
    if sent:
        sizes.append(f"sent {human_size(sent)}")
    if received is not None:
        sizes.append(f"received {human_size(received)}")
    extra = "; ".join(filter(None, [", ".join(sizes), f"request id {request_id}" if request_id else "", note]))
    logger.debug(
        "%s %s %s -> %s in %s%s", service, method, path, status, seconds(elapsed), f" ({extra})" if extra else ""
    )

"""The import flow every front end shares: the CLI (``railyard-sync import …``) and the DCIM plugins' import jobs.

A snapshot becomes a saved Railyard estate in four steps, each here once:

1. :func:`plan_import` — fetch the estate being refreshed (with its revision), refuse to refresh it from another
   source or from a subset of its sites (:func:`check_same_source`), build the Railyard project from the snapshot
   and merge it into the estate (``merge``), giving an :class:`ImportPlan`: the document to save and the diff.
2. :func:`preflight` — check the document with Railyard's ``/api/validate``, returning the errors that should stop
   the save (by rack and device) and logging the warnings; what a save would accept is not held against it.
3. The caller decides: a dry run stops here; conflicts stop the save (:meth:`ImportPlan.conflicts`); a refresh
   that changes nothing but the import record is not saved (:attr:`ImportPlan.up_to_date`).
4. :func:`save` — ``PUT`` the document with ``If-Match`` (the revision it was merged into), so a change made in
   Railyard meanwhile is never overwritten.

Nothing here prints; progress goes to the ``railyard_sync`` loggers and to an optional ``on_step`` callback (the
CLI's account of how far a failed run got). Front ends word the results themselves.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..client import RailyardClient
from ..dcim.snapshot import Snapshot
from ..errors import (
    RailyardAPIError,
    RailyardBadRequestError,
    RailyardBusyError,
    RailyardConnectionError,
    RailyardNotFoundError,
    RailyardServerError,
    RailyardTooLargeError,
)
from ..log import count, seconds
from ..problems import ERROR, ProblemNamer, identity, listing, severity
from .merge import MergeDiff, merge

log = logging.getLogger(__name__)

#: How each source is named to people, and the prefix its ids carry.
SOURCE_NAMES = {"netbox": "NetBox", "nautobot": "Nautobot"}
PREFIXES = {"netbox": "nb", "nautobot": "nbt"}

#: Problems a save holds only an existing estate to (Railyard's WriteRuleProblems: a new document is not held to
#: the rack-name rules at all), so they do not stop a create.
NEW_DOCUMENT_EXEMPT = ("rack.name-",)


class ImportRefused(Exception):
    """The import can't go ahead as asked; the message says why and what to do instead. Nothing was saved."""


def source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source)


def new_project_id() -> str:
    return "ry-" + uuid.uuid4().hex[:20]


def _no_step(_: str) -> None:
    pass


# ---- the plan -----------------------------------------------------------------------------------------------


@dataclass
class ImportPlan:
    """What an import would save: the document, and how it came about."""

    project_id: str
    name: str
    existing: dict | None  # the estate as Railyard holds it (a refresh), or None (a new estate)
    revision: int | None  # the revision the merge is based on (a refresh)
    imported: dict  # the project built from the snapshot alone
    report: Any  # the builder's ImportReport
    document: dict  # what to save: ``imported`` for a new estate, the merge for a refresh
    diff: MergeDiff | None  # the merge's diff (a refresh)

    @property
    def refresh(self) -> bool:
        return self.existing is not None

    @property
    def conflicts(self) -> list:
        """Conflicts between Railyard's design and the DCIM the server would refuse; they stop a save."""
        return list(self.diff.conflicts) if self.diff is not None else []

    @property
    def up_to_date(self) -> bool:
        """Whether a refresh changes nothing but the import record (its time), so it need not be saved."""
        return self.existing is not None and same_design(self.existing, self.document)


def plan_import(
    client: RailyardClient,
    snapshot: Snapshot,
    *,
    project: str | None = None,
    name: str | None = None,
    build: Callable[..., Any],
    catalogue: Any = None,
    allow_deletes: bool = False,
    requested: list[str] | None = None,
    place_flag: str = "--site",
    source_url: str = "",
    new_id: Callable[[], str] = new_project_id,
    on_step: Callable[[str], None] = _no_step,
) -> ImportPlan:
    """Build the snapshot as a Railyard project and, for a refresh (``project``: an id or URL slug), merge it into
    the estate. A new estate (``name``) gets ``new_id()``.

    ``build(snapshot, project_id=, name=, catalogue=, prefix=)`` is :func:`railyard_sync.importer.build_project`
    (injected so a front end can wrap it). ``requested`` are the site or location references the user named,
    which :func:`check_same_source` accepts for the estate's recorded ones (``place_flag`` is how to name more on a
    command line; blank, the message just lists them).
    Raises :class:`ImportRefused` for a refresh from another source, another DCIM or a subset of its sites."""
    if (project is None) == (name is None):
        raise ValueError("pass either project (a refresh) or name (a new estate)")
    prefix = PREFIXES.get(snapshot.source, snapshot.source)
    existing: dict | None = None
    revision: int | None = None
    if project is not None:
        log.info("Fetching estate %s from Railyard…", project)
        started = time.monotonic()
        existing, revision = client.get_project_with_revision(project)
        project_id, name = str(existing["id"]), str(existing.get("name") or project)
        log.info(
            "Fetched %r (%s) at revision %d: %s, in %s",
            name,
            project_id,
            revision,
            doc_counts(existing),
            seconds(time.monotonic() - started),
        )
        on_step(f"fetched {name!r} ({project_id}) at revision {revision}")
        check_same_source(existing, snapshot, requested or [], place_flag=place_flag, source_url=source_url)
    else:
        project_id, name = new_id(), str(name).strip()

    log.info("Building the Railyard project…")
    started = time.monotonic()
    built = build(snapshot, project_id=project_id, name=name, catalogue=catalogue, prefix=prefix)
    imported = built.project
    log.info("Built %s in %s", doc_counts(imported), seconds(time.monotonic() - started))
    on_step(f"built the Railyard project: {doc_counts(imported)}")

    diff: MergeDiff | None = None
    document = imported
    if existing is not None:
        log.info("Merging into %r…", name)
        started = time.monotonic()
        result = merge(existing, imported, prefix=prefix, allow_deletes=allow_deletes)
        document, diff = result.project, result.diff
        merged = diff_counts(diff)
        log.info("Merged in %s: %s", seconds(time.monotonic() - started), merged)
        on_step(f"merged it into {name!r}: {merged}")
    return ImportPlan(
        project_id=project_id,
        name=name,
        existing=existing,
        revision=revision,
        imported=imported,
        report=built.report,
        document=document,
        diff=diff,
    )


def save(client: RailyardClient, plan: ImportPlan) -> int:
    """``PUT`` the plan's document, with ``If-Match`` for a refresh; the new revision. Railyard's refusals raise
    the client's typed errors (412: the estate changed meanwhile, 402: the plan's rack limit…)."""
    return client.put_project(plan.document, if_match=plan.revision)


# ---- same source --------------------------------------------------------------------------------------------


def sync_meta(doc: dict) -> dict:
    meta = doc.get("meta") or {}
    sync = meta.get("railyardSync") if isinstance(meta, dict) else None
    return sync if isinstance(sync, dict) else {}


def recorded_source(doc: dict) -> tuple[str, str, list[dict]]:
    """(kind, url, sites) an earlier import recorded in ``meta.railyardSync``, as far as it says; each site as
    ``{"id", "slug", "name"}`` (whichever it recorded)."""
    sync = sync_meta(doc)
    # The builder writes {"source": "netbox", "url": …, "sites": [{id, slug, name}]}; accept a nested
    # {"source": {"kind", "url", "sites"}} and bare slugs too.
    source = sync.get("source") if isinstance(sync.get("source"), dict) else sync
    kind = source.get("kind") or (sync.get("source") if isinstance(sync.get("source"), str) else "")
    sites = []
    for site in source.get("sites") or sync.get("sites") or []:
        record = dict(site) if isinstance(site, dict) else {"slug": str(site)}
        if any(record.get(k) for k in ("id", "slug", "name")):
            sites.append(record)
    return str(kind or ""), str(source.get("url") or ""), sites


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def check_same_source(
    existing: dict, snapshot: Snapshot, requested: list[str], *, place_flag: str = "--site", source_url: str = ""
) -> None:
    """Refuse to merge another DCIM into an estate (their ids would collide), or a subset of its sites (the others
    would all read as deleted). ``source_url`` stands in for a snapshot that does not record its URL."""
    kind, url, sites = recorded_source(existing)
    name = source_name(snapshot.source)
    now = snapshot.source_url or source_url
    if kind and kind != snapshot.source:
        raise ImportRefused(
            f"this estate was imported from {source_name(kind)}, not {name}; it cannot be refreshed from it"
        )
    if url and now and _norm_url(url) != _norm_url(now):
        raise ImportRefused(
            f"this estate was imported from {url}, not {now}: ids from two {name} instances would "
            "collide. Import into a new estate with --name instead."
        )
    have_ids = {s.id for s in snapshot.sites}
    have_refs = {s.slug for s in snapshot.sites} | {s.name for s in snapshot.sites} | set(requested)
    missing = [
        s
        for s in sites
        if not (s.get("id") and str(s["id"]) in have_ids) and not ({s.get("slug"), s.get("name")} & have_refs)
    ]
    if missing:
        shown = [str(s.get("name") if snapshot.source == "nautobot" else s.get("slug")) or s.get("id") for s in sites]
        listed = ", ".join(str(s) for s in shown)
        flags = " ".join(f"{place_flag} {_quote(str(s))}" for s in shown) if place_flag else listed
        what = "location(s)" if snapshot.source == "nautobot" else "site(s)"
        raise ImportRefused(
            f"this estate was imported from {what} {listed}; import all of them again ({flags}) so the objects of "
            "the others are not reported as deleted."
        )


def _quote(text: str) -> str:
    return f"'{text}'" if any(c.isspace() for c in text) else text


def same_design(existing: dict, document: dict) -> bool:
    """Whether two documents differ only in the import record (``meta.railyardSync``)."""

    def strip(doc: dict) -> dict:
        doc = dict(doc)
        meta = dict(doc.get("meta") or {})
        meta.pop("railyardSync", None)
        doc["meta"] = meta
        return doc

    return strip(existing) == strip(document)


# ---- preflight ----------------------------------------------------------------------------------------------


def preflight(
    client: RailyardClient,
    document: dict,
    existing: dict | None,
    *,
    validate: bool = True,
    on_step: Callable[[str], None] = _no_step,
) -> list[str]:
    """Check the document with Railyard's ``/api/validate`` and return the errors that should stop the save, as
    lines naming racks and devices ([] to go ahead). Warnings are logged, not returned.

    /api/validate checks a standalone document, so what a save would accept is not held against the import: for a
    new estate the rack-name rules (a create is not held to them); for a refresh any error the estate already had
    (a save compares with the stored document and keeps a legacy finding), and a refusal to load it at all (an
    older estate may hold values only a new document is refused for). A Railyard that cannot check (no endpoint,
    too large, busy, failing) is a warning: the save itself is the authority."""
    if not validate:
        log.info("Skipping Railyard's check of the document (--no-validate).")
        on_step("skipped Railyard's check (--no-validate)")
        return []
    log.info("Checking the document with Railyard…")
    started = time.monotonic()
    namer = ProblemNamer(document)
    try:
        result = client.validate(document)
    except RailyardNotFoundError:
        log.warning("this Railyard cannot check documents (no /api/validate); saving without the check.")
        return []
    except RailyardTooLargeError as e:
        log.warning("the document is larger than Railyard's check accepts (%s); saving without the check.", e.message)
        return []
    except RailyardBadRequestError as e:
        said = e.server_message or e.message
        if existing is not None:
            log.warning(
                "Railyard's check could not load the document on its own (%s). An estate saved before Railyard's "
                "current limits can hold values only a new document is refused for, and the save checks against the "
                "stored estate, so carrying on.",
                said,
            )
            return []
        return [f"Railyard cannot load the document: {said}", *listing(e.problems, namer)]
    except (RailyardBusyError, RailyardServerError, RailyardConnectionError) as e:
        log.warning("Railyard's check failed (%s); saving without it.", e.message)
        return []

    problems = result["problems"]
    errors = [p for p in problems if severity(p) == ERROR]
    warnings = [p for p in problems if severity(p) != ERROR]
    exempt: list[dict]
    if existing is None:
        exempt = [p for p in errors if str(p.get("code") or "").startswith(NEW_DOCUMENT_EXEMPT)]
    elif errors:
        known = _known_problems(client, existing)
        exempt = [p for p in errors if identity(p) in known]
    else:
        exempt = []
    errors = [p for p in errors if p not in exempt]
    more = " (Railyard listed only the first ones)" if result.get("truncated") else ""
    why = (
        "a new estate is not held to these rules"
        if existing is None
        else "the estate already had them before this import, and Railyard keeps saving them"
    )
    _log_problems(f"{count(len(exempt), 'error')} that do not stop the import ({why})", exempt, namer)
    _log_problems(f"{count(len(warnings), 'warning')}{more}; they do not stop the import", warnings, namer)
    log.info(
        "Railyard's check found %s and %s in %s",
        count(len(errors), "error"),
        count(len(warnings), "warning"),
        seconds(time.monotonic() - started),
    )
    tally = f"{count(len(errors), 'error')}, {count(len(warnings), 'warning')}"
    on_step(f"checked it with Railyard: {tally}" + (f" ({len(exempt)} more that do not stop it)" if exempt else ""))
    return listing(errors, namer) + ([more.strip(" ()").capitalize() + "."] if errors and more else [])


def _log_problems(title: str, problems: list[dict], namer: ProblemNamer, shown: int = 10) -> None:
    """Up to ``shown`` problems as one warning (the rest at debug level, for -v and the log file)."""
    if not problems:
        return
    lines = "\n".join(f"  - {line}" for line in listing(problems, namer, limit=shown))
    if len(problems) > shown:
        lines += " (-v lists them all)"
    log.warning("Railyard's check found %s:\n%s", title, lines)
    if len(problems) > shown:
        everything = "\n".join(f"  - {line}" for line in listing(problems, namer, limit=len(problems)))
        log.debug("All of them:\n%s", everything)


def _known_problems(client: RailyardClient, existing: dict) -> set[tuple[str, str, str, str]]:
    """The findings the estate already had before this import (none when it cannot be checked)."""
    try:
        return {identity(p) for p in client.validate(existing)["problems"]}
    except RailyardAPIError as e:
        log.debug("Could not check the estate as it was: %s", e.message)
        return set()


def preflight_failure(problems: list[str], *, dry_run: bool, source: str = "netbox", hint: str = "") -> str:
    """The problems :func:`preflight` returned, as the message that stops (or, for a dry run, would stop) a save.
    ``hint`` is how to skip the check in this front end (the CLI's ``--no-validate``)."""
    errors = len([p for p in problems if not p.startswith("…")])
    head = (
        f"Railyard's check found {errors} problem(s) a real import would stop for:"
        if dry_run
        else f"Railyard's check found {errors} problem(s) in the document, so it was not saved:"
    )
    tail = f"Fix them in {source_name(source)} (or in Railyard, for objects designed there) and import again"
    tail += f", or {hint}." if hint else "."
    return "\n  ".join([head, *(f"- {line}" for line in problems), tail])


# ---- counts, for reports ------------------------------------------------------------------------------------


def document_counts(doc: dict) -> dict[str, int]:
    racks = doc.get("racks") or []
    return {
        "racks": len(racks),
        "devices": sum(len(r.get("placements") or []) for r in racks),
        "cables": len(doc.get("cables") or []),
        "power links": len(doc.get("powerLinks") or []),
    }


def doc_counts(doc: dict) -> str:
    """``3 racks, 12 devices, 40 cables, 6 power links``."""
    return ", ".join(count(n, what[:-1], what) for what, n in document_counts(doc).items())


def diff_counts(diff: MergeDiff) -> str:
    """``12 added, 3 updated, 1 stale, 0 removed`` (and the conflicts, when any)."""
    totals = {word: 0 for word in ("added", "updated", "stale", "removed")}
    for kind in diff.kinds.values():
        for word in totals:
            totals[word] += len(getattr(kind, word))
    text = ", ".join(f"{n:,} {word}" for word, n in totals.items())
    return text + (f", {count(len(diff.conflicts), 'conflict')}" if diff.conflicts else "")


def snapshot_counts(snapshot: Snapshot) -> str:
    """``47 racks, 86 devices, 3,343 ports, 53 cables`` (ports: interfaces, front, rear and console ports)."""
    ports = sum(1 for c in snapshot.components if c.kind in ("interface", "front-port", "rear-port", "console-port"))
    return ", ".join(
        [
            count(len(snapshot.racks), "rack"),
            count(len(snapshot.devices), "device"),
            count(ports, "port"),
            count(len(snapshot.cables), "cable"),
        ]
    )


def report_text(report: Any) -> str:
    """The builder's report as text: its summary_lines() (an ImportReport) or summary() when it has one."""
    if report is None:
        return ""
    lines = getattr(report, "summary_lines", None)
    if callable(lines):
        return "\n".join(str(line) for line in lines()).strip()
    summary = getattr(report, "summary", None)
    text = summary() if callable(summary) else str(report)
    return str(text).strip()


def import_summary(snapshot: Snapshot, imported: dict, report: Any) -> str:
    """``Read NetBox (ldn1): 3 racks, 12 devices, …`` and the builder's report."""
    sites = ", ".join(s.name if snapshot.source == "nautobot" else s.slug for s in snapshot.sites) or "no sites"
    counts = ", ".join(f"{n} {what if n != 1 else what[:-1]}" for what, n in document_counts(imported).items())
    lines = [f"Read {source_name(snapshot.source)} ({sites}): {counts}."]
    text = report_text(report)
    if text:
        lines.append(text)
    return "\n".join(lines)

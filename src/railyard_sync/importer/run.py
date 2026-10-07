"""``ImportRun`` — import a DCIM snapshot into Railyard: a new estate, or a refresh of one as a baseline.

The steps the ``railyard-sync import`` command and the NetBox plugin's import job share. Each caller reports
between them in its own way (the CLI prints, the plugin writes its job log), so they are separate methods:

1. :meth:`ImportRun.fetch` — a refresh only: the estate and the revision it is at. Refuses an estate imported
   from another DCIM or another URL (their ids would collide) or from sites this import leaves out (their
   objects would all read as deleted).
2. :meth:`ImportRun.build` — the Railyard project for the snapshot (:func:`~railyard_sync.importer.build_project`,
   device types matched against Railyard's catalogue).
3. :meth:`ImportRun.merge` — a refresh only: merge it into the estate (:func:`~railyard_sync.importer.merge`).
4. :meth:`ImportRun.check` — Railyard's ``/api/validate``: the errors that must stop the save. What a save would
   accept is not held against the import (see the method).
5. :meth:`ImportRun.save` — ``PUT`` the document; a refresh names the revision it replaces (``If-Match``), so a
   change made in Railyard meanwhile is never overwritten.

:meth:`ImportRun.run` does all of it with the CLI's rules: a dry run checks and stops; conflicts or check errors
stop the save; a refresh that changes nothing but the import record is not saved.

Progress goes to the ``step`` callback (one line per finished step, the CLI's "Before the failure…" list) and to
this module's logger. Nothing here prints. A refusal the caller should show as it is raises
:class:`ImportRefused`; Railyard's own errors (a 402 plan limit, a 401 token…) propagate as the client raised
them.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..client import RailyardClient
from ..dcim.snapshot import Snapshot
from ..errors import (
    RailyardAPIError,
    RailyardBadRequestError,
    RailyardBusyError,
    RailyardConflictError,
    RailyardConnectionError,
    RailyardNotFoundError,
    RailyardPreconditionError,
    RailyardServerError,
    RailyardTooLargeError,
)
from ..log import count, seconds
from ..problems import ERROR, ProblemNamer, identity, listing, severity
from .build import BuildResult
from .merge import MergeDiff

log = logging.getLogger(__name__)

#: Problems a save holds only an existing estate to (Railyard's WriteRuleProblems: a new document is not held
#: to the rack-name rules at all), so they do not stop a create.
NEW_DOCUMENT_EXEMPT = ("rack.name-",)

SOURCE_NAMES = {"netbox": "NetBox", "nautobot": "Nautobot"}

#: ``ImportRun(catalogue=…)`` when none is given: Railyard's own catalogue, through the client.
RAILYARD_CATALOGUE: Any = object()


class ImportRefused(Exception):
    """The import must not go ahead, for a reason to show as it is (conflicts, failed checks, another source,
    a refresh Railyard refused). Nothing was saved."""


def new_project_id() -> str:
    """A new estate's id: ``ry-`` and 20 hex digits."""
    return "ry-" + uuid.uuid4().hex[:20]


def _default_build(snapshot: Snapshot, *, project_id: str, name: str, catalogue: Any, prefix: str) -> BuildResult:
    from .build import build_project

    return build_project(snapshot, project_id=project_id, name=name, catalogue=catalogue, prefix=prefix)


def _default_catalogue(client: RailyardClient) -> Any:
    from .catalogue import RailyardCatalogue

    return RailyardCatalogue(client.get_json)


@dataclass
class ImportOutcome:
    """What :meth:`ImportRun.run` did."""

    status: str  # "dry-run", "saved", "up-to-date"
    project_id: str
    name: str
    refreshed: bool
    revision: int | None = None  # the revision written (saved) or fetched (otherwise)
    problems: list[str] = field(default_factory=list)  # Railyard's check: errors that stop a save


class ImportRun:
    """One import of ``snapshot`` into Railyard through ``client``: a new estate called ``name``, or a refresh
    of the estate ``project`` (an id or URL slug). Exactly one of them is given.

    ``prefix`` starts every derived id (``nb`` for NetBox, ``nbt`` for Nautobot) and must be the same on every
    import of an estate. ``source_url`` is the DCIM the snapshot came from when the snapshot does not say,
    and ``requested_sites`` the sites asked for (slugs), both only for the same-source check of a refresh.
    ``catalogue`` (``None``: build every device type from the snapshot), ``build`` and ``project_id`` replace
    the Railyard catalogue lookup, the builder and the new estate's id (the CLI's test seams). ``step`` is called
    with a line for each finished step, and ``name_taken_hint`` says what to do when the new estate's name is
    taken (worded for the caller's form or flags).
    """

    def __init__(
        self,
        client: RailyardClient,
        snapshot: Snapshot,
        *,
        project: str | None = None,
        name: str | None = None,
        prefix: str = "nb",
        allow_deletes: bool = False,
        source_url: str = "",
        requested_sites: Iterable[str] = (),
        catalogue: Any = RAILYARD_CATALOGUE,
        build: Callable[..., BuildResult] | None = None,
        project_id: str | None = None,
        step: Callable[[str], None] | None = None,
        name_taken_hint: str = "refresh it with --project, or choose another --name",
    ) -> None:
        project = (project or "").strip() or None
        name = (name or "").strip() or None
        if (project is None) == (name is None):
            raise ValueError("give either the estate to refresh or the name of a new estate, not both")
        self.client = client
        self.snapshot = snapshot
        self.project_ref = project
        self.prefix = prefix
        self.allow_deletes = allow_deletes
        self.source_url = source_url
        self.requested_sites = list(requested_sites)
        self._catalogue = catalogue
        self._build = build or _default_build
        self._step = step or (lambda line: None)
        self._name_taken_hint = name_taken_hint

        self.project_id = "" if project else (project_id or new_project_id())
        self.name = name or ""
        self.existing: dict | None = None
        self.revision: int | None = None
        self.built: BuildResult | None = None
        self.document: dict | None = None
        self.diff: MergeDiff | None = None

    @property
    def refreshing(self) -> bool:
        return self.project_ref is not None

    # -- 1. fetch ---------------------------------------------------------------------------------------

    def fetch(self) -> dict | None:
        """A refresh: the estate as Railyard holds it, and its revision (``None`` for a new estate)."""
        if not self.refreshing:
            return None
        log.info("Fetching estate %s from Railyard…", self.project_ref)
        started = time.monotonic()
        self.existing, self.revision = self.client.get_project_with_revision(self.project_ref)
        self.project_id = str(self.existing["id"])
        self.name = str(self.existing.get("name") or self.project_ref)
        log.info(
            "Fetched %r (%s) at revision %d: %s, in %s",
            self.name,
            self.project_id,
            self.revision,
            doc_counts(self.existing),
            seconds(time.monotonic() - started),
        )
        self._step(f"fetched {self.name!r} ({self.project_id}) at revision {self.revision}")
        check_same_source(self.existing, self.snapshot, self.source_url, self.requested_sites)
        return self.existing

    # -- 2. build ---------------------------------------------------------------------------------------

    def build(self) -> BuildResult:
        """The Railyard project for the snapshot (``built.project``) and what the build left out (``built.report``)."""
        if self.refreshing and self.existing is None:
            self.fetch()
        log.info("Building the Railyard project…")
        started = time.monotonic()
        catalogue = _default_catalogue(self.client) if self._catalogue is RAILYARD_CATALOGUE else self._catalogue
        self.built = self._build(
            self.snapshot, project_id=self.project_id, name=self.name, catalogue=catalogue, prefix=self.prefix
        )
        imported = self.built.project
        log.info("Built %s in %s", doc_counts(imported), seconds(time.monotonic() - started))
        self._step(f"built the Railyard project: {doc_counts(imported)}")
        self.document = imported
        return self.built

    # -- 3. merge ---------------------------------------------------------------------------------------

    def merge(self) -> MergeDiff | None:
        """A refresh: merge the build into the estate (the document to save becomes the merged one) and return
        what changed; ``None`` for a new estate."""
        from .merge import merge

        if self.built is None:
            self.build()
        if self.existing is None:
            return None
        log.info("Merging into %r…", self.name)
        started = time.monotonic()
        result = merge(self.existing, self.built.project, prefix=self.prefix, allow_deletes=self.allow_deletes)
        self.document, self.diff = result.project, result.diff
        merged = diff_counts(self.diff)
        log.info("Merged in %s: %s", seconds(time.monotonic() - started), merged)
        self._step(f"merged it into {self.name!r}: {merged}")
        return self.diff

    @property
    def conflicts(self) -> list:
        return list(self.diff.conflicts) if self.diff is not None else []

    def up_to_date(self) -> bool:
        """A refresh that changes nothing but the import record (its time), so it need not be saved."""
        return self.existing is not None and self.document is not None and same_design(self.existing, self.document)

    # -- 4. check ---------------------------------------------------------------------------------------

    def check(self, *, validate: bool = True) -> list[str]:
        """Check the document with Railyard's ``/api/validate`` and return the errors that should stop the save,
        as lines naming racks and devices ([] to go ahead). Warnings are logged, not returned.

        /api/validate checks a standalone document, so what a save would accept is not held against the import:
        for a new estate the rack-name rules (a create is not held to them); for a refresh any error the estate
        already had (a save compares with the stored document and keeps a legacy finding), and a refusal to load
        it at all (an older estate may hold values only a new document is refused for). A Railyard that cannot
        check (no endpoint, too large, busy, failing) is a warning: the save itself is the authority."""
        if self.document is None:
            self.merge()
        if not validate:
            log.info("Skipping Railyard's check of the document (--no-validate).")
            self._step("skipped Railyard's check (--no-validate)")
            return []
        document, existing = self.document, self.existing
        log.info("Checking the document with Railyard…")
        started = time.monotonic()
        namer = ProblemNamer(document)
        try:
            result = self.client.validate(document)
        except RailyardNotFoundError:
            log.warning("this Railyard cannot check documents (no /api/validate); saving without the check.")
            return []
        except RailyardTooLargeError as e:
            log.warning(
                "the document is larger than Railyard's check accepts (%s); saving without the check.", e.message
            )
            return []
        except RailyardBadRequestError as e:
            said = e.server_message or e.message
            if existing is not None:
                log.warning(
                    "Railyard's check could not load the document on its own (%s). An estate saved before Railyard's "
                    "current limits can hold values only a new document is refused for, and the save checks against "
                    "the stored estate, so carrying on.",
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
            known = self._known_problems(existing)
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
        self._step(
            f"checked it with Railyard: {tally}" + (f" ({len(exempt)} more that do not stop it)" if exempt else "")
        )
        return listing(errors, namer) + ([more.strip(" ()").capitalize() + "."] if errors and more else [])

    def _known_problems(self, existing: dict) -> set[tuple[str, str, str, str]]:
        """The findings the estate already had before this import (none when it cannot be checked)."""
        try:
            return {identity(p) for p in self.client.validate(existing)["problems"]}
        except RailyardAPIError as e:
            log.debug("Could not check the estate as it was: %s", e.message)
            return set()

    # -- 5. save ----------------------------------------------------------------------------------------

    def save(self, *, on_refused: Callable[[dict, RailyardAPIError], list[str]] | None = None) -> int:
        """``PUT`` the document (with ``If-Match`` on a refresh) and return the revision written.

        When Railyard refuses it, ``on_refused(document, error)`` may return lines to add to the error (the CLI
        keeps the document that was sent in a file and says where). An estate that changed meanwhile (412) or a
        taken name (409 ``name_taken``) raises :class:`ImportRefused` saying what to do; anything else is
        Railyard's error, re-raised with those lines as hints."""
        if self.document is None:
            raise ValueError("nothing to save: build (and, for a refresh, merge) first")
        try:
            return self.client.put_project(self.document, if_match=self.revision)
        except RailyardAPIError as e:
            kept = on_refused(self.document, e) if on_refused is not None else []
            if isinstance(e, RailyardPreconditionError) and e.status == 412:
                head = (
                    f"{self.name!r} changed in Railyard while the import ran, so it was not overwritten. Run the "
                    "import again: it merges onto the latest revision."
                )
            elif isinstance(e, RailyardConflictError) and e.code == "name_taken":
                head = f"an estate named {self.name!r} already exists in this organisation: {self._name_taken_hint}."
            else:
                e.hints += kept
                raise
            rid = [f"Quote request id {e.request_id} when reporting this."] if e.request_id else []
            raise ImportRefused("\n  ".join([head, *kept, *rid])) from None

    # -- all of it --------------------------------------------------------------------------------------

    def run(
        self,
        *,
        dry_run: bool,
        validate: bool = True,
        on_refused: Callable[[dict, RailyardAPIError], list[str]] | None = None,
    ) -> ImportOutcome:
        """Fetch, build, merge and check; then, unless ``dry_run``, save with the CLI's rules: conflicts and
        check errors raise :class:`ImportRefused` (nothing saved), a refresh that changes nothing is not saved.
        A dry run returns the check's errors in ``problems`` instead of raising."""
        self.fetch()
        self.build()
        self.merge()
        outcome = ImportOutcome(
            status="dry-run",
            project_id=self.project_id,
            name=self.name,
            refreshed=self.refreshing,
            revision=self.revision,
        )
        if dry_run:
            outcome.problems = self.check(validate=validate)
            return outcome
        if self.conflicts:
            raise ImportRefused(conflicts_message(len(self.conflicts)))
        if self.up_to_date():
            outcome.status = "up-to-date"
            return outcome
        problems = self.check(validate=validate)
        if problems:
            outcome.problems = problems
            raise ImportRefused(check_failure(problems, dry_run=False))
        outcome.revision = self.save(on_refused=on_refused)
        outcome.status = "saved"
        return outcome


# ---- shared wording and checks -----------------------------------------------------------------------


def conflicts_message(n: int) -> str:
    return (
        f"{n} conflict(s) between Railyard's design and NetBox (listed above); resolve them in Railyard, then import "
        "again. Nothing was saved."
    )


def check_failure(problems: list[str], *, dry_run: bool, fix: str = "") -> str:
    """The message for Railyard's check refusing the document: the problems, then what to do."""
    errors = len([p for p in problems if not p.startswith("…")])
    head = (
        f"Railyard's check found {errors} problem(s) a real import would stop for:"
        if dry_run
        else f"Railyard's check found {errors} problem(s) in the document, so it was not saved:"
    )
    fix = fix or (
        "Fix them in NetBox (or in Railyard, for objects designed there) and import again, or pass --no-validate "
        "to let Railyard's save decide."
    )
    return "\n  ".join([head, *(f"- {line}" for line in problems), fix])


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


def sync_meta(doc: dict) -> dict:
    meta = doc.get("meta") or {}
    sync = meta.get("railyardSync") if isinstance(meta, dict) else None
    return sync if isinstance(sync, dict) else {}


def recorded_source(doc: dict) -> tuple[str, str, list[str]]:
    """(kind, url, site slugs) an earlier import recorded in meta.railyardSync, as far as it says."""
    sync = sync_meta(doc)
    # The builder writes {"source": "netbox", "url": …, "sites": […]}; accept a nested
    # {"source": {"kind", "url", "sites"}} too.
    source = sync.get("source") if isinstance(sync.get("source"), dict) else sync
    kind = source.get("kind") or (sync.get("source") if isinstance(sync.get("source"), str) else "")
    sites = source.get("sites") or sync.get("sites") or []
    slugs = [str(s.get("slug") or s.get("name") or "") if isinstance(s, dict) else str(s) for s in sites]
    return str(kind or ""), str(source.get("url") or ""), [s for s in slugs if s]


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def check_same_source(existing: dict, snapshot: Snapshot, source_url: str = "", requested: Iterable[str] = ()) -> None:
    """Refuse to merge another DCIM into an estate (their ids would collide), or a subset of its sites (the
    others would all read as deleted)."""
    kind, url, sites = recorded_source(existing)
    if kind and kind != snapshot.source:
        raise ImportRefused(
            f"this estate was imported from {kind}, not {snapshot.source}; it cannot be refreshed from it"
        )
    source_url = snapshot.source_url or source_url or ""
    if url and source_url and _norm_url(url) != _norm_url(source_url):
        raise ImportRefused(
            f"this estate was imported from {url}, not {source_url}: ids from two NetBox instances would collide. "
            "Import into a new estate with --name instead."
        )
    asked = {s.slug for s in snapshot.sites} | set(requested)
    missing = [s for s in sites if s not in asked]
    if missing:
        flags = " ".join(f"--site {s}" for s in sites)
        raise ImportRefused(
            f"this estate was imported from site(s) {', '.join(sites)}; import all of them again ({flags}) so the "
            "objects of the others are not reported as deleted."
        )


def same_design(existing: dict, document: dict) -> bool:
    """Whether a refresh changes nothing but the import record (its time), so it need not be saved."""

    def strip(doc: dict) -> dict:
        doc = dict(doc)
        meta = dict(doc.get("meta") or {})
        meta.pop("railyardSync", None)
        doc["meta"] = meta
        return doc

    return strip(existing) == strip(document)


def doc_count(doc: dict) -> dict[str, int]:
    racks = doc.get("racks") or []
    return {
        "racks": len(racks),
        "devices": sum(len(r.get("placements") or []) for r in racks),
        "cables": len(doc.get("cables") or []),
        "power links": len(doc.get("powerLinks") or []),
    }


def doc_counts(doc: dict) -> str:
    return ", ".join(count(n, what[:-1], what) for what, n in doc_count(doc).items())


def diff_counts(diff: MergeDiff) -> str:
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


def import_summary(snapshot: Snapshot, imported: dict, report: Any) -> str:
    """``Read NetBox (ldn1): 4 racks, 12 devices, …`` and the builder's report."""
    sites = ", ".join(s.slug for s in snapshot.sites) or "no sites"
    counts = ", ".join(f"{n} {what if n != 1 else what[:-1]}" for what, n in doc_count(imported).items())
    source = SOURCE_NAMES.get(snapshot.source, snapshot.source)
    lines = [f"Read {source} ({sites}): {counts}."]
    text = report_text(report)
    if text:
        lines.append(text)
    return "\n".join(lines)


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

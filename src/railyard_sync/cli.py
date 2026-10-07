"""The ``railyard-sync`` command line.

::

    railyard-sync import netbox --netbox-url URL (--site SLUG [--site SLUG …] | --all-sites)
        [--railyard-url URL] --org ORG (--project REF | --name NAME)
        [--dry-run] [--allow-deletes] [--out FILE] [--snapshot-out FILE] [--from-snapshot FILE]
        [--insecure] [--name-version] [--no-validate] [--save-document FILE] [--failed-dir DIR]
        [-v | -q] [--debug] [--log-file FILE]

    railyard-sync import nautobot --nautobot-url URL (--location NAME [--location NAME …] | --all-locations)
        … the same Railyard and run options as import netbox

    railyard-sync export netbox [--railyard-url URL] --org ORG --project REF [--change-request ID]
        --netbox-url URL [--netbox-version X.Y] [--apply] [--allow-deletes] [--import-components]
        [--insecure] [--json] [-v | -q] [--debug] [--log-file FILE]

    railyard-sync export nautobot [--railyard-url URL] --org ORG --project REF [--change-request ID]
        --nautobot-url URL [--apply] [--allow-deletes] [--insecure] [--json] [-v | -q] [--debug] [--log-file FILE]

**Import** reads the NetBox sites or Nautobot locations into a :class:`~railyard_sync.dcim.snapshot.Snapshot` (or
replays one saved with ``--snapshot-out``), builds a Railyard project from it (device types matched against
Railyard's catalogue), and then either creates a new estate (``--name``) or refreshes an existing one
(``--project``) by merging the import into it, saving it with ``If-Match`` so a change made in Railyard meanwhile is
never overwritten. The steps are :mod:`railyard_sync.importer.flow`'s, which the DCIM plugins' import jobs share.
``--dry-run`` shows what would happen and saves nothing.

**Export** fetches the estate's sync document from Railyard (a paid deliverable on hosted Railyard; a self-hosted
server with billing off allows it) — for NetBox, for the release it reads from ``/api/status/`` (or
``--netbox-version``) — and syncs it with :func:`railyard_sync.export.run.sync_to_netbox` or
:func:`~railyard_sync.export.run.sync_to_nautobot`, under the ownership tag keyed by ``--railyard-url`` and the
project id. It is a dry run that prints the planned changes unless ``--apply`` is given.

Before a save, the document is checked with Railyard's ``/api/validate`` (``--no-validate`` skips it): errors
stop the import, listed by rack and device, and warnings are reported. A save Railyard refuses keeps the
document that was sent in ``railyard-sync-failed-<project id>-<UTC time>.json`` (``--failed-dir``), readable
only by its owner, for the report; ``--save-document`` keeps it on success too.

stdout carries the results (summaries, the diff, the export report, ``--json``); stderr carries progress lines,
warnings and errors (``-v`` adds every HTTP request with its status, time, size and request id; ``-q`` keeps
only errors; ``--debug`` adds tracebacks), and ``--log-file`` writes everything at debug level to a file. No
log ever holds a token, an Authorization header or a request body.

Tokens come from the environment only (``NETBOX_TOKEN``, ``NAUTOBOT_TOKEN``, ``RAILYARD_TOKEN``): a token on the
command line would sit in shell history and the process list, so one is refused.

Exit codes: 0 success, 1 error (an export with errors or conflicts too), 2 usage, 3 refused by the
Railyard plan (its rack limit, or a deliverable the plan does not include).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from . import __version__
from .client import RailyardClient
from .dcim.snapshot import Snapshot
from .dcim_http import APIError
from .errors import (
    RailyardAPIError,
    RailyardConflictError,
    RailyardPlanError,
    RailyardPlanLimitError,
    RailyardPlanRequiredError,
    RailyardPreconditionError,
)
from .export import nautobot_rest, netbox_rest
from .export.netbox_rest import MIN_VERSION, NetBoxClient, NetBoxVersionError, parse_version
from .export.policy import DEFAULT_RAILYARD_URL
from .export.run import NautobotSyncResult, SyncOutcome, SyncRefused, sync_to_nautobot, sync_to_netbox
from .importer import flow
from .importer.flow import ImportPlan, ImportRefused
from .log import FILE_ONLY, CLILogging, configure_cli, count, human_size, seconds

log = logging.getLogger(__name__)

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PLAN = 0, 1, 2, 3
NETBOX_TOKEN_ENV = "NETBOX_TOKEN"
NAUTOBOT_TOKEN_ENV = "NAUTOBOT_TOKEN"
RAILYARD_TOKEN_ENV = "RAILYARD_TOKEN"
NETBOX_PREFIX = "nb"
NAUTOBOT_PREFIX = "nbt"

SOURCE_NAMES = flow.SOURCE_NAMES

# Display names for the plan ids Railyard reports (backend/internal/plans/catalogue.json).
PLAN_NAMES = {
    "community": "Community",
    "project-pass": "Project Pass",
    "pro": "Pro",
    "team": "Team",
    "partner": "Partner",
    "self-hosted": "Self-hosted",
    "enterprise": "Enterprise",
}


@dataclass(frozen=True)
class DCIM:
    """What the commands say and ask for one DCIM."""

    key: str  # "netbox": the subcommand and the snapshot's source
    name: str  # "NetBox"
    token_env: str
    place: str  # what an import is built around: "site" / "location"
    place_help: str
    all_help: str

    @property
    def url_flag(self) -> str:
        return f"--{self.key}-url"

    @property
    def place_flag(self) -> str:
        return f"--{self.place}"

    @property
    def all_flag(self) -> str:
        return f"--all-{self.place}s"


NETBOX = DCIM(
    key="netbox",
    name="NetBox",
    token_env=NETBOX_TOKEN_ENV,
    place="site",
    place_help="site slug; repeat for more",
    all_help="every site the NetBox token can see, into one estate",
)
NAUTOBOT = DCIM(
    key="nautobot",
    name="Nautobot",
    token_env=NAUTOBOT_TOKEN_ENV,
    place="location",
    place_help="the location to import (a name or UUID; Nautobot has no sites); repeat for more",
    all_help="every top data-centre location the Nautobot token can see (the first location type down the tree "
    "that may hold racks or devices), into one estate",
)
DCIMS = {d.key: d for d in (NETBOX, NAUTOBOT)}


class UsageError(Exception):
    """A command-line mistake: reported with exit code 2."""


class CommandError(Exception):
    """A failure with a message for the user: reported with exit code 1."""


class Progress:
    """The steps a run has finished, so a failure can say how far it got."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.save_attempted = False

    def done(self, step: str) -> None:
        self.steps.append(step)

    def report(self, command: str | None, error: BaseException | None = None) -> list[str]:
        if not self.steps:
            return []
        lines = ["Before the failure, railyard-sync had:", *(f"  - {step}" for step in self.steps)]
        if command == "import":
            # A refusal (4xx) saved nothing; a failure (5xx, no response) may not say.
            status = getattr(error, "status", None)
            refused = isinstance(status, int) and 400 <= status < 500
            uncertain = self.save_attempted and not refused and not isinstance(error, CommandError | ImportRefused)
            lines.append("The save did not complete (see above)." if uncertain else "Nothing was saved to Railyard.")
        return lines


# ---- seams to the loaders and the builder (imported lazily; the tests replace them) -------------------------


def load_netbox_snapshot(url: str, token: str, sites: list[str] | None, *, verify: bool = True) -> Snapshot:
    from .dcim.netbox import load_netbox_snapshot as load

    return load(url, token, sites, verify=verify)


def load_nautobot_snapshot(url: str, token: str, locations: list[str] | None, *, verify: bool = True) -> Snapshot:
    from .dcim.nautobot import load_nautobot_snapshot as load

    return load(url, token, locations, verify=verify)


def make_catalogue(client: RailyardClient) -> Any:
    from .importer import RailyardCatalogue

    return RailyardCatalogue(client.get_json)


def build_project(snapshot: Snapshot, *, project_id: str, name: str, catalogue: Any, prefix: str) -> Any:
    from .importer import build_project as build

    return build(snapshot, project_id=project_id, name=name, catalogue=catalogue, prefix=prefix)


def dcim_errors() -> tuple[type[BaseException], ...]:
    try:
        from .dcim.errors import DCIMError
    except ImportError:  # pragma: no cover - the loader ships with this package
        return ()
    return (DCIMError,)


def new_project_id() -> str:
    return "ry-" + uuid.uuid4().hex[:20]


def today() -> str:
    return datetime.now(UTC).date().isoformat()


# ---- arguments (shared by import and export) ----------------------------------------------------------

NETBOX_VERSION = re.compile(r"v?(\d+)\.(\d+)(\.\d+)?")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="railyard-sync",
        description="Sync Railyard estates with a DCIM. Tokens are read from the environment "
        f"({NETBOX_TOKEN_ENV}, {NAUTOBOT_TOKEN_ENV}, {RAILYARD_TOKEN_ENV}), never from the command line.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    imp = commands.add_parser("import", help="import DCIM sites or locations into a Railyard estate")
    sources = imp.add_subparsers(dest="source", metavar="SOURCE", required=True)
    for dcim in DCIMS.values():
        _add_import(sources, dcim)
    exp = commands.add_parser("export", help="export a Railyard estate into a DCIM")
    targets = exp.add_subparsers(dest="target", metavar="TARGET", required=True)
    for dcim in DCIMS.values():
        _add_export(targets, dcim)
    return parser


def _add_dcim_url(group: Any, dcim: DCIM, *, required: bool, what: str) -> None:
    help_ = f"{dcim.name} base URL (token from ${dcim.token_env}: {what})"
    group.add_argument(dcim.url_flag, dest="dcim_url", metavar="URL", required=required, help=help_)
    group.add_argument("--insecure", action="store_true", help=f"do not verify {dcim.name}'s TLS certificate")


def _add_railyard(group: Any) -> None:
    group.add_argument(
        "--railyard-url",
        metavar="URL",
        default=DEFAULT_RAILYARD_URL,
        help=f"Railyard URL (default {DEFAULT_RAILYARD_URL}; token from ${RAILYARD_TOKEN_ENV})",
    )
    group.add_argument("--org", required=True, help="organisation id, slug or name")


def _add_output(parser: argparse.ArgumentParser) -> None:
    out = parser.add_argument_group("output (progress and logs go to stderr; results to stdout)")
    level = out.add_mutually_exclusive_group()
    level.add_argument(
        "-v", "--verbose", action="store_true", help="also log every HTTP request (status, time, size, request id)"
    )
    level.add_argument("-q", "--quiet", action="store_true", help="print only errors on stderr")
    out.add_argument("--debug", action="store_true", help="--verbose, plus tracebacks for errors")
    out.add_argument(
        "--log-file", metavar="FILE", help="also write a debug-level log to FILE (never tokens or document contents)"
    )


def _add_import(sources: Any, dcim: DCIM) -> None:
    places = f"{dcim.place}s"
    sub = sources.add_parser(
        dcim.key,
        help=f"import {dcim.name} {places}",
        description=f"Import {dcim.name} {places} as a Railyard estate (--name), or refresh one an earlier import "
        f"created (--project). What was designed in Railyard is kept; objects deleted in {dcim.name} are reported, "
        "and removed only with --allow-deletes.",
    )
    src = sub.add_argument_group(dcim.name)
    _add_dcim_url(src, dcim, required=False, what="read-only is enough")
    metavar = "SLUG" if dcim.place == "site" else "NAME"
    src.add_argument(dcim.place_flag, dest="places", metavar=metavar, action="append", default=[], help=dcim.place_help)
    src.add_argument(dcim.all_flag, dest="all_places", action="store_true", help=dcim.all_help)
    src.add_argument("--from-snapshot", metavar="FILE", help="replay a snapshot saved with --snapshot-out")
    src.add_argument("--snapshot-out", metavar="FILE", help=f"save what was read from {dcim.name} as JSON")
    dst = sub.add_argument_group("Railyard")
    _add_railyard(dst)
    target = dst.add_mutually_exclusive_group(required=True)
    target.add_argument("--project", metavar="REF", help="refresh this estate (id or URL slug)")
    target.add_argument("--name", help="create a new estate with this name")
    run = sub.add_argument_group("run")
    run.add_argument("--dry-run", action="store_true", help="show what would change; save nothing")
    run.add_argument("--allow-deletes", action="store_true", help=f"remove objects deleted in {dcim.name}")
    run.add_argument("--out", metavar="FILE", help="also write the Railyard project JSON to FILE")
    run.add_argument("--name-version", action="store_true", help=f'name the saved version "{dcim.name} import <date>"')
    run.add_argument(
        "--no-validate", action="store_true", help="save without checking the document with Railyard first"
    )
    run.add_argument(
        "--save-document", metavar="FILE", help="keep a copy of the document sent to Railyard (also on success)"
    )
    run.add_argument(
        "--failed-dir",
        metavar="DIR",
        help="where to keep the document of a save Railyard refuses (default: the current directory)",
    )
    _add_output(sub)


def _add_export(targets: Any, dcim: DCIM) -> None:
    sub = targets.add_parser(
        dcim.key,
        help=f"sync a Railyard estate into {dcim.name}",
        description=f"Sync a Railyard estate into {dcim.name} over its REST API. A dry run by default: it prints the "
        "planned changes and writes nothing; --apply writes them. Every object the sync creates carries the "
        f"project's ownership tag, and only those objects are ever updated or deleted. The {dcim.name} sync "
        "document is a paid deliverable on hosted Railyard.",
    )
    src = sub.add_argument_group("Railyard")
    _add_railyard(src)
    src.add_argument("--project", metavar="REF", required=True, help="the estate to export (id or URL slug)")
    src.add_argument("--change-request", metavar="ID", help="export a merge request's draft instead of main")
    dst = sub.add_argument_group(dcim.name)
    _add_dcim_url(dst, dcim, required=True, what="it must be allowed to write what the sync creates")
    if dcim is NETBOX:
        dst.add_argument(
            "--netbox-version",
            metavar="X.Y",
            help="the NetBox release to write for (default: read from NetBox's /api/status/)",
        )
    run = sub.add_argument_group("run")
    run.add_argument("--apply", action="store_true", help="write the changes (default: a dry run)")
    run.add_argument("--allow-deletes", action="store_true", help="delete owned objects gone from Railyard")
    if dcim is NETBOX:
        run.add_argument(
            "--import-components",
            action="store_true",
            help="give new device types their component templates from the netbox-community devicetype-library",
        )
    run.add_argument("--json", action="store_true", help="print the result as JSON")
    _add_output(sub)


def _refuse_tokens_on_argv(argv: list[str]) -> None:
    for arg in argv:
        flag = arg.split("=", 1)[0]
        if (flag.startswith("--") and "token" in flag.lower()) or arg.startswith("ry_"):
            raise UsageError(
                f"tokens are read from {NETBOX_TOKEN_ENV}, {NAUTOBOT_TOKEN_ENV} and {RAILYARD_TOKEN_ENV}, never from "
                "the command line (it would be kept in shell history and visible to other users). Set the variables "
                "instead, and revoke a token you have already typed on a command line."
            )


def _dcim(args: argparse.Namespace) -> DCIM:
    return DCIMS[args.target if args.command == "export" else args.source]


def _validate(args: argparse.Namespace) -> None:
    dcim = _dcim(args)
    if args.command == "export":
        if getattr(args, "netbox_version", None) is not None:
            match = NETBOX_VERSION.fullmatch(args.netbox_version.strip())
            if not match or (int(match.group(1)), int(match.group(2))) < MIN_VERSION:
                raise UsageError("--netbox-version must be a NetBox release from 4.0 on, such as 4.4 or 4.5")
        if args.change_request is not None and not args.change_request.strip():
            raise UsageError("--change-request must not be empty")
        return
    if args.from_snapshot is None:
        if not args.dcim_url:
            raise UsageError(f"{dcim.url_flag} is required (or replay a saved snapshot with --from-snapshot)")
        if args.places and args.all_places:
            raise UsageError(f"use {dcim.place_flag} or {dcim.all_flag}, not both")
        if not args.places and not args.all_places:
            raise UsageError(
                f"name at least one {dcim.place} with {dcim.place_flag}, or import every {dcim.place} with "
                f"{dcim.all_flag}"
            )
    if args.name is not None and not args.name.strip():
        raise UsageError("--name must not be empty")


def _env_token(name: str, what: str) -> str:
    token = os.environ.get(name, "").strip()
    if not token:
        raise UsageError(f"set {name} to {what}")
    return token


def _railyard_client(args: argparse.Namespace, token: str) -> RailyardClient:
    """A client for ``--railyard-url`` with ``--org`` resolved now, so a wrong org fails before any work."""
    try:
        client = RailyardClient(args.railyard_url, token, org=args.org)
    except ValueError as e:
        raise UsageError(str(e)) from None
    log.debug("Railyard %s, organisation %s", client.base_url, args.org)
    log.debug("Organisation %r is %s", args.org, client.org_id())
    return client


def _configure_logging() -> None:
    """Keep library logging off stdout, which carries only the CLI's own output (``--json`` must parse).

    diffsync logs every diff and write through structlog, whose default configuration prints to stdout.
    Route it through the standard library instead, like this package's own loggers: with no handlers
    configured only warnings and errors are shown, on stderr, and the chatter is dropped. An application
    that configures logging itself keeps its configuration.
    """
    import structlog

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


# ---- the commands -----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    args: argparse.Namespace | None = None
    logs: CLILogging | None = None
    progress = Progress()
    try:
        _refuse_tokens_on_argv(argv)
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exit_:  # argparse already printed the usage message
            return int(exit_.code or 0)
        _validate(args)
        logs = configure_cli(err, verbose=args.verbose, quiet=args.quiet, debug=args.debug, log_file=args.log_file)
        for name in (RAILYARD_TOKEN_ENV, NETBOX_TOKEN_ENV, NAUTOBOT_TOKEN_ENV):
            logs.add_secret(os.environ.get(name))
        log.debug("railyard-sync %s: %s", __version__, _shown_argv(argv))
        _configure_logging()
        if args.command == "export":
            return _export(args, out, err, progress, _dcim(args))
        return _import(args, out, err, progress, _dcim(args))
    except UsageError as e:
        print(f"railyard-sync: error: {e}", file=err)
        return EXIT_USAGE
    except RailyardPlanError as e:
        message = plan_message(e, args) + "".join(f"\n  {hint}" for hint in e.hints)
        _report_failure(err, message, args, logs, progress)
        return EXIT_PLAN
    except (CommandError, ImportRefused, RailyardAPIError, APIError, SyncRefused, *dcim_errors()) as e:
        _report_failure(err, f"error: {e}", args, logs, progress)
        return EXIT_ERROR
    except (OSError, ValueError) as e:  # unreadable/unwritable files, a malformed snapshot or sync document
        _report_failure(err, f"error: {e}", args, logs, progress)
        return EXIT_ERROR
    except KeyboardInterrupt:
        _report_failure(err, "interrupted.", args, logs, progress)
        return 130
    except Exception as e:  # a bug: say so, and how to get the detail for the report
        _report_failure(
            err,
            f"error: unexpected {type(e).__name__}: {e}\n  This is a railyard-sync bug. Re-run with --debug (and "
            "--log-file FILE) and report the output.",
            args,
            logs,
            progress,
        )
        return EXIT_ERROR
    finally:
        if logs is not None:
            logs.close()


def _shown_argv(argv: list[str]) -> str:
    return " ".join(argv)  # tokens never reach argv (_refuse_tokens_on_argv), and the filter redacts anyway


def _report_failure(
    err: TextIO, message: str, args: argparse.Namespace | None, logs: CLILogging | None, progress: Progress
) -> None:
    """The final error on stderr, how far the run got, and (with --debug) the traceback; the log file gets
    all of it."""
    scrub = logs.scrub if logs is not None else (lambda text: text)
    error = sys.exc_info()[1]
    lines = [f"railyard-sync: {message}", *progress.report(getattr(args, "command", None), error)]
    print(scrub("\n".join(lines)), file=err)
    log.debug("Failed: %s", "\n".join(lines), exc_info=True, extra={FILE_ONLY: True})
    if args is not None and getattr(args, "debug", False):
        print(scrub(traceback.format_exc()).rstrip(), file=err)


# ---- import -----------------------------------------------------------------------------------------------


def _read_snapshot(args: argparse.Namespace, dcim: DCIM) -> Snapshot:
    if args.from_snapshot:
        log.info("Reading the snapshot %s…", args.from_snapshot)
        with open(args.from_snapshot, encoding="utf-8") as fh:
            snapshot = Snapshot.from_dict(json.load(fh))
        if snapshot.source != dcim.key:
            raise CommandError(
                f"the snapshot {args.from_snapshot} was read from {flow.source_name(snapshot.source)}, not "
                f"{dcim.name}: replay it with 'railyard-sync import {snapshot.source}'."
            )
        return snapshot
    token = _env_token(dcim.token_env, f"a {dcim.name} API token (read-only is enough)")
    if args.insecure:
        log.warning("not verifying %s's TLS certificate (--insecure)", dcim.name)
    places = None if args.all_places else list(args.places)
    load = load_nautobot_snapshot if dcim is NAUTOBOT else load_netbox_snapshot
    return load(args.dcim_url, token, places, verify=not args.insecure)


def _import(args: argparse.Namespace, out: TextIO, err: TextIO, progress: Progress, dcim: DCIM) -> int:
    railyard_token = _env_token(RAILYARD_TOKEN_ENV, "a Railyard personal access token (ry_…)")
    started = time.monotonic()
    snapshot = _read_snapshot(args, dcim)
    read = flow.snapshot_counts(snapshot)
    log.info("Read %s in %s", read, seconds(time.monotonic() - started))
    progress.done(f"read {_snapshot_source(snapshot, args)}: {read}")
    if args.snapshot_out:
        _write_json(args.snapshot_out, snapshot.to_dict())
        print(f"Snapshot written to {args.snapshot_out}", file=out)

    client = _railyard_client(args, railyard_token)
    plan = flow.plan_import(
        client,
        snapshot,
        project=args.project,
        name=None if args.project else args.name,
        build=build_project,
        catalogue=make_catalogue(client),
        allow_deletes=args.allow_deletes,
        requested=list(args.places),
        place_flag=dcim.place_flag,
        source_url=args.dcim_url or "",
        new_id=new_project_id,
        on_step=progress.done,
    )
    name, project_id = plan.name, plan.project_id
    print(flow.import_summary(snapshot, plan.imported, plan.report), file=out)
    if plan.diff is not None:
        print(plan.diff.summary(), file=out)
        if plan.diff.stale_count and not args.allow_deletes:
            print(f"Objects deleted in {dcim.name} were kept; re-run with --allow-deletes to remove them.", file=out)

    if args.out:
        _write_json(args.out, plan.document)
        print(f"Project written to {args.out}", file=out)

    if args.dry_run:
        problems = _preflight(client, plan, args, progress)
        if problems:
            print(_preflight_failure(problems, dry_run=True, dcim=dcim), file=out)
        action = f"refresh {name!r}" if plan.refresh else f"create {name!r} ({project_id})"
        print(f"Dry run: would {action}. Nothing was saved.", file=out)
        return EXIT_ERROR if problems else EXIT_OK
    if plan.conflicts:
        raise CommandError(
            f"{len(plan.conflicts)} conflict(s) between Railyard's design and {dcim.name} (listed above); "
            "resolve them in Railyard, then import again. Nothing was saved."
        )
    if plan.up_to_date:
        print(f"{name!r} is already up to date with {dcim.name}; nothing was saved.", file=out)
        return EXIT_OK
    problems = _preflight(client, plan, args, progress)
    if problems:
        raise CommandError(_preflight_failure(problems, dry_run=False, dcim=dcim))

    if args.save_document:
        _write_private_json(args.save_document, plan.document)
        log.info("Kept a copy of the document to send in %s", args.save_document)
    size = len(json.dumps(plan.document).encode("utf-8"))
    log.info("Saving to Railyard (%s)…", human_size(size))
    started = time.monotonic()
    progress.save_attempted = True
    new_revision = _save(client, plan, args)
    log.info("Saved revision %d in %s", new_revision, seconds(time.monotonic() - started))
    verb = "Refreshed" if plan.refresh else "Created"
    print(f"{verb} {name!r} ({project_id}) at revision {new_revision}.", file=out)
    if args.name_version:
        _name_version(client, project_id, new_revision, out, dcim)
    return EXIT_OK


def _preflight(client: RailyardClient, plan: ImportPlan, args: argparse.Namespace, progress: Progress) -> list[str]:
    return flow.preflight(client, plan.document, plan.existing, validate=not args.no_validate, on_step=progress.done)


def _preflight_failure(problems: list[str], *, dry_run: bool, dcim: DCIM) -> str:
    return flow.preflight_failure(
        problems, dry_run=dry_run, source=dcim.key, hint="pass --no-validate to let Railyard's save decide"
    )


def _save(client: RailyardClient, plan: ImportPlan, args: argparse.Namespace) -> int:
    """PUT the document; on any refusal keep what was sent (``--failed-dir``) and say where."""
    name = plan.name
    try:
        return flow.save(client, plan)
    except RailyardAPIError as e:
        path = keep_failed_document(plan.document, args.failed_dir)
        kept = (
            [
                f"The document that was sent is in {path} (readable only by you; it holds the estate's design, so "
                "share it only with Railyard support)."
            ]
            if path
            else []
        )
        if isinstance(e, RailyardPreconditionError) and e.status == 412:
            head = (
                f"{name!r} changed in Railyard while the import ran, so it was not overwritten. Run the import again: "
                "it merges onto the latest revision."
            )
        elif isinstance(e, RailyardConflictError) and e.code == "name_taken":
            head = (
                f"an estate named {name!r} already exists in this organisation: refresh it with --project, or choose "
                "another --name."
            )
        else:
            e.hints += kept
            raise
        raise CommandError("\n  ".join([head, *kept, *_request_id_line(e)])) from None


def _request_id_line(e: RailyardAPIError) -> list[str]:
    return [f"Quote request id {e.request_id} when reporting this."] if e.request_id else []


def keep_failed_document(document: dict, directory: str | None) -> str | None:
    """Write the document a refused save sent to ``railyard-sync-failed-<project id>-<UTC time>.json`` in
    ``directory`` (default: the current directory), readable only by its owner; its path, or ``None`` when
    it could not be written (a warning says why)."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    project = re.sub(r"[^A-Za-z0-9._-]", "_", str(document.get("id") or ""))[:80] or "estate"
    base = os.path.join(directory or ".", f"railyard-sync-failed-{project}-{stamp}")
    for n in range(1, 100):
        path = base + (f"-{n}" if n > 1 else "") + ".json"
        try:
            _write_private_json(path, document, exclusive=True)
        except FileExistsError:
            continue
        except OSError as exc:
            log.warning("could not keep the document that was sent (%s)", exc)
            return None
        log.debug("Kept the refused document in %s", path)
        return path
    return None  # pragma: no cover - a hundred failures in one second


def _write_private_json(path: str, data: Any, *, exclusive: bool = False) -> None:
    """Write JSON that only the owner may read (mode 0600): it holds the estate's design."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)  # an existing file keeps its mode through O_TRUNC; tighten it
    except (AttributeError, OSError):  # pragma: no cover - not every platform has fchmod
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _name_version(client: RailyardClient, project_id: str, revision: int, out: TextIO, dcim: DCIM) -> None:
    title = f"{dcim.name} import {today()}"
    try:
        client.name_version(project_id, title, revision)
    except RailyardAPIError as e:
        # The import is saved; a version name is a convenience, so its failure is a warning.
        disabled = getattr(e, "code", "") == "version_control_disabled"
        hint = " (switch version control on for the estate in Railyard)" if disabled else ""
        log.warning("the import was saved, but naming the version failed: %s%s", e, hint)
        return
    print(f"Named the version {title!r}.", file=out)


def _snapshot_source(snapshot: Snapshot, args: argparse.Namespace) -> str:
    source = SOURCE_NAMES.get(snapshot.source, snapshot.source)
    where = snapshot.source_url or args.dcim_url or ""
    version = f" ({snapshot.source_version})" if snapshot.source_version else ""
    origin = f"the snapshot {args.from_snapshot} of " if args.from_snapshot else ""
    places = "location" if snapshot.source == "nautobot" else "site"
    return f"{origin}{source} {where}{version}, {count(len(snapshot.sites), places)}".replace("  ", " ")


# ---- export -----------------------------------------------------------------------------------------------


def _export(args: argparse.Namespace, out: TextIO, err: TextIO, progress: Progress, dcim: DCIM) -> int:
    railyard_token = _env_token(RAILYARD_TOKEN_ENV, "a Railyard personal access token (ry_…)")
    token = _env_token(dcim.token_env, f"a {dcim.name} API token that may write what the sync creates")
    verify = not args.insecure
    if args.insecure:
        log.warning("not verifying %s's TLS certificate (--insecure)", dcim.name)
    netbox_version: str | None = None
    if dcim is NETBOX:
        try:
            netbox = NetBoxClient(args.dcim_url, token, verify=verify)
        except ValueError as e:
            raise UsageError(str(e)) from None
        netbox_version = args.netbox_version.strip() if args.netbox_version else _netbox_release(netbox)
        progress.done(f"read NetBox {netbox.url}" + (f" ({netbox.version})" if netbox.version else ""))
    else:
        try:
            nautobot = nautobot_rest.NautobotClient(args.dcim_url, token, verify=verify)
        except ValueError as e:
            raise UsageError(str(e)) from None
        log.info("Reading Nautobot %s…", nautobot.url)
        nautobot.status()
        nautobot_rest.check_version(nautobot, [])
        progress.done(f"read Nautobot {nautobot.url}" + (f" ({nautobot.version})" if nautobot.version else ""))

    client = _railyard_client(args, railyard_token)
    log.info("Fetching the %s sync document for %s from Railyard…", dcim.name, args.project)
    started = time.monotonic()
    change_request = args.change_request or None
    if dcim is NETBOX:
        document = client.netbox_sync_document(
            args.project, netbox_version=netbox_version, change_request_id=change_request
        )
    else:
        document = client.nautobot_sync_document(args.project, change_request_id=change_request)
    log.info(
        "Fetched the %s sync document (%s) in %s",
        dcim.name,
        human_size(len(json.dumps(document).encode("utf-8"))),
        seconds(time.monotonic() - started),
    )
    progress.done(f"fetched the {dcim.name} sync document for {args.project}")
    started = time.monotonic()
    result: SyncOutcome
    if dcim is NETBOX:
        result = sync_to_netbox(
            document,
            args.dcim_url,
            token,
            dry_run=not args.apply,
            allow_deletes=args.allow_deletes,
            railyard_url=args.railyard_url,
            verify=verify,
            import_components=args.import_components,
        )
        if netbox_version and parse_version(netbox_version) != parse_version(result.target_version):
            result.warnings.append(
                f"The document was written for NetBox {netbox_version}, but {result.target_url} runs NetBox "
                f"{result.target_version}; leave out --netbox-version to match it."
            )
    else:
        result = sync_to_nautobot(
            document,
            client=nautobot,
            dry_run=not args.apply,
            allow_deletes=args.allow_deletes,
            railyard_url=args.railyard_url,
        )
    log.info("Done in %s", seconds(time.monotonic() - started))
    if args.json:
        json.dump(result.as_dict(), out, indent=2, ensure_ascii=False)
        out.write("\n")
    else:
        print(export_report(result), file=out)
    return EXIT_ERROR if result.errors or result.conflicts else EXIT_OK


def _netbox_release(netbox: NetBoxClient) -> str | None:
    """The connected NetBox's release line (``"4.5"``), read from ``/api/status/``, for the document."""
    log.info("Reading NetBox %s…", netbox.url)
    netbox.status()
    major, minor = parse_version(netbox.version)
    if (major, minor) == (0, 0):
        log.warning(
            "NetBox did not report a readable version (%r); asking Railyard for its default. Pass "
            "--netbox-version to choose one.",
            netbox.version,
        )
        return None
    if (major, minor) < MIN_VERSION:
        raise NetBoxVersionError(f"NetBox {netbox.version} is not supported: 4.0 or later.")
    return f"{major}.{minor}"


# ---- export report ------------------------------------------------------------------------------------------

#: Object types in the order a sync creates them.
OBJECT_TYPES = tuple(netbox_rest.ENDPOINT)
NAUTOBOT_OBJECT_TYPES = tuple(nautobot_rest.ENDPOINT)


def _type_counts(counts: dict[str, dict[str, int]], verbs: tuple[str, str, str], order: tuple[str, ...]) -> list[str]:
    """``device: 4 created, 1 updated`` per object type, in creation order, for the types that change."""
    actions = ("create", "update", "delete")
    seen = {t for action in actions for t in (counts.get(action) or {})}
    lines = []
    for type_name in [*order, *sorted(seen - set(order))]:
        parts = [
            f"{counts[action][type_name]} {verb}"
            for action, verb in zip(actions, verbs, strict=True)
            if (counts.get(action) or {}).get(type_name)
        ]
        if parts:
            lines.append(f"  {type_name.replace('_', ' ')}: {', '.join(parts)}")
    return lines


def _section(title: str, items: list[str], hint: str = "") -> list[str]:
    if not items:
        return []
    return [f"{title} ({len(items)}){': ' + hint if hint else ''}:", *(f"  - {item}" for item in items)]


def export_report(result: SyncOutcome) -> str:
    """A sync's result as text: what changes (or would) per object type, then everything the operator should
    know — conflicts, adopted and renamed objects, refused and stale deletes, warnings and errors."""
    name = result.target_name
    order = NAUTOBOT_OBJECT_TYPES if isinstance(result, NautobotSyncResult) else OBJECT_TYPES
    lines = [
        f"Railyard estate {result.project_name!r} ({result.project_id}) → {name} {result.target_version} "
        f"at {result.target_url}",
        f"Ownership tag: {result.tag} (slug {result.tag_slug})",
    ]
    diff = result.diff
    if result.dry_run:
        lines.append(
            f"Dry run, planned: {diff.get('create', 0)} to create, {diff.get('update', 0)} to update, "
            f"{diff.get('delete', 0)} to delete, {diff.get('no-change', 0)} unchanged."
        )
        lines += _type_counts(result.planned, ("to create", "to update", "to delete"), order)
        lines += _section("Changes", result.changes)
    else:
        lines.append(
            f"Applied: {result.created} created, {result.updated} updated, {result.deleted} deleted, "
            f"{diff.get('no-change', 0)} unchanged."
        )
        lines += _type_counts(result.applied, ("created", "updated", "deleted"), order)
    skipped = f"skipped, with {result.dependents_skipped} dependent object(s)" if result.dependents_skipped else ""
    lines += _section("Conflicts", result.conflicts, skipped or f"skipped; resolve them in {name}")
    lines += _section("Used as they are", result.referenced, "existing shared objects, never changed")
    lines += _section("Adopted", result.adopted, "existing components of owned devices, now tagged")
    lines += _section("Renamed", result.renamed)
    lines += _section("Deletes refused", result.kept, "kept, as they reach objects the sync does not own")
    lines += _section("Stale", result.stale, "owned objects gone from Railyard, kept; --allow-deletes removes them")
    lines += _section("Warnings", result.warnings)
    lines += _section("Errors", result.errors, f"writes {name} refused; run the export again once fixed")
    if result.dry_run:
        lines.append(f"Dry run: nothing was written to {name}. Re-run with --apply to write these changes.")
    elif result.errors or result.conflicts:
        lines.append("Finished with errors or conflicts (listed above).")
    else:
        lines.append(f"{name} is in step with the Railyard estate.")
    return "\n".join(lines)


# ---- plan refusals ------------------------------------------------------------------------------------------


def _plan_name(plan_id: str) -> str:
    return PLAN_NAMES.get(plan_id, plan_id.replace("-", " ").title() if plan_id else "current")


def _either(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _upgrade_options(required_plans: list[str], project_pass: bool) -> list[str]:
    options = []
    if required_plans:
        options.append(f"upgrade to {_either([_plan_name(p) for p in required_plans])}")
    if project_pass:
        options.append("buy a Project Pass for this estate")
    return options


def _sentence(options: list[str]) -> str:
    """['a', 'b', 'c'] -> 'A, b, or c.'"""
    text = options[0] if len(options) == 1 else ", ".join(options[:-1]) + ", or " + options[-1]
    return text[:1].upper() + text[1:] + "."


def plan_message(e: RailyardPlanError, args: argparse.Namespace | None) -> str:
    """The upgrade message for a plan refusal, worded for the command that met it."""
    dcim = _dcim(args) if args is not None else NETBOX
    if args is not None and args.command == "export":
        return deliverable_plan_message(e, dcim.name)
    if isinstance(e, RailyardPlanLimitError):
        return plan_limit_message(e, refreshing=bool(args is not None and args.project), places=f"{dcim.place}s")
    options = _upgrade_options(e.required_plans, e.project_pass)
    return f"{e}" + (f". {_sentence(options)}" if options else "")


def deliverable_plan_message(e: RailyardPlanError, target: str = "NetBox") -> str:
    """'Exporting to NetBox needs a plan with deliverables: the Community plan does not include them. Upgrade to
    Pro or Team, or buy a Project Pass for this estate. Nothing was written to NetBox.'"""
    plan = _plan_name(e.plan)
    plan = plan if plan == "Project Pass" else f"the {plan} plan"
    if isinstance(e, RailyardPlanLimitError) and e.current is not None and e.limit is not None:
        resource = e.resource or "racks"
        head = (
            f"This estate has {e.current} {resource}; {plan} exports deliverables for estates of up to "
            f"{e.limit} {resource}."
        )
        extra = [f"remove {resource}"]
    elif isinstance(e, RailyardPlanRequiredError):
        head = f"Exporting to {target} needs a plan with deliverables: {plan} does not include them."
        extra = []
    else:
        head = f"Railyard refused the {target} sync document: {e}."
        extra = []
    options = _upgrade_options(e.required_plans, e.project_pass) or ["contact Railyard about an Enterprise plan"]
    return f"{head} {_sentence(options + extra)} Nothing was written to {target}."


def plan_limit_message(e: RailyardPlanLimitError, *, refreshing: bool = False, places: str = "sites") -> str:
    """'This import has 140 racks; the Community plan allows 25 per estate. Upgrade to Team or Partner,
    or import fewer sites. Nothing was saved.' — built from the refusal's fields."""
    resource = e.resource or "racks"
    per = " per estate" if (e.scope or "estate") == "estate" else ""
    plan = _plan_name(e.plan)
    plan = plan if plan == "Project Pass" else f"the {plan} plan"
    if e.current is None or e.limit is None:
        head = f"Railyard refused the import: {e}."
    elif refreshing:
        head = f"After this import the estate would have {e.current} {resource}; {plan} allows {e.limit}{per}."
    else:
        head = f"This import has {e.current} {resource}; {plan} allows {e.limit}{per}."
    options = _upgrade_options(e.required_plans, e.project_pass) or ["contact Railyard about an Enterprise plan"]
    if resource == "racks":
        options.append(f"import fewer {places}")
    return f"{head} {_sentence(options)} Nothing was saved."


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

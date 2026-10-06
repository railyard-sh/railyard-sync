"""The ``railyard-sync`` command line.

::

    railyard-sync import netbox --netbox-url URL (--site SLUG [--site SLUG …] | --all-sites)
        [--railyard-url URL] --org ORG (--project REF | --name NAME)
        [--dry-run] [--allow-deletes] [--out FILE] [--snapshot-out FILE] [--from-snapshot FILE]
        [--insecure] [--name-version]

    railyard-sync export netbox [--railyard-url URL] --org ORG --project REF [--change-request ID]
        --netbox-url URL [--netbox-version X.Y] [--apply] [--allow-deletes] [--import-components]
        [--insecure] [--json]

**Import** reads the NetBox sites into a :class:`~railyard_sync.dcim.snapshot.Snapshot` (or replays one
saved with ``--snapshot-out``), builds a Railyard project from it (device types matched against
Railyard's catalogue), and then either creates a new estate (``--name``) or refreshes an existing one
(``--project``) by merging the import into it with :func:`railyard_sync.importer.merge.merge` and saving
it with ``If-Match``, so a change made in Railyard meanwhile is never overwritten. ``--dry-run`` shows
what would happen and saves nothing.

**Export** fetches the estate's NetBox sync document from Railyard (a paid deliverable on hosted
Railyard; a self-hosted server with billing off allows it) for the NetBox release it reads from
``/api/status/`` (or ``--netbox-version``), and syncs it into NetBox with
:func:`railyard_sync.export.run.sync_to_netbox`, under the ownership tag keyed by ``--railyard-url`` and
the project id. It is a dry run that prints the planned changes unless ``--apply`` is given.

Tokens come from the environment only (``NETBOX_TOKEN``, ``RAILYARD_TOKEN``): a token on the command
line would sit in shell history and the process list, so one is refused.

Exit codes: 0 success, 1 error (an export with errors or conflicts too), 2 usage, 3 refused by the
Railyard plan (its rack limit, or a deliverable the plan does not include).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import Any, TextIO

from .client import RailyardClient
from .dcim.snapshot import Snapshot
from .errors import (
    RailyardAPIError,
    RailyardConflictError,
    RailyardPlanError,
    RailyardPlanLimitError,
    RailyardPlanRequiredError,
    RailyardPreconditionError,
)
from .export.netbox_rest import ENDPOINT, MIN_VERSION, NetBoxClient, NetBoxError, NetBoxVersionError, parse_version
from .export.policy import DEFAULT_RAILYARD_URL
from .export.run import SyncRefused, SyncResult, sync_to_netbox
from .importer.merge import MergeDiff, merge

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PLAN = 0, 1, 2, 3
NETBOX_TOKEN_ENV = "NETBOX_TOKEN"
RAILYARD_TOKEN_ENV = "RAILYARD_TOKEN"
NETBOX_PREFIX = "nb"

SOURCE_NAMES = {"netbox": "NetBox", "nautobot": "Nautobot"}

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


class UsageError(Exception):
    """A command-line mistake: reported with exit code 2."""


class CommandError(Exception):
    """A failure with a message for the user: reported with exit code 1."""


# ---- seams to the loader and the builder (imported lazily; the tests replace them) ----------------------


def load_netbox_snapshot(url: str, token: str, sites: list[str] | None, *, verify: bool = True) -> Snapshot:
    from .dcim.netbox import load_netbox_snapshot as load

    return load(url, token, sites, verify=verify)


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
        f"({NETBOX_TOKEN_ENV}, {RAILYARD_TOKEN_ENV}), never from the command line.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    _add_import(commands)
    _add_export(commands)
    return parser


def _add_netbox_url(group: Any, *, required: bool, what: str) -> None:
    help_ = f"NetBox base URL (token from ${NETBOX_TOKEN_ENV}: {what})"
    group.add_argument("--netbox-url", metavar="URL", required=required, help=help_)
    group.add_argument("--insecure", action="store_true", help="do not verify NetBox's TLS certificate")


def _add_railyard(group: Any) -> None:
    group.add_argument(
        "--railyard-url",
        metavar="URL",
        default=DEFAULT_RAILYARD_URL,
        help=f"Railyard URL (default {DEFAULT_RAILYARD_URL}; token from ${RAILYARD_TOKEN_ENV})",
    )
    group.add_argument("--org", required=True, help="organisation id, slug or name")


def _add_import(commands: Any) -> None:
    imp = commands.add_parser("import", help="import DCIM sites into a Railyard estate")
    sources = imp.add_subparsers(dest="source", metavar="SOURCE", required=True)
    nb = sources.add_parser(
        "netbox",
        help="import NetBox sites",
        description="Import NetBox sites as a Railyard estate (--name), or refresh one an earlier import "
        "created (--project). What was designed in Railyard is kept; objects deleted in NetBox are reported, "
        "and removed only with --allow-deletes.",
    )
    src = nb.add_argument_group("NetBox")
    _add_netbox_url(src, required=False, what="read-only is enough")
    src.add_argument("--site", metavar="SLUG", action="append", default=[], help="site slug; repeat for more")
    src.add_argument("--all-sites", action="store_true", help="every site the NetBox token can see, into one estate")
    src.add_argument("--from-snapshot", metavar="FILE", help="replay a snapshot saved with --snapshot-out")
    src.add_argument("--snapshot-out", metavar="FILE", help="save what was read from NetBox as JSON")
    dst = nb.add_argument_group("Railyard")
    _add_railyard(dst)
    target = dst.add_mutually_exclusive_group(required=True)
    target.add_argument("--project", metavar="REF", help="refresh this estate (id or URL slug)")
    target.add_argument("--name", help="create a new estate with this name")
    run = nb.add_argument_group("run")
    run.add_argument("--dry-run", action="store_true", help="show what would change; save nothing")
    run.add_argument("--allow-deletes", action="store_true", help="remove objects deleted in NetBox")
    run.add_argument("--out", metavar="FILE", help="also write the Railyard project JSON to FILE")
    run.add_argument("--name-version", action="store_true", help='name the saved version "NetBox import <date>"')


def _add_export(commands: Any) -> None:
    exp = commands.add_parser("export", help="export a Railyard estate into a DCIM")
    targets = exp.add_subparsers(dest="target", metavar="TARGET", required=True)
    nb = targets.add_parser(
        "netbox",
        help="sync a Railyard estate into NetBox",
        description="Sync a Railyard estate into NetBox over its REST API. A dry run by default: it prints the "
        "planned changes and writes nothing; --apply writes them. Every object the sync creates carries the "
        "project's ownership tag, and only tagged objects are ever updated or deleted. The NetBox sync document "
        "is a paid deliverable on hosted Railyard.",
    )
    src = nb.add_argument_group("Railyard")
    _add_railyard(src)
    src.add_argument("--project", metavar="REF", required=True, help="the estate to export (id or URL slug)")
    src.add_argument("--change-request", metavar="ID", help="export a merge request's draft instead of main")
    dst = nb.add_argument_group("NetBox")
    _add_netbox_url(dst, required=True, what="it must be allowed to write what the sync creates")
    dst.add_argument(
        "--netbox-version",
        metavar="X.Y",
        help="the NetBox release to write for (default: read from NetBox's /api/status/)",
    )
    run = nb.add_argument_group("run")
    run.add_argument("--apply", action="store_true", help="write the changes (default: a dry run)")
    run.add_argument("--allow-deletes", action="store_true", help="delete owned objects gone from Railyard")
    run.add_argument(
        "--import-components",
        action="store_true",
        help="give new device types their component templates from the netbox-community devicetype-library",
    )
    run.add_argument("--json", action="store_true", help="print the result as JSON")


def _refuse_tokens_on_argv(argv: list[str]) -> None:
    for arg in argv:
        flag = arg.split("=", 1)[0]
        if (flag.startswith("--") and "token" in flag.lower()) or arg.startswith("ry_"):
            raise UsageError(
                f"tokens are read from {NETBOX_TOKEN_ENV} and {RAILYARD_TOKEN_ENV}, never from the command line "
                "(it would be kept in shell history and visible to other users). Set the variables instead, and "
                "revoke a token you have already typed on a command line."
            )


def _validate(args: argparse.Namespace) -> None:
    if args.command == "export":
        if args.netbox_version is not None:
            match = NETBOX_VERSION.fullmatch(args.netbox_version.strip())
            if not match or (int(match.group(1)), int(match.group(2))) < MIN_VERSION:
                raise UsageError("--netbox-version must be a NetBox release from 4.0 on, such as 4.4 or 4.5")
        if args.change_request is not None and not args.change_request.strip():
            raise UsageError("--change-request must not be empty")
        return
    if args.from_snapshot is None:
        if not args.netbox_url:
            raise UsageError("--netbox-url is required (or replay a saved snapshot with --from-snapshot)")
        if args.site and args.all_sites:
            raise UsageError("use --site or --all-sites, not both")
        if not args.site and not args.all_sites:
            raise UsageError("name at least one site with --site, or import every site with --all-sites")
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
    client.org_id()
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
    try:
        _refuse_tokens_on_argv(argv)
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exit_:  # argparse already printed the usage message
            return int(exit_.code or 0)
        _validate(args)
        _configure_logging()
        if args.command == "export":
            return _export_netbox(args, out, err)
        return _import_netbox(args, out, err)
    except UsageError as e:
        print(f"railyard-sync: error: {e}", file=err)
        return EXIT_USAGE
    except RailyardPlanError as e:
        print(f"railyard-sync: {plan_message(e, args)}", file=err)
        return EXIT_PLAN
    except (CommandError, RailyardAPIError, NetBoxError, SyncRefused, *dcim_errors()) as e:
        print(f"railyard-sync: error: {e}", file=err)
        return EXIT_ERROR
    except (OSError, ValueError) as e:  # unreadable/unwritable files, a malformed snapshot or sync document
        print(f"railyard-sync: error: {e}", file=err)
        return EXIT_ERROR


def _import_netbox(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    railyard_token = _env_token(RAILYARD_TOKEN_ENV, "a Railyard personal access token (ry_…)")
    if args.from_snapshot:
        with open(args.from_snapshot, encoding="utf-8") as fh:
            snapshot = Snapshot.from_dict(json.load(fh))
    else:
        netbox_token = _env_token(NETBOX_TOKEN_ENV, "a NetBox API token (read-only is enough)")
        if args.insecure:
            print("warning: not verifying NetBox's TLS certificate (--insecure)", file=err)
        sites = None if args.all_sites else list(args.site)
        snapshot = load_netbox_snapshot(args.netbox_url, netbox_token, sites, verify=not args.insecure)
    if args.snapshot_out:
        _write_json(args.snapshot_out, snapshot.to_dict())
        print(f"Snapshot written to {args.snapshot_out}", file=out)

    client = _railyard_client(args, railyard_token)

    existing: dict | None = None
    revision: int | None = None
    if args.project:
        existing, revision = client.get_project_with_revision(args.project)
        _check_same_source(existing, snapshot, args)
        project_id, name = str(existing["id"]), str(existing.get("name") or args.project)
    else:
        project_id, name = new_project_id(), args.name.strip()

    built = build_project(
        snapshot, project_id=project_id, name=name, catalogue=make_catalogue(client), prefix=NETBOX_PREFIX
    )
    imported, report = built.project, built.report
    print(_import_summary(snapshot, imported, report), file=out)

    diff: MergeDiff | None = None
    if existing is not None:
        result = merge(existing, imported, prefix=NETBOX_PREFIX, allow_deletes=args.allow_deletes)
        document, diff = result.project, result.diff
        print(diff.summary(), file=out)
        if diff.stale_count and not args.allow_deletes:
            print("Objects deleted in NetBox were kept; re-run with --allow-deletes to remove them.", file=out)
    else:
        document = imported

    if args.out:
        _write_json(args.out, document)
        print(f"Project written to {args.out}", file=out)

    if args.dry_run:
        action = f"refresh {name!r}" if existing is not None else f"create {name!r} ({project_id})"
        print(f"Dry run: would {action}. Nothing was saved.", file=out)
        return EXIT_OK
    if diff is not None and diff.conflicts:
        raise CommandError(
            f"{len(diff.conflicts)} conflict(s) between Railyard's design and NetBox (listed above); "
            "resolve them in Railyard, then import again. Nothing was saved."
        )
    if existing is not None and _same_design(existing, document):
        print(f"{name!r} is already up to date with NetBox; nothing was saved.", file=out)
        return EXIT_OK

    new_revision = _save(client, document, revision, name)
    verb = "Refreshed" if existing is not None else "Created"
    print(f"{verb} {name!r} ({project_id}) at revision {new_revision}.", file=out)
    if args.name_version:
        _name_version(client, project_id, new_revision, out, err)
    return EXIT_OK


def _save(client: RailyardClient, document: dict, revision: int | None, name: str) -> int:
    try:
        return client.put_project(document, if_match=revision)
    except RailyardPreconditionError:
        raise CommandError(
            f"{name!r} changed in Railyard while the import ran, so it was not overwritten. Run the import again."
        ) from None
    except RailyardConflictError as e:
        if e.code == "name_taken":
            raise CommandError(
                f"an estate named {name!r} already exists in this organisation: refresh it with --project, "
                "or choose another --name."
            ) from None
        raise


def _name_version(client: RailyardClient, project_id: str, revision: int, out: TextIO, err: TextIO) -> None:
    title = f"NetBox import {today()}"
    try:
        client.name_version(project_id, title, revision)
    except RailyardAPIError as e:
        # The import is saved; a version name is a convenience, so its failure is a warning.
        disabled = getattr(e, "code", "") == "version_control_disabled"
        hint = " (switch version control on for the estate in Railyard)" if disabled else ""
        print(f"warning: the import was saved, but naming the version failed: {e}{hint}", file=err)
        return
    print(f"Named the version {title!r}.", file=out)


def _export_netbox(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    railyard_token = _env_token(RAILYARD_TOKEN_ENV, "a Railyard personal access token (ry_…)")
    netbox_token = _env_token(NETBOX_TOKEN_ENV, "a NetBox API token that may write what the sync creates")
    verify = not args.insecure
    if args.insecure:
        print("warning: not verifying NetBox's TLS certificate (--insecure)", file=err)
    try:
        netbox = NetBoxClient(args.netbox_url, netbox_token, verify=verify)
    except ValueError as e:
        raise UsageError(str(e)) from None

    version = args.netbox_version.strip() if args.netbox_version else _netbox_release(netbox, err)
    client = _railyard_client(args, railyard_token)
    document = client.netbox_sync_document(
        args.project, netbox_version=version, change_request_id=args.change_request or None
    )
    result = sync_to_netbox(
        document,
        args.netbox_url,
        netbox_token,
        dry_run=not args.apply,
        allow_deletes=args.allow_deletes,
        railyard_url=args.railyard_url,
        verify=verify,
        import_components=args.import_components,
    )
    if version and parse_version(version) != parse_version(result.netbox_version):
        result.warnings.append(
            f"The document was written for NetBox {version}, but {result.netbox_url} runs NetBox "
            f"{result.netbox_version}; leave out --netbox-version to match it."
        )
    if args.json:
        json.dump(result.as_dict(), out, indent=2, ensure_ascii=False)
        out.write("\n")
    else:
        print(export_report(result), file=out)
    return EXIT_ERROR if result.errors or result.conflicts else EXIT_OK


def _netbox_release(netbox: NetBoxClient, err: TextIO) -> str | None:
    """The connected NetBox's release line (``"4.5"``), read from ``/api/status/``, for the document."""
    netbox.status()
    major, minor = parse_version(netbox.version)
    if (major, minor) == (0, 0):
        print(
            f"warning: NetBox did not report a readable version ({netbox.version!r}); asking Railyard for its "
            "default. Pass --netbox-version to choose one.",
            file=err,
        )
        return None
    if (major, minor) < MIN_VERSION:
        raise NetBoxVersionError(f"NetBox {netbox.version} is not supported: 4.0 or later.")
    return f"{major}.{minor}"


# ---- checks and reporting -----------------------------------------------------------------------------------


def _sync_meta(doc: dict) -> dict:
    meta = doc.get("meta") or {}
    sync = meta.get("railyardSync") if isinstance(meta, dict) else None
    return sync if isinstance(sync, dict) else {}


def _recorded_source(doc: dict) -> tuple[str, str, list[str]]:
    """(kind, url, site slugs) an earlier import recorded in meta.railyardSync, as far as it says."""
    sync = _sync_meta(doc)
    # The builder writes {"source": "netbox", "url": …, "sites": […]}; accept a nested
    # {"source": {"kind", "url", "sites"}} too.
    source = sync.get("source") if isinstance(sync.get("source"), dict) else sync
    kind = source.get("kind") or (sync.get("source") if isinstance(sync.get("source"), str) else "")
    sites = source.get("sites") or sync.get("sites") or []
    slugs = [str(s.get("slug") or s.get("name") or "") if isinstance(s, dict) else str(s) for s in sites]
    return str(kind or ""), str(source.get("url") or ""), [s for s in slugs if s]


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _check_same_source(existing: dict, snapshot: Snapshot, args: argparse.Namespace) -> None:
    """Refuse to merge another DCIM into an estate (their ids would collide), or a subset of its sites
    (the others would all read as deleted)."""
    kind, url, sites = _recorded_source(existing)
    if kind and kind != snapshot.source:
        raise CommandError(
            f"this estate was imported from {kind}, not {snapshot.source}; it cannot be refreshed from it"
        )
    source_url = snapshot.source_url or args.netbox_url or ""
    if url and source_url and _norm_url(url) != _norm_url(source_url):
        raise CommandError(
            f"this estate was imported from {url}, not {source_url}: ids from two NetBox instances would collide. "
            "Import into a new estate with --name instead."
        )
    requested = {s.slug for s in snapshot.sites} | set(args.site)
    missing = [s for s in sites if s not in requested]
    if missing:
        flags = " ".join(f"--site {s}" for s in sites)
        raise CommandError(
            f"this estate was imported from site(s) {', '.join(sites)}; import all of them again ({flags}) so the "
            "objects of the others are not reported as deleted."
        )


def _same_design(existing: dict, document: dict) -> bool:
    """Whether a refresh changes nothing but the import record (its time), so it need not be saved."""

    def strip(doc: dict) -> dict:
        doc = dict(doc)
        meta = dict(doc.get("meta") or {})
        meta.pop("railyardSync", None)
        doc["meta"] = meta
        return doc

    return strip(existing) == strip(document)


def _count(doc: dict) -> dict[str, int]:
    racks = doc.get("racks") or []
    return {
        "racks": len(racks),
        "devices": sum(len(r.get("placements") or []) for r in racks),
        "cables": len(doc.get("cables") or []),
        "power links": len(doc.get("powerLinks") or []),
    }


def _import_summary(snapshot: Snapshot, imported: dict, report: Any) -> str:
    sites = ", ".join(s.slug for s in snapshot.sites) or "no sites"
    counts = ", ".join(f"{n} {what if n != 1 else what[:-1]}" for what, n in _count(imported).items())
    source = SOURCE_NAMES.get(snapshot.source, snapshot.source)
    lines = [f"Read {source} ({sites}): {counts}."]
    text = _report_text(report)
    if text:
        lines.append(text)
    return "\n".join(lines)


def _report_text(report: Any) -> str:
    """The builder's report as text: its summary() when it has one."""
    if report is None:
        return ""
    summary = getattr(report, "summary", None)
    text = summary() if callable(summary) else str(report)
    return str(text).strip()


# ---- export report ------------------------------------------------------------------------------------------

#: Object types in the order a sync creates them (``netbox_rest.ENDPOINT``).
OBJECT_TYPES = tuple(ENDPOINT)


def _type_counts(counts: dict[str, dict[str, int]], verbs: tuple[str, str, str]) -> list[str]:
    """``device: 4 created, 1 updated`` per object type, in creation order, for the types that change."""
    actions = ("create", "update", "delete")
    seen = {t for action in actions for t in (counts.get(action) or {})}
    lines = []
    for type_name in [*OBJECT_TYPES, *sorted(seen - set(OBJECT_TYPES))]:
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


def export_report(result: SyncResult) -> str:
    """A sync's result as text: what changes (or would) per object type, then everything the operator should
    know — conflicts, adopted and renamed objects, refused and stale deletes, warnings and errors."""
    lines = [
        f"Railyard estate {result.project_name!r} ({result.project_id}) → NetBox {result.netbox_version} "
        f"at {result.netbox_url}",
        f"Ownership tag: {result.tag} (slug {result.tag_slug})",
    ]
    diff = result.diff
    if result.dry_run:
        lines.append(
            f"Dry run, planned: {diff.get('create', 0)} to create, {diff.get('update', 0)} to update, "
            f"{diff.get('delete', 0)} to delete, {diff.get('no-change', 0)} unchanged."
        )
        lines += _type_counts(result.planned, ("to create", "to update", "to delete"))
        lines += _section("Changes", result.changes)
    else:
        lines.append(
            f"Applied: {result.created} created, {result.updated} updated, {result.deleted} deleted, "
            f"{diff.get('no-change', 0)} unchanged."
        )
        lines += _type_counts(result.applied, ("created", "updated", "deleted"))
    skipped = f"skipped, with {result.dependents_skipped} dependent object(s)" if result.dependents_skipped else ""
    lines += _section("Conflicts", result.conflicts, skipped or "skipped; resolve them in NetBox")
    lines += _section("Used as they are", result.referenced, "existing shared objects, never changed")
    lines += _section("Adopted", result.adopted, "existing components of owned devices, now tagged")
    lines += _section("Renamed", result.renamed)
    lines += _section("Deletes refused", result.kept, "kept, as they reach objects the sync does not own")
    lines += _section("Stale", result.stale, "owned objects gone from Railyard, kept; --allow-deletes removes them")
    lines += _section("Warnings", result.warnings)
    lines += _section("Errors", result.errors, "writes NetBox refused; run the export again once fixed")
    if result.dry_run:
        lines.append("Dry run: nothing was written to NetBox. Re-run with --apply to write these changes.")
    elif result.errors or result.conflicts:
        lines.append("Finished with errors or conflicts (listed above).")
    else:
        lines.append("NetBox is in step with the Railyard estate.")
    return "\n".join(lines)


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
    if args is not None and args.command == "export":
        return deliverable_plan_message(e)
    if isinstance(e, RailyardPlanLimitError):
        return plan_limit_message(e, refreshing=bool(args is not None and args.project))
    options = _upgrade_options(e.required_plans, e.project_pass)
    return f"{e}" + (f". {_sentence(options)}" if options else "")


def deliverable_plan_message(e: RailyardPlanError) -> str:
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
        head = f"Exporting to NetBox needs a plan with deliverables: {plan} does not include them."
        extra = []
    else:
        head = f"Railyard refused the NetBox sync document: {e}."
        extra = []
    options = _upgrade_options(e.required_plans, e.project_pass) or ["contact Railyard about an Enterprise plan"]
    return f"{head} {_sentence(options + extra)} Nothing was written to NetBox."


def plan_limit_message(e: RailyardPlanLimitError, *, refreshing: bool = False) -> str:
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
        options.append("import fewer sites")
    return f"{head} {_sentence(options)} Nothing was saved."


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

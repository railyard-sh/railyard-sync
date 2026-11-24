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

    railyard-sync export sunbird [--railyard-url URL] --org ORG --project REF [--change-request ID | --version ID]
        [--container ID | --rack ID [--rack ID …]]
        --dctrack-url URL (--dctrack-user USER [--dctrack-password-file FILE] | --dctrack-client-id ID)
        [--ca-bundle FILE | --no-verify-tls] [--insecure-http]
        (--location CODE | --location-map "SPACE=CODE" …) [--match-u bottom|own]
        [--devices [--cabinet-model MAKE/MODEL] [--model-map FILE] [--placeholder-make MAKE] [--no-placeholders]]
        [--apply] [--overwrite] [--import-file FILE] [--json] [-v | -q] [--debug] [--log-file FILE]

**Import** reads the NetBox sites or Nautobot locations into a :class:`~railyard_sync.dcim.snapshot.Snapshot` (or
replays one saved with ``--snapshot-out``), builds a Railyard project from it (device types matched against
Railyard's catalogue), and then either creates a new estate (``--name``) or refreshes an existing one
(``--project``) by merging the import into it, saving it with ``If-Match`` so a change made in Railyard meanwhile is
never overwritten. The steps are :class:`railyard_sync.importer.run.ImportRun`'s, which the plugins' import jobs share.
``--dry-run`` shows what would happen and saves nothing.

**Export** fetches the estate's sync document from Railyard (a paid deliverable on hosted Railyard; a self-hosted
server with billing off allows it) — for NetBox, for the release it reads from ``/api/status/`` (or
``--netbox-version``) — and syncs it with :func:`railyard_sync.export.run.sync_to_netbox` or
:func:`~railyard_sync.export.run.sync_to_nautobot`, under the ownership tag keyed by ``--railyard-url`` and the
project id. It is a dry run that prints the planned changes unless ``--apply`` is given.

**Export sunbird** fetches the estate's ``sunbird-sync`` document and pushes its device serials into Sunbird dcTrack
(:func:`railyard_sync.export.dctrack_serials.sync_serials_to_dctrack`): each device is matched to an existing dcTrack
item by cabinet and U position, and its serial written over dcTrack's REST API (``--apply``) or into a File Import CSV
(``--import-file``). A dry run by default too. With ``--devices`` it reconciles placement as well
(:func:`railyard_sync.export.dctrack_devices.sync_placements_to_dctrack`): it creates the devices dcTrack lacks (on
placeholder models where its Models Library has no match) and moves Planned items Railyard places elsewhere.

Before a save, the document is checked with Railyard's ``/api/validate`` (``--no-validate`` skips it): errors
stop the import, listed by rack and device, and warnings are reported. A save Railyard refuses keeps the
document that was sent in ``railyard-sync-failed-<project id>-<UTC time>.json`` (``--failed-dir``), readable
only by its owner, for the report; ``--save-document`` keeps it on success too.

stdout carries the results (summaries, the diff, the export report, ``--json``); stderr carries progress lines,
warnings and errors (``-v`` adds every HTTP request with its status, time, size and request id; ``-q`` keeps
only errors; ``--debug`` adds tracebacks), and ``--log-file`` writes everything at debug level to a file. No
log ever holds a token, an Authorization header or a request body.

Tokens come from the environment only (``NETBOX_TOKEN``, ``NAUTOBOT_TOKEN``, ``RAILYARD_TOKEN``): a token on the
command line would sit in shell history and the process list, so one is refused. dcTrack's password comes from
``DCTRACK_PASSWORD`` or ``--dctrack-password-file``, an OAuth2 client's secret from ``DCTRACK_CLIENT_SECRET``.

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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from . import __version__
from .client import RailyardClient
from .dcim.snapshot import Snapshot
from .dcim_http import APIError
from .errors import RailyardAPIError, RailyardPlanError, RailyardPlanLimitError
from .export import dctrack_serials, nautobot_rest, netbox_rest
from .export.dctrack_devices import (
    DEFAULT_PLACEHOLDER_MAKE,
    PlacementResult,
    parse_cabinet_model,
    read_model_map,
    sync_placements_to_dctrack,
)
from .export.dctrack_rest import CLIENT_SECRET_ENV, PASSWORD_ENV, DcTrackClient
from .export.dctrack_serials import SerialSyncResult, sync_serials_to_dctrack
from .export.netbox_rest import MIN_VERSION, NetBoxClient, NetBoxVersionError, parse_version
from .export.policy import DEFAULT_RAILYARD_URL
from .export.run import NautobotSyncResult, SyncOutcome, SyncRefused, sync_to_nautobot, sync_to_netbox
from .importer.run import (
    SOURCE_NAMES,
    ImportRefused,
    ImportRun,
    check_failure,
    conflicts_message,
    import_summary,
    new_project_id,
    snapshot_counts,
    source_name,
)
from .log import FILE_ONLY, CLILogging, configure_cli, count, human_size, seconds
from .plans import PLAN_NAMES, deliverable_plan_message, plan_limit_message, sentence, upgrade_options

log = logging.getLogger(__name__)

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PLAN = 0, 1, 2, 3
NETBOX_TOKEN_ENV = "NETBOX_TOKEN"
NAUTOBOT_TOKEN_ENV = "NAUTOBOT_TOKEN"
RAILYARD_TOKEN_ENV = "RAILYARD_TOKEN"
NETBOX_PREFIX = "nb"
NAUTOBOT_PREFIX = "nbt"

__all__ = ["PLAN_NAMES", "SOURCE_NAMES", "deliverable_plan_message", "main", "plan_limit_message"]


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
#: ``export sunbird``: Sunbird dcTrack takes serials only, not a DiffSync export, so it has its own parser.
SUNBIRD = "sunbird"
DCTRACK_NAME = "dcTrack"


class UsageError(Exception):
    """A command-line mistake: reported with exit code 2."""


class CommandError(ImportRefused):
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
            uncertain = self.save_attempted and not refused and not isinstance(error, ImportRefused)
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
    _add_export_sunbird(targets)
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


def _add_export_sunbird(targets: Any) -> None:
    sub = targets.add_parser(
        SUNBIRD,
        help="push a Railyard estate's device serials into Sunbird dcTrack",
        allow_abbrev=False,  # --dctrack-password must never be taken for --dctrack-password-file
        description="Push the serial numbers Railyard records for an estate's devices (design serials and those the "
        "build crew read on site) into Sunbird dcTrack, where the devices already exist. Each device is matched to a "
        "dcTrack item by cabinet and U position, not by name. A dry run by default: it prints the plan and writes "
        "nothing; --apply writes the serials over dcTrack's REST API, and --import-file writes them to a File Import "
        "CSV instead (or as well). A dcTrack serial is never cleared, and one that differs from Railyard's is kept "
        "unless --overwrite. The Sunbird sync document is a paid deliverable on hosted Railyard.",
    )
    src = sub.add_argument_group("Railyard")
    _add_railyard(src)
    src.add_argument("--project", metavar="REF", required=True, help="the estate to export (id or URL slug)")
    source = src.add_mutually_exclusive_group()
    source.add_argument("--change-request", metavar="ID", help="export a merge request's draft instead of main")
    source.add_argument("--version", metavar="ID", help="export a saved version (such as 'Approved for build')")
    scope = src.add_mutually_exclusive_group()
    scope.add_argument("--container", metavar="ID", help="only the racks in this space (its Railyard id)")
    scope.add_argument(
        "--rack", metavar="ID", dest="racks", action="append", default=[], help="only this rack (its id); repeatable"
    )
    dst = sub.add_argument_group("dcTrack")
    dst.add_argument("--dctrack-url", metavar="URL", required=True, help="dcTrack base URL (https)")
    dst.add_argument(
        "--dctrack-user",
        metavar="USER",
        help=f"dcTrack user (basic auth; password from ${PASSWORD_ENV} or --dctrack-password-file; not a SAML account)",
    )
    dst.add_argument("--dctrack-password-file", metavar="FILE", help="read the dcTrack user's password from FILE")
    dst.add_argument(
        "--dctrack-client-id",
        metavar="ID",
        help=f"dcTrack OAuth2 client id (dcTrack 9.3 or later; secret from ${CLIENT_SECRET_ENV})",
    )
    tls = dst.add_mutually_exclusive_group()
    tls.add_argument("--ca-bundle", metavar="FILE", help="verify dcTrack's certificate with this CA bundle")
    tls.add_argument("--no-verify-tls", action="store_true", help="do not verify dcTrack's TLS certificate")
    dst.add_argument(
        "--insecure-http", action="store_true", help="allow an http:// dcTrack URL (the password crosses in clear)"
    )
    match = sub.add_argument_group("matching")
    match.add_argument("--location", metavar="CODE", help="the dcTrack location code holding the cabinets")
    match.add_argument(
        "--location-map",
        metavar="SPACE=CODE",
        action="append",
        default=[],
        help="put the cabinets under this Railyard space in this dcTrack location (the deepest mapped space wins; "
        "the rest use --location); repeatable",
    )
    match.add_argument(
        "--match-u",
        choices=dctrack_serials.MATCH_U,
        default=dctrack_serials.MATCH_BOTTOM,
        help="match dcTrack's U position against the device's bottom U counted from the cabinet's bottom (bottom, "
        "the default, as dcTrack numbers cabinets) or in the rack's own numbering (own, for top-down cabinets "
        "numbered alike in both)",
    )
    run = sub.add_argument_group("run")
    run.add_argument("--apply", action="store_true", help="write the serials to dcTrack (default: a dry run)")
    run.add_argument("--overwrite", action="store_true", help="replace a dcTrack serial that differs from Railyard's")
    run.add_argument(
        "--import-file",
        metavar="FILE",
        help="also write the updates as a dcTrack File Import CSV (EDIT rows: # Operation, Object, Location, Name, "
        "Serial Number); the column headings must match your dcTrack import template and may need adjusting",
    )
    run.add_argument("--json", action="store_true", help="print the result as JSON")
    place = sub.add_argument_group(
        "placement (--devices)",
        "With --devices the export also places devices: it creates (as Planned) those dcTrack does not have and moves "
        "Planned items Railyard places elsewhere. It never deletes, renames, or moves an Installed item.",
    )
    place.add_argument(
        "--devices", action="store_true", help="reconcile device placement as well as serials (not with --import-file)"
    )
    place.add_argument(
        "--cabinet-model",
        metavar="MAKE/MODEL",
        help="create cabinets dcTrack lacks with this cabinet model from its Models Library (each uses a dcTrack "
        "cabinet licence); without it their devices are skipped",
    )
    place.add_argument(
        "--model-map",
        metavar="FILE",
        help="a CSV mapping Railyard models to dcTrack's (columns railyard_make, railyard_model, dctrack_make, "
        "dctrack_model), tried before an exact make and model match in dcTrack's Models Library",
    )
    place.add_argument(
        "--placeholder-make",
        metavar="MAKE",
        default=DEFAULT_PLACEHOLDER_MAKE,
        help=f"the make of placeholder models ('Generic 2U Device'…) for unmatched models (default "
        f"{DEFAULT_PLACEHOLDER_MAKE})",
    )
    place.add_argument(
        "--no-placeholders", action="store_true", help="report a device with no matching model instead of a placeholder"
    )
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
        lowered = flag.lower()
        secret_flag = "password" in lowered or "secret" in lowered
        if flag.startswith("--") and secret_flag and lowered != "--dctrack-password-file":
            raise UsageError(
                f"passwords and secrets are read from {PASSWORD_ENV} (or --dctrack-password-file) and "
                f"{CLIENT_SECRET_ENV}, never from the command line (it would be kept in shell history and visible to "
                "other users). Change a password you have already typed on a command line."
            )


def _dcim(args: argparse.Namespace) -> DCIM:
    return DCIMS[args.target if args.command == "export" else args.source]


def _validate(args: argparse.Namespace) -> None:
    if args.command == "export" and args.target == SUNBIRD:
        _validate_sunbird(args)
        return
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


def _validate_sunbird(args: argparse.Namespace) -> None:
    for flag, value in (
        ("--change-request", args.change_request),
        ("--version", args.version),
        ("--container", args.container),
        ("--location", args.location),
    ):
        if value is not None and not value.strip():
            raise UsageError(f"{flag} must not be empty")
    if any(not rack.strip() for rack in args.racks):
        raise UsageError("--rack must not be empty")
    if bool(args.dctrack_user) == bool(args.dctrack_client_id):
        raise UsageError(
            f"give --dctrack-user (its password from {PASSWORD_ENV} or --dctrack-password-file) or --dctrack-client-id "
            f"(its secret from {CLIENT_SECRET_ENV}), one of them"
        )
    if args.dctrack_password_file and not args.dctrack_user:
        raise UsageError("--dctrack-password-file goes with --dctrack-user")
    if not args.location and not args.location_map:
        raise UsageError("name the dcTrack location with --location CODE, or map Railyard spaces with --location-map")
    try:
        dctrack_serials.parse_location_map(args.location_map)
    except ValueError as e:
        raise UsageError(str(e)) from None
    if args.devices and args.import_file:
        raise UsageError(
            "--import-file writes serials only; it cannot be combined with --devices (run the two separately)"
        )
    placement = [
        flag
        for flag, given in (
            ("--cabinet-model", args.cabinet_model is not None),
            ("--model-map", args.model_map is not None),
            ("--placeholder-make", args.placeholder_make != DEFAULT_PLACEHOLDER_MAKE),
            ("--no-placeholders", args.no_placeholders),
        )
        if given
    ]
    if placement and not args.devices:
        raise UsageError(f"{', '.join(placement)} go{'es' if len(placement) == 1 else ''} with --devices")
    if args.cabinet_model is not None:
        try:
            parse_cabinet_model(args.cabinet_model)
        except ValueError as e:
            raise UsageError(str(e)) from None
    if not args.placeholder_make.strip():
        raise UsageError("--placeholder-make must not be empty")


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
        for name in (RAILYARD_TOKEN_ENV, NETBOX_TOKEN_ENV, NAUTOBOT_TOKEN_ENV, PASSWORD_ENV, CLIENT_SECRET_ENV):
            logs.add_secret(os.environ.get(name))
        log.debug("railyard-sync %s: %s", __version__, _shown_argv(argv))
        _configure_logging()
        if args.command == "export" and args.target == SUNBIRD:
            return _export_sunbird(args, out, progress, logs)
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
    except (ImportRefused, RailyardAPIError, APIError, SyncRefused, *dcim_errors()) as e:
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
                f"the snapshot {args.from_snapshot} was read from {source_name(snapshot.source)}, not "
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
    read = snapshot_counts(snapshot)
    log.info("Read %s in %s", read, seconds(time.monotonic() - started))
    progress.done(f"read {_snapshot_source(snapshot, args)}: {read}")
    if args.snapshot_out:
        _write_json(args.snapshot_out, snapshot.to_dict())
        print(f"Snapshot written to {args.snapshot_out}", file=out)

    client = _railyard_client(args, railyard_token)
    run = ImportRun(
        client,
        snapshot,
        project=args.project,
        name=None if args.project else args.name,
        allow_deletes=args.allow_deletes,
        source_url=args.dcim_url or "",
        requested_sites=args.places,
        catalogue=make_catalogue(client),
        build=build_project,
        project_id=None if args.project else new_project_id(),
        step=progress.done,
        place_flag=dcim.place_flag,
    )
    run.fetch()
    built = run.build()
    print(import_summary(snapshot, built.project, built.report), file=out)

    diff = run.merge()
    if diff is not None:
        print(diff.summary(), file=out)
        if diff.stale_count and not args.allow_deletes:
            print(f"Objects deleted in {dcim.name} were kept; re-run with --allow-deletes to remove them.", file=out)
    document, name = run.document, run.name

    if args.out:
        _write_json(args.out, document)
        print(f"Project written to {args.out}", file=out)

    if args.dry_run:
        problems = run.check(validate=not args.no_validate)
        if problems:
            print(check_failure(problems, dry_run=True, source=dcim.key), file=out)
        action = f"refresh {name!r}" if run.refreshing else f"create {name!r} ({run.project_id})"
        print(f"Dry run: would {action}. Nothing was saved.", file=out)
        return EXIT_ERROR if problems else EXIT_OK
    if run.conflicts:
        raise CommandError(conflicts_message(len(run.conflicts), dcim.key))
    if run.up_to_date():
        print(f"{name!r} is already up to date with {dcim.name}; nothing was saved.", file=out)
        return EXIT_OK
    problems = run.check(validate=not args.no_validate)
    if problems:
        raise CommandError(check_failure(problems, dry_run=False, source=dcim.key))

    if args.save_document:
        _write_private_json(args.save_document, document)
        log.info("Kept a copy of the document to send in %s", args.save_document)
    size = len(json.dumps(document).encode("utf-8"))
    log.info("Saving to Railyard (%s)…", human_size(size))
    started = time.monotonic()
    progress.save_attempted = True
    new_revision = run.save(on_refused=lambda doc, e: _kept_document_lines(doc, args.failed_dir))
    log.info("Saved revision %d in %s", new_revision, seconds(time.monotonic() - started))
    verb = "Refreshed" if run.refreshing else "Created"
    print(f"{verb} {name!r} ({run.project_id}) at revision {new_revision}.", file=out)
    if args.name_version:
        _name_version(client, run.project_id, new_revision, out, dcim)
    return EXIT_OK


def _kept_document_lines(document: dict, directory: str | None) -> list[str]:
    """Keep the document a refused save sent (``--failed-dir``) and say where, for the error."""
    path = keep_failed_document(document, directory)
    if not path:
        return []
    return [
        f"The document that was sent is in {path} (readable only by you; it holds the estate's design, so share it "
        "only with Railyard support)."
    ]


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


# ---- export sunbird ------------------------------------------------------------------------------------------


def _dctrack_secret(args: argparse.Namespace) -> str:
    """The dcTrack user's password (``--dctrack-password-file``, else ``$DCTRACK_PASSWORD``) or the OAuth2 client's
    secret (``$DCTRACK_CLIENT_SECRET``)."""
    if args.dctrack_client_id:
        return _env_token(CLIENT_SECRET_ENV, "the dcTrack OAuth2 client's secret")
    if not args.dctrack_password_file:
        return _env_token(PASSWORD_ENV, f"the password of dcTrack user {args.dctrack_user!r}")
    path = args.dctrack_password_file
    try:
        mode = os.stat(path).st_mode
        with open(path, encoding="utf-8") as fh:
            password = fh.read().rstrip("\r\n")
    except OSError as e:
        raise UsageError(f"cannot read --dctrack-password-file {path}: {e.strerror or e}") from None
    if not password:
        raise UsageError(f"--dctrack-password-file {path} is empty")
    if mode & 0o077:
        log.warning("%s can be read by other users: make it readable only by you (chmod 600)", path)
    return password


def _dctrack_client(args: argparse.Namespace, secret: str, logs: CLILogging) -> DcTrackClient:
    verify: bool | str = args.ca_bundle or not args.no_verify_tls
    if args.no_verify_tls:
        log.warning("not verifying dcTrack's TLS certificate (--no-verify-tls)")
    if args.ca_bundle and not os.path.isfile(args.ca_bundle):
        raise UsageError(f"--ca-bundle {args.ca_bundle} is not a file")
    try:
        client = DcTrackClient(
            args.dctrack_url,
            user=args.dctrack_user or "",
            password=secret if args.dctrack_user else "",
            client_id=args.dctrack_client_id or "",
            client_secret="" if args.dctrack_user else secret,
            verify=verify,
            allow_http=args.insecure_http,
            on_secret=logs.add_secret,
        )
    except ValueError as e:
        raise UsageError(str(e)) from None
    if not client.url.startswith("https:"):
        log.warning("dcTrack %s is not https: the credentials cross the network in clear text", client.url)
    return client


def _export_sunbird(args: argparse.Namespace, out: TextIO, progress: Progress, logs: CLILogging) -> int:
    railyard_token = _env_token(RAILYARD_TOKEN_ENV, "a Railyard personal access token (ry_…)")
    secret = _dctrack_secret(args)
    logs.add_secret(secret)
    dctrack = _dctrack_client(args, secret, logs)
    location_map = dctrack_serials.parse_location_map(args.location_map)
    try:
        model_map = read_model_map(args.model_map) if args.devices and args.model_map else {}
    except ValueError as e:
        raise UsageError(str(e)) from None

    client = _railyard_client(args, railyard_token)
    log.info("Fetching the Sunbird sync document for %s from Railyard…", args.project)
    started = time.monotonic()
    document = client.sunbird_sync_document(
        args.project,
        scope={"kind": "container", "id": args.container.strip()} if args.container else None,
        rack_ids=[rack.strip() for rack in args.racks] or None,
        change_request_id=(args.change_request or "").strip() or None,
        version_id=(args.version or "").strip() or None,
    )
    log.info(
        "Fetched the Sunbird sync document (%s) in %s",
        human_size(len(json.dumps(document).encode("utf-8"))),
        seconds(time.monotonic() - started),
    )
    progress.done(f"fetched the Sunbird sync document for {args.project}")
    if args.devices:
        placed = sync_placements_to_dctrack(
            document,
            dctrack,
            location=(args.location or "").strip(),
            location_map=location_map,
            match_u=args.match_u,
            overwrite=args.overwrite,
            dry_run=not args.apply,
            cabinet_model=parse_cabinet_model(args.cabinet_model) if args.cabinet_model else None,
            model_map=model_map,
            placeholder_make=args.placeholder_make.strip(),
            placeholders=not args.no_placeholders,
        )
        if args.json:
            json.dump(placed.as_dict(), out, indent=2, ensure_ascii=False)
            out.write("\n")
        else:
            print(placement_report(placed), file=out)
        return EXIT_ERROR if placed.failed else EXIT_OK
    result = sync_serials_to_dctrack(
        document,
        dctrack,
        location=(args.location or "").strip(),
        location_map=location_map,
        match_u=args.match_u,
        overwrite=args.overwrite,
        dry_run=not args.apply,
        import_file=args.import_file,
    )
    if args.json:
        json.dump(result.as_dict(), out, indent=2, ensure_ascii=False)
        out.write("\n")
    else:
        print(serial_report(result), file=out)
    return EXIT_ERROR if result.failed else EXIT_OK


def serial_report(result: SerialSyncResult) -> str:
    """``export sunbird``'s result as text: counts by outcome, then a line per update, conflict and unmatched device,
    the document's own skipped devices and warnings, and the refused writes."""
    numbering = "the U counted from the cabinet's bottom" if result.match_u == "bottom" else "the rack's own U"
    source = f", change request {result.change_request_id}" if result.change_request_id else ""
    source += f", version {result.version_id}" if result.version_id else ""
    lines = [
        f"Railyard estate {result.project_name!r} ({result.project_id}{source}) → dcTrack at {result.dctrack_url}",
        f"Matched by cabinet and {numbering}.",
    ]
    counts = ", ".join(f"{n} {outcome}" for outcome, n in result.counts.items() if n) or "no devices"
    lines.append(f"{'Dry run, planned' if result.dry_run else 'Devices'}: {counts}.")
    of = result.of
    verb = "to write" if result.dry_run else "sent to dcTrack; refusals under Errors" if result.errors else "written"
    lines += _section("Updates", [d.line() for d in of(dctrack_serials.UPDATE)], verb)
    lines += _section(
        "Conflicts",
        [d.line() for d in of(dctrack_serials.CONFLICT)],
        "not written; correct the serial in dcTrack or Railyard (--overwrite replaces a differing serial)",
    )
    lines += _section(
        "Ambiguous", [d.line() for d in of(dctrack_serials.AMBIGUOUS)], "not written: no single dcTrack item matches"
    )
    lines += _section(
        "Not matched",
        [d.line() for d in of(*dctrack_serials.PROBLEMS)],
        "devices with a serial that no dcTrack item was found for",
    )
    lines += _section("Skipped by Railyard", result.skipped)
    lines += _section("Warnings", result.warnings)
    lines += _section("Errors", result.errors, "updates dcTrack refused; run the export again once fixed")
    if result.import_file:
        lines.append(
            f"Wrote {count(result.import_rows, 'row')} to the dcTrack import file {result.import_file} (check its "
            "column headings against your dcTrack import template)."
        )
    if result.dry_run:
        lines.append("Dry run: nothing was written to dcTrack. Re-run with --apply to write these serials.")
    else:
        lines.append(f"Wrote {count(result.updated, 'serial')} to dcTrack.")
        if result.failed:
            lines.append("Finished with errors, conflicts or ambiguities (listed above).")
    return "\n".join(lines)


def placement_report(result: PlacementResult) -> str:
    """``export sunbird --devices``'s result as text: the serials report's sections, plus creates, moves, the cabinets,
    makes and placeholder models to create, devices on placeholders, names dcTrack gives otherwise, and items only
    dcTrack has."""
    numbering = "the U counted from the cabinet's bottom" if result.match_u == "bottom" else "the rack's own U"
    source = f", change request {result.change_request_id}" if result.change_request_id else ""
    source += f", version {result.version_id}" if result.version_id else ""
    dry = result.dry_run
    lines = [
        f"Railyard estate {result.project_name!r} ({result.project_id}{source}) → dcTrack at {result.dctrack_url}",
        f"Devices placed by cabinet and {numbering}; serials as for the serials export.",
    ]
    counts = ", ".join(f"{n} {outcome}" for outcome, n in result.counts.items() if n) or "no devices"
    lines.append(f"{'Dry run, planned' if dry else 'Devices'}: {counts}.")
    if result.only_in_dctrack:
        lines.append(f"Only in dcTrack: {count(len(result.only_in_dctrack), 'item')} (left as they are).")
    verb = "would create" if dry else "created"
    lines += _section(
        "Cabinets to create" if dry else "Cabinets",
        [f"{verb} cabinet {c}" for c in result.cabinets],
        "Planned; each uses a dcTrack cabinet licence",
    )
    lines += _section("Makes to create" if dry else "Makes", [f"{verb} make {m}" for m in result.makes])
    lines += _section("Models to create" if dry else "Models", [f"{verb} model {m}" for m in result.models])
    lines += _section("Creates", [d.line() for d in result.placements(dctrack_serials.CREATE)], "as Planned items")
    lines += _section("Moves", [d.line() for d in result.placements(dctrack_serials.MOVE)], "Planned items only")
    if result.placeholders:
        lines.append(
            f"{count(result.placeholders, 'device')} {'would land' if dry else 'landed'} on placeholder models: swap "
            "them for real models later with dcTrack's UPDATE MODEL import."
        )
    lines += _section(
        "Serial updates", [d.line() for d in result.of(dctrack_serials.UPDATE)], "to write" if dry else "sent"
    )
    lines += _section(
        "Conflicts",
        [d.line() for d in result.of(dctrack_serials.CONFLICT)],
        "not written; resolve them in dcTrack or Railyard (--overwrite replaces a differing serial)",
    )
    lines += _section(
        "Ambiguous", [d.line() for d in result.of(dctrack_serials.AMBIGUOUS)], "not written: no single dcTrack item"
    )
    lines += _section("Not placed", [d.line() for d in result.of(*dctrack_serials.DEVICE_PROBLEMS)])
    named = [
        f"{d.cabinet} {d.where_in_cabinet()} {d.name} is {d.item_name} in dcTrack"
        for d in result.devices
        if getattr(d, "name_differs", False)
    ]
    lines += _section("Named differently in dcTrack", named, "not renamed")
    lines += _section("Only in dcTrack", result.only_in_dctrack, "no Railyard device; never deleted")
    lines += _section("Skipped by Railyard", result.skipped)
    lines += _section("Warnings", result.warnings)
    lines += _section("Errors", result.errors, "writes dcTrack refused; run the export again once fixed")
    if dry:
        lines.append("Dry run: nothing was written to dcTrack. Re-run with --apply to write these changes.")
    else:
        lines.append(
            f"Created {count(result.created, 'item')}, moved {result.moved:,} and wrote "
            f"{count(result.updated, 'serial')} in dcTrack."
        )
        if result.failed:
            lines.append("Finished with errors, conflicts or ambiguities (listed above).")
    return "\n".join(lines)


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


def plan_message(e: RailyardPlanError, args: argparse.Namespace | None) -> str:
    """The upgrade message for a plan refusal, worded for the command that met it."""
    if args is not None and args.command == "export" and args.target == SUNBIRD:
        return deliverable_plan_message(e, DCTRACK_NAME)
    dcim = _dcim(args) if args is not None else NETBOX
    if args is not None and args.command == "export":
        return deliverable_plan_message(e, dcim.name)
    if isinstance(e, RailyardPlanLimitError):
        return plan_limit_message(e, refreshing=bool(args is not None and args.project), places=f"{dcim.place}s")
    options = upgrade_options(e.required_plans, e.project_pass)
    return f"{e}" + (f". {sentence(options)}" if options else "")


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

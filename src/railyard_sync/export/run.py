"""``sync_to_netbox`` and ``sync_to_nautobot`` — export a Railyard estate into NetBox or Nautobot over its REST API.

The command-line counterpart of the NetBox plugin's sync job (``netbox_railyard/jobs.py``): the
source is Railyard's NetBox sync document (``sync_document.py``), the target NetBox's REST API
(``netbox_rest.py``), and the ownership rules are the plugin's. ``sync_to_nautobot`` is the same flow
over the Nautobot sync document (``nautobot_document.py``) and Nautobot's REST API (``nautobot_rest.py``),
and is what the Nautobot app's export job runs, in-process.

1. Read the document and check NetBox's version.
2. Find the project's ownership tag (a real run creates it), the ``railyard_id`` custom field (a real
   run creates it when the token may, else the devices' Railyard ids are not stamped) and the design's
   own tags (created when missing).
3. Load what the project owns in NetBox, decide what to use as-is, adopt, rename or skip
   (``reconcile``), and diff.
4. A dry run stops there and reports. A real run renames, deletes owned cables that are gone from
   Railyard (only with ``allow_deletes``), creates and updates in dependency order, then deletes the
   rest in reverse order — each delete refused, and reported, when it would reach objects the sync
   does not own.

Nothing here prints; the caller (the CLI) reports the returned :class:`SyncResult`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from diffsync.enum import DiffSyncFlags

from . import nautobot_rest
from .devicetype_library import DeviceTypeLibrary
from .nautobot_document import NautobotSyncDocumentAdapter
from .nautobot_rest import (
    NautobotClient,
    NautobotError,
    NautobotPermissionError,
    NautobotRESTAdapter,
    check_version as check_nautobot_version,
)
from .netbox_rest import (
    CUSTOM_FIELD,
    ENDPOINT,
    MAX_TESTED_VERSION,
    MIN_VERSION,
    NetBoxClient,
    NetBoxError,
    NetBoxPermissionError,
    NetBoxRESTAdapter,
    NetBoxVersionError,
    parse_version,
)
from .ownership import change_lines, planned_changes
from .policy import DEFAULT_RAILYARD_URL, TAG_MARKER, TagSpec, insecure_url, ownership_tag
from .sync_document import SyncDocumentAdapter

TAG_COLOUR = "1f8bff"

log = logging.getLogger(__name__)


class SyncRefused(Exception):
    """The sync can't safely go ahead; the message says why. Nothing was written."""


@dataclass
class SyncOutcome:
    """What a sync did (or, for a dry run, would do), whichever DCIM it wrote to."""

    dry_run: bool
    project_id: str
    project_name: str
    tag: str  # the ownership tag's name
    tag_slug: str
    #: Planned changes: create / update (renames included) / delete (0 unless deletes are allowed)
    #: / no-change, as counted before anything is written.
    diff: dict[str, int] = field(default_factory=dict)
    #: The same plan per object type: {"create"|"update"|"delete": {"device": 4, …}}.
    planned: dict[str, dict[str, int]] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)  # each planned change, one line per object
    #: Applied changes (all 0 for a dry run), in total and per object type.
    created: int = 0
    updated: int = 0
    deleted: int = 0
    applied: dict[str, dict[str, int]] = field(default_factory=dict)
    referenced: list[str] = field(default_factory=list)  # existing shared objects used as they are
    conflicts: list[str] = field(default_factory=list)  # skipped: would touch objects the sync doesn't own
    dependents_skipped: int = 0
    adopted: list[str] = field(default_factory=list)  # existing components of owned devices taken over
    renamed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # deletes refused (a dry run: would be refused)
    stale: list[str] = field(default_factory=list)  # owned objects gone from Railyard, kept: deletes are off
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # writes the DCIM refused (a real run carries on)

    #: The DCIM's name, for reports.
    target_name = "DCIM"

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def target_url(self) -> str:
        return ""

    @property
    def target_version(self) -> str:
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


@dataclass
class SyncResult(SyncOutcome):
    """What a NetBox sync did."""

    netbox_url: str = ""
    netbox_version: str = ""

    target_name = "NetBox"

    @property
    def target_url(self) -> str:
        return self.netbox_url

    @property
    def target_version(self) -> str:
        return self.netbox_version


@dataclass
class NautobotSyncResult(SyncOutcome):
    """What a Nautobot sync did."""

    nautobot_url: str = ""
    nautobot_version: str = ""

    target_name = "Nautobot"

    @property
    def target_url(self) -> str:
        return self.nautobot_url

    @property
    def target_version(self) -> str:
        return self.nautobot_version


def sync_to_netbox(
    document: dict,
    netbox_url: str,
    netbox_token: str,
    *,
    dry_run: bool = True,
    allow_deletes: bool = False,
    session: Any = None,
    railyard_url: str = DEFAULT_RAILYARD_URL,
    verify: bool | str = True,
    timeout: float = 30,
    import_components: bool = False,
    devicetype_library: DeviceTypeLibrary | None = None,
    port_mappings: bool | None = None,
) -> SyncResult:
    """Sync a Railyard NetBox sync document into NetBox.

    ``railyard_url`` is the Railyard instance the document came from: with the project id it keys the
    ownership tag, so it must name the same instance as any NetBox plugin syncing the same project.
    ``import_components`` creates a new device type's component templates from the netbox-community
    devicetype-library (``devicetype_library``, fetched from GitHub unless one is given).
    ``port_mappings`` forces the front-port API shape (``None``: by NetBox version).

    Raises ``SyncDocumentError`` for a document it can't read, ``NetBoxError`` subclasses when NetBox
    can't be used at all (unreachable, token refused, unsupported version) and ``SyncRefused`` when the
    ownership tag can't be trusted or created. A write NetBox refuses is in ``SyncResult.errors``.
    """
    source = SyncDocumentAdapter(document, name="railyard")
    source.load()
    warnings = list(source.warnings)

    client = NetBoxClient(netbox_url, netbox_token, session=session, verify=verify, timeout=timeout)
    if insecure_url(client.url):
        warnings.append(f"{client.url} is not https: the NetBox token is sent in clear text. Use https.")
    client.status()
    log.info(
        "Syncing %r into NetBox %s (%s)%s…",
        source.project_name,
        client.url,
        client.version or "unknown version",
        " as a dry run" if dry_run else "",
    )
    version = parse_version(client.version)
    if version < MIN_VERSION:
        raise NetBoxVersionError(f"NetBox {client.version or '(unknown version)'} is not supported: 4.0 or later.")
    if version > MAX_TESTED_VERSION:
        warnings.append(f"NetBox {client.version} is newer than this railyard-sync was tested with (4.6).")

    spec = ownership_tag(railyard_url, source.project_id, source.project_name)
    tag = _find_ownership_tag(client, spec)
    if not dry_run:
        tag = _ensure_ownership_tag(client, spec, tag, warnings)
    elif tag is None:
        warnings.append(
            f"Ownership tag {spec.name!r} doesn't exist yet: this sync owns nothing in NetBox, so nothing can "
            "be updated or deleted. A real run creates it."
        )

    custom_field = _ensure_custom_field(client, dry_run, warnings)
    if not custom_field:
        for device in source.get_all("device"):
            device.railyard_id = ""
    user_tags = _ensure_user_tags(client, source, spec, dry_run, warnings)

    if devicetype_library is None and import_components:
        devicetype_library = DeviceTypeLibrary()
    target = NetBoxRESTAdapter(
        client,
        tag=tag,
        tag_slug=spec.slug,
        user_tags=user_tags,
        custom_field=custom_field,
        port_mappings=port_mappings,
        import_components=import_components,
        devicetype_library=devicetype_library,
        name="netbox",
    )
    result = SyncResult(
        dry_run=dry_run,
        netbox_url=client.url,
        netbox_version=client.version,
        project_id=source.project_id,
        project_name=source.project_name,
        tag=spec.name,
        tag_slug=spec.slug,
        warnings=warnings,
    )
    return _run(source, target, result, allow_deletes=allow_deletes, endpoints=ENDPOINT, product="NetBox")


def _run(source, target, result: SyncOutcome, *, allow_deletes: bool, endpoints: dict, product: str):
    """Load what the project owns, reconcile, diff and — unless a dry run — apply: renames, owned cable deletes,
    creates and updates in dependency order, then the other deletes, dependents first."""
    started = time.monotonic()
    log.info("Reading what the estate owns in %s (tag %s)…", product, result.tag)
    target.load()
    report = target.reconcile(source, allow_deletes=allow_deletes)
    diff = target.diff_from(source, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
    candidates = target.delete_candidates(source)

    summary = diff.summary()
    log.info(
        "Planned in %.1fs: %d to create, %d to update, %d stale or to delete, %d unchanged",
        time.monotonic() - started,
        summary.get("create", 0),
        summary.get("update", 0) + len(target.renames),
        len(candidates),
        summary.get("no-change", 0),
    )
    result.diff = {
        "create": summary.get("create", 0),
        "update": summary.get("update", 0) + len(target.renames),
        "delete": len(candidates) if allow_deletes else 0,
        "no-change": summary.get("no-change", 0),
    }
    result.planned = planned_changes(diff, len(target.renames), candidates if allow_deletes else [])
    result.changes = [f"rename: device {old} → {new}" for old, new, _ in target.renames] + change_lines(diff)
    if allow_deletes:
        result.changes += [f"delete: {m.get_type()} [{m.get_unique_id()}]" for m in candidates]
    else:
        result.stale = [f"{m.get_type()} [{m.get_unique_id()}]" for m in candidates]

    if result.dry_run:
        if allow_deletes:
            scheduled = {(endpoints[m.get_type()], m.nb_id) for m in candidates}
            for model in candidates:
                if reasons := target.preview_delete(model, scheduled):
                    report.kept.append(f"{model.get_type()} {model.get_unique_id()}: {'; '.join(reasons)}")
        return _finish(result, target)

    started = time.monotonic()
    log.info("Writing to %s…", product)
    target.apply_renames()
    deletes = candidates if allow_deletes else []
    for model in deletes:  # cables first, so a re-patched port is free before its new cable is created
        if model.get_type() == "cable":
            model.delete()
    target.sync_from(source, flags=DiffSyncFlags.SKIP_UNMATCHED_DST | DiffSyncFlags.CONTINUE_ON_FAILURE, diff=diff)
    for model in deletes:  # then the rest, dependents before what they depend on
        if model.get_type() != "cable":
            model.delete()
    target.after_sync()  # undo what the writes needed for a while (rear port positions parked on)
    result = _finish(result, target)
    log.info(
        "Wrote to %s in %.1fs: %d created, %d updated, %d deleted, %d refused",
        product,
        time.monotonic() - started,
        result.created,
        result.updated,
        result.deleted,
        len(result.errors),
    )
    return result


def _finish(result: SyncOutcome, target: Any) -> SyncOutcome:
    report = target.report
    result.referenced = report.referenced
    result.conflicts = report.conflicts
    result.dependents_skipped = report.dependents_skipped
    result.adopted = report.adopted
    result.renamed = report.renamed
    result.kept = report.kept
    result.warnings += report.warnings
    result.errors = report.errors
    result.applied = {action: dict(counts) for action, counts in target.counts.items()}
    result.created = sum(target.counts["create"].values())
    result.updated = sum(target.counts["update"].values())
    result.deleted = sum(target.counts["delete"].values())
    return result


# ---- prerequisites -------------------------------------------------------------------------------


def _find_ownership_tag(client: NetBoxClient, spec: TagSpec) -> dict | None:
    """The project's ownership tag if it exists, verified, without creating or changing anything.

    A tag with the expected slug but a different description isn't ours (an operator's tag, or a tag
    someone edited), so the sync refuses to treat the objects carrying it as Railyard-owned.
    """
    tag = client.first("extras/tags", slug=spec.slug)
    if tag is not None and (tag.get("description") or "") != spec.description:
        raise SyncRefused(
            f"Tag {tag.get('name')!r} (slug {spec.slug!r}) is not this project's Railyard ownership tag: its "
            f"description has changed. If it is, restore the description to {spec.description!r}."
        )
    return tag


def _ensure_ownership_tag(client: NetBoxClient, spec: TagSpec, tag: dict | None, warnings: list[str]) -> dict:
    """Get or create the project's ownership tag. Its name follows project renames when it can."""
    if tag is None:
        if client.first("extras/tags", name=spec.name) is not None:
            raise SyncRefused(f"A different NetBox tag is already named {spec.name!r}; rename it and re-run.")
        data = {"name": spec.name, "slug": spec.slug, "description": spec.description, "color": TAG_COLOUR}
        try:
            return client.create("extras/tags", data)
        except NetBoxPermissionError:
            raise SyncRefused(
                f"The NetBox token may not create this project's ownership tag {spec.name!r} (extras.add_tag): "
                "ask an administrator to run the first sync, or to grant it."
            ) from None
        except NetBoxError as exc:
            raise SyncRefused(
                client.scrub(f"NetBox refused to create the ownership tag {spec.name!r}: {exc}")
            ) from None
    if tag.get("name") != spec.name and client.first("extras/tags", name=spec.name) is None:
        try:
            tag = client.update("extras/tags", tag["id"], {"name": spec.name})
        except NetBoxError:
            warnings.append(f"Could not rename the ownership tag {tag.get('name')!r} to {spec.name!r}; kept its name.")
    return tag


def _ensure_custom_field(client: NetBoxClient, dry_run: bool, warnings: list[str]) -> bool:
    """Whether devices can carry the ``railyard_id`` custom field (their stable Railyard identity).

    A real run creates the field (text, on dcim.device) when it is missing and the token may; otherwise
    the devices' Railyard ids are not stamped, which only costs rename detection. A dry run never
    writes: it reports what a real run would do and diffs as if the field existed.
    """
    try:
        cf = client.first("extras/custom-fields", name=CUSTOM_FIELD)
    except NetBoxError as exc:
        warnings.append(client.scrub(f"Could not read the {CUSTOM_FIELD!r} custom field ({exc}); ids not stamped."))
        return False
    if cf is not None:
        key = "object_types" if "object_types" in cf else "content_types"
        types = [str(t) for t in cf.get(key) or []]
        if "dcim.device" in types:
            return True
        if dry_run:
            warnings.append(f"The {CUSTOM_FIELD!r} custom field isn't enabled on devices; a real run enables it.")
            return True
        try:
            client.update("extras/custom-fields", cf["id"], {key: types + ["dcim.device"]})
            return True
        except NetBoxError as exc:
            warnings.append(
                client.scrub(
                    f"The {CUSTOM_FIELD!r} custom field isn't enabled on devices and couldn't be ({exc}); "
                    "devices are synced without their Railyard id."
                )
            )
            return False
    if dry_run:
        warnings.append(f"The {CUSTOM_FIELD!r} custom field doesn't exist yet; a real run creates it if allowed.")
        return True
    data = {
        "name": CUSTOM_FIELD,
        "type": "text",
        "label": "Railyard ID",
        "description": "Railyard placement id",
        "object_types": ["dcim.device"],
    }
    try:
        client.create("extras/custom-fields", data)
        return True
    except NetBoxError as exc:
        why = "the token may not create custom fields" if isinstance(exc, NetBoxPermissionError) else str(exc)
        warnings.append(
            client.scrub(
                f"The {CUSTOM_FIELD!r} custom field doesn't exist and couldn't be created ({why}): devices "
                "are synced without their Railyard id. Ask an administrator to create it (text, on "
                "dcim.device)."
            )
        )
        return False


def _ensure_user_tags(
    client: NetBoxClient, source: SyncDocumentAdapter, spec: TagSpec, dry_run: bool, warnings: list[str]
) -> dict[str, int]:
    """Find (a real run: create) the tags the design puts on racks and devices; slug -> NetBox id.

    A tag that can't be used is dropped from the objects (with a warning) so the diff doesn't keep
    asking for it. A tag with the ownership tag's slug is never a user tag.
    """
    declared = {t.slug: t for t in source.tags}
    wanted = sorted({slug for type_name in ("rack", "device") for m in source.get_all(type_name) for slug in m.tags})
    ids: dict[str, int] = {}
    unusable: set[str] = set()
    for slug in wanted:
        if slug == spec.slug:
            warnings.append(f"Tag {slug!r} is this project's ownership tag; it is not synced as a design tag.")
            unusable.add(slug)
            continue
        found = client.first("extras/tags", slug=slug)
        if found is not None:
            ids[slug] = found["id"]
            continue
        tag = declared.get(slug)
        if dry_run:
            warnings.append(f"Tag {slug!r} doesn't exist in NetBox; a real run creates it.")
            continue
        data = {"name": tag.name if tag else slug, "slug": slug, "color": tag.color if tag else "9e9e9e"}
        try:
            ids[slug] = client.create("extras/tags", data)["id"]
        except NetBoxError as exc:
            warnings.append(client.scrub(f"Tag {slug!r} couldn't be created ({exc}); objects are synced without it."))
            unusable.add(slug)
    if unusable:
        for type_name in ("rack", "device"):
            for model in source.get_all(type_name):
                model.tags = [s for s in model.tags if s not in unusable]
    return ids


# ---- Nautobot ----------------------------------------------------------------------------------------

#: The description of a tag the sync creates for the design's racks and devices; the sync may enable such a tag for
#: more content types later, but never changes a tag someone else made.
DESIGN_TAG_DESCRIPTION = f"Created by the Railyard sync for a design's racks and devices. {TAG_MARKER} design-tag"


def sync_to_nautobot(
    document: dict,
    nautobot_url: str = "",
    nautobot_token: str = "",
    *,
    client: NautobotClient | None = None,
    dry_run: bool = True,
    allow_deletes: bool = False,
    session: Any = None,
    railyard_url: str = DEFAULT_RAILYARD_URL,
    verify: bool | str = True,
    timeout: float = 30,
) -> NautobotSyncResult:
    """Sync a Railyard Nautobot sync document into Nautobot 2.x.

    ``railyard_url`` is the Railyard instance the document came from: with the project id it keys the ownership
    tag, so it must name the same instance as any other sync of the same project (the Nautobot app's, say).
    ``client`` is a ready :class:`NautobotClient` (the Nautobot app passes one that calls its own API in-process);
    otherwise one is made for ``nautobot_url`` and ``nautobot_token``.

    Raises ``SyncDocumentError`` for a document it can't read, ``NautobotError`` subclasses when Nautobot can't be
    used at all (unreachable, token refused, unsupported version) and ``SyncRefused`` when the ownership tag can't
    be trusted or created. A write Nautobot refuses is in ``NautobotSyncResult.errors``.
    """
    source = NautobotSyncDocumentAdapter(document, name="railyard")
    source.load()
    warnings = list(source.warnings)

    if client is None:
        client = NautobotClient(nautobot_url, nautobot_token, session=session, verify=verify, timeout=timeout)
        if insecure_url(client.url):
            warnings.append(f"{client.url} is not https: the Nautobot token is sent in clear text. Use https.")
    client.status()
    log.info(
        "Syncing %r into Nautobot %s (%s)%s…",
        source.project_name,
        client.url,
        client.version or "unknown version",
        " as a dry run" if dry_run else "",
    )
    check_nautobot_version(client, warnings)

    spec = ownership_tag(railyard_url, source.project_id, source.project_name)
    tag = _find_nautobot_tag(client, spec)
    if not dry_run:
        tag = _ensure_nautobot_tag(client, spec, tag, warnings)
    elif tag is None:
        warnings.append(
            f"Ownership tag {spec.name!r} doesn't exist yet: this sync owns nothing in Nautobot, so nothing can be "
            "updated or deleted. A real run creates it."
        )
    elif missing := sorted(set(nautobot_rest.TAG_CONTENT_TYPES) - set(nautobot_rest.content_types(tag))):
        warnings.append(f"The ownership tag isn't enabled for {', '.join(missing)}; a real run enables it.")

    custom_field, _ = _ensure_nautobot_field(
        client,
        nautobot_rest.CUSTOM_FIELD,
        "Railyard ID",
        "Railyard placement id",
        ["dcim.device"],
        dry_run,
        warnings,
        "devices are synced without their Railyard id (so a rename in Railyard recreates the device)",
    )
    if not custom_field:
        for device in source.get_all("device"):
            device.railyard_id = ""
    owner_field, owner_types = _ensure_nautobot_field(
        client,
        nautobot_rest.OWNER_FIELD,
        "Railyard owner",
        f"The Railyard project that owns this object. {TAG_MARKER}",
        nautobot_rest.OWNER_CONTENT_TYPES,
        dry_run,
        warnings,
        "location types, statuses, manufacturers and roles the sync creates are not marked as the project's, so "
        "it will never update or delete them",
    )
    user_tags = _ensure_nautobot_user_tags(client, source, spec, dry_run, warnings)

    target = NautobotRESTAdapter(
        client,
        tag=tag,
        tag_slug=spec.slug,
        user_tags=user_tags,
        custom_field=custom_field,
        owner_field=owner_field,
        owner_types=owner_types,
        name="nautobot",
    )
    result = NautobotSyncResult(
        dry_run=dry_run,
        nautobot_url=client.url,
        nautobot_version=client.version,
        project_id=source.project_id,
        project_name=source.project_name,
        tag=spec.name,
        tag_slug=spec.slug,
        warnings=warnings,
    )
    return _run(
        source, target, result, allow_deletes=allow_deletes, endpoints=nautobot_rest.ENDPOINT, product="Nautobot"
    )


def _find_nautobot_tag(client: NautobotClient, spec: TagSpec) -> dict | None:
    """The project's ownership tag, recognised by its exact description (Nautobot tags have no slug), without
    creating or changing anything. A tag with the ownership tag's name but another description is not ours."""
    tags = client.list("extras/tags")
    ours = [t for t in tags if (t.get("description") or "") == spec.description]
    if len(ours) > 1:
        raise SyncRefused(
            f"{len(ours)} Nautobot tags carry this project's ownership description ({spec.description!r}); keep one "
            "and re-run."
        )
    return ours[0] if ours else None


def _ensure_nautobot_tag(client: NautobotClient, spec: TagSpec, tag: dict | None, warnings: list[str]) -> dict:
    """Get or create the project's ownership tag, enabled for every model the sync tags. Its name follows project
    renames when it can."""
    wanted = nautobot_rest.TAG_CONTENT_TYPES
    if tag is None:
        if client.first("extras/tags", name=spec.name) is not None:
            raise SyncRefused(f"A different Nautobot tag is already named {spec.name!r}; rename it and re-run.")
        data = {"name": spec.name, "description": spec.description, "color": TAG_COLOUR, "content_types": wanted}
        try:
            return client.create("extras/tags", data)
        except NautobotPermissionError:
            raise SyncRefused(
                f"The Nautobot token may not create this project's ownership tag {spec.name!r} (extras.add_tag): ask "
                "an administrator to run the first sync, or to grant it."
            ) from None
        except NautobotError as exc:
            raise SyncRefused(
                client.scrub(f"Nautobot refused to create the ownership tag {spec.name!r}: {exc}")
            ) from None
    patch: dict[str, Any] = {}
    have = nautobot_rest.content_types(tag)
    if set(wanted) - set(have):
        patch["content_types"] = sorted(set(have) | set(wanted))
    if tag.get("name") != spec.name and client.first("extras/tags", name=spec.name) is None:
        patch["name"] = spec.name
    if patch:
        try:
            tag = client.update("extras/tags", tag["id"], patch)
        except NautobotError as exc:
            if "content_types" in patch:
                raise SyncRefused(
                    client.scrub(
                        f"The ownership tag {tag.get('name')!r} couldn't be enabled for every synced model: {exc}"
                    )
                ) from None
            warnings.append(f"Could not rename the ownership tag {tag.get('name')!r} to {spec.name!r}; kept its name.")
    return tag


def _ensure_nautobot_field(
    client: NautobotClient,
    key: str,
    label: str,
    description: str,
    wanted: list[str],
    dry_run: bool,
    warnings: list[str],
    without: str,
) -> tuple[bool, set[str]]:
    """Whether a text custom field ``key`` is available on the ``wanted`` content types, and the content types it is
    enabled for in Nautobot now (Nautobot filters by a custom field only on those; asking another is a 400).

    A real run creates or enables the field when it may; otherwise ``without`` says what the sync does without it.
    A dry run never writes: it reports what a real run would do and plans as if the field were available."""
    try:
        found = [f for f in client.list("extras/custom-fields") if (f.get("key") or f.get("name")) == key]
    except NautobotError as exc:
        warnings.append(client.scrub(f"Could not read the {key!r} custom field ({exc}): {without}."))
        return False, set()
    if found:
        field_ = found[0]
        have = nautobot_rest.content_types(field_)
        missing = sorted(set(wanted) - set(have))
        if not missing:
            return True, set(have)
        if dry_run:
            warnings.append(f"The {key!r} custom field isn't enabled for {', '.join(missing)}; a real run enables it.")
            return True, set(have)
        try:
            client.update("extras/custom-fields", field_["id"], {"content_types": sorted(set(have) | set(wanted))})
            return True, set(have) | set(wanted)
        except NautobotError as exc:
            warnings.append(
                client.scrub(
                    f"The {key!r} custom field isn't enabled for {', '.join(missing)} and couldn't be "
                    f"({exc}): {without}."
                )
            )
            return False, set(have)
    if dry_run:
        warnings.append(f"The {key!r} custom field doesn't exist yet; a real run creates it if allowed.")
        return True, set()
    data = {
        "key": key,
        "label": label,
        "type": "text",
        "description": description,
        "content_types": list(wanted),
        "filter_logic": "exact",
    }
    try:
        client.create("extras/custom-fields", data)
        return True, set(wanted)
    except NautobotError as exc:
        why = "the token may not create custom fields" if isinstance(exc, NautobotPermissionError) else str(exc)
        warnings.append(
            client.scrub(
                f"The {key!r} custom field doesn't exist and couldn't be created ({why}): {without}. Ask an "
                f"administrator to create it (text, on {', '.join(wanted)})."
            )
        )
        return False, set()


def _ensure_nautobot_user_tags(
    client: NautobotClient, source: NautobotSyncDocumentAdapter, spec: TagSpec, dry_run: bool, warnings: list[str]
) -> dict[str, str]:
    """Find (a real run: create) the tags the design puts on racks and devices; name -> Nautobot id.

    Nautobot applies a tag only to the content types it is enabled for. A tag the sync created is enabled for more
    when the design needs it; a tag someone else made is used as it is, and dropped (with a warning) from the
    objects it isn't enabled for, so the diff doesn't keep asking for it."""
    needs: dict[str, set[str]] = {}
    for type_name, ct in (("rack", "dcim.rack"), ("device", "dcim.device")):
        for model in source.get_all(type_name):
            for name in model.tags:
                needs.setdefault(name, set()).add(ct)
    ids: dict[str, str] = {}
    unusable: dict[str, set[str]] = {}  # tag name -> content types it can't be used on
    for name in sorted(needs):
        wanted = needs[name]
        if name == spec.name:
            warnings.append(f"Tag {name!r} is this project's ownership tag; it is not synced as a design tag.")
            unusable[name] = wanted
            continue
        found = client.first("extras/tags", name=name)
        if found is None:
            if dry_run:
                warnings.append(f"Tag {name!r} doesn't exist in Nautobot; a real run creates it.")
                continue
            data = {
                "name": name,
                "color": "9e9e9e",
                "description": DESIGN_TAG_DESCRIPTION,
                "content_types": sorted(wanted),
            }
            try:
                ids[name] = str(client.create("extras/tags", data)["id"])
            except NautobotError as exc:
                warnings.append(
                    client.scrub(f"Tag {name!r} couldn't be created ({exc}); objects are synced without it.")
                )
                unusable[name] = wanted
            continue
        ids[name] = str(found["id"])
        missing = wanted - set(nautobot_rest.content_types(found))
        if not missing:
            continue
        if (found.get("description") or "") == DESIGN_TAG_DESCRIPTION:
            if dry_run:
                warnings.append(f"Tag {name!r} isn't enabled for {', '.join(sorted(missing))}; a real run enables it.")
                continue
            try:
                cts = sorted(set(nautobot_rest.content_types(found)) | wanted)
                client.update("extras/tags", found["id"], {"content_types": cts})
                continue
            except NautobotError as exc:
                warnings.append(
                    client.scrub(f"Tag {name!r} couldn't be enabled for {', '.join(sorted(missing))} ({exc}).")
                )
        else:
            warnings.append(
                f"Tag {name!r} exists in Nautobot but isn't enabled for {', '.join(sorted(missing))}; the objects of "
                "those types are synced without it (enable it for them in Nautobot to sync it)."
            )
        unusable[name] = missing
    if unusable:
        for type_name, ct in (("rack", "dcim.rack"), ("device", "dcim.device")):
            for model in source.get_all(type_name):
                model.tags = [t for t in model.tags if ct not in unusable.get(t, set())]
    return ids

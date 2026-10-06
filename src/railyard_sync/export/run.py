"""``sync_to_netbox`` — export a Railyard estate into NetBox over its REST API.

The command-line counterpart of the NetBox plugin's sync job (``netbox_railyard/jobs.py``): the
source is Railyard's NetBox sync document (``sync_document.py``), the target NetBox's REST API
(``netbox_rest.py``), and the ownership rules are the plugin's.

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
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from diffsync.enum import DiffSyncFlags

from .devicetype_library import DeviceTypeLibrary
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
from .policy import DEFAULT_RAILYARD_URL, TagSpec, insecure_url, ownership_tag
from .sync_document import SyncDocumentAdapter

TAG_COLOUR = "1f8bff"

log = logging.getLogger(__name__)


class SyncRefused(Exception):
    """The sync can't safely go ahead; the message says why. Nothing was written."""


@dataclass
class SyncResult:
    """What a sync did (or, for a dry run, would do)."""

    dry_run: bool
    netbox_url: str
    netbox_version: str
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
    errors: list[str] = field(default_factory=list)  # writes NetBox refused (a real run carries on)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


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
    started = time.monotonic()
    log.info("Reading what the estate owns in NetBox (tag %s)…", spec.name)
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
    result = SyncResult(
        dry_run=dry_run,
        netbox_url=client.url,
        netbox_version=client.version,
        project_id=source.project_id,
        project_name=source.project_name,
        tag=spec.name,
        tag_slug=spec.slug,
        diff={
            "create": summary.get("create", 0),
            "update": summary.get("update", 0) + len(target.renames),
            "delete": len(candidates) if allow_deletes else 0,
            "no-change": summary.get("no-change", 0),
        },
        planned=_planned(diff, len(target.renames), candidates if allow_deletes else []),
        changes=[f"rename: device {old} → {new}" for old, new, _ in target.renames] + _changes(diff),
        warnings=warnings,
    )
    if allow_deletes:
        result.changes += [f"delete: {m.get_type()} [{m.get_unique_id()}]" for m in candidates]
    else:
        result.stale = [f"{m.get_type()} [{m.get_unique_id()}]" for m in candidates]

    if dry_run:
        if allow_deletes:
            scheduled = {(ENDPOINT[m.get_type()], m.nb_id) for m in candidates}
            for model in candidates:
                if reasons := target.preview_delete(model, scheduled):
                    report.kept.append(f"{model.get_type()} {model.get_unique_id()}: {'; '.join(reasons)}")
        return _finish(result, target)

    started = time.monotonic()
    log.info("Writing to NetBox…")
    target.apply_renames()
    deletes = candidates if allow_deletes else []
    for model in deletes:  # cables first, so a re-patched port is free before its new cable is created
        if model.get_type() == "cable":
            model.delete()
    target.sync_from(source, flags=DiffSyncFlags.SKIP_UNMATCHED_DST | DiffSyncFlags.CONTINUE_ON_FAILURE, diff=diff)
    for model in deletes:  # then the rest, dependents before what they depend on
        if model.get_type() != "cable":
            model.delete()
    result = _finish(result, target)
    log.info(
        "Wrote to NetBox in %.1fs: %d created, %d updated, %d deleted, %d refused",
        time.monotonic() - started,
        result.created,
        result.updated,
        result.deleted,
        len(result.errors),
    )
    return result


def _finish(result: SyncResult, target: NetBoxRESTAdapter) -> SyncResult:
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


def _planned(diff, renames: int, deletes: list) -> dict[str, dict[str, int]]:
    """The planned creates, updates (renames included, as device updates) and deletes per object type."""
    planned: dict[str, Counter] = {"create": Counter(), "update": Counter(), "delete": Counter()}

    def walk(elements) -> None:
        for el in elements:
            if el.action in ("create", "update"):
                planned[el.action][el.type] += 1
            walk(el.get_children())

    walk(diff.get_children())
    if renames:
        planned["update"]["device"] += renames
    for model in deletes:
        planned["delete"][model.get_type()] += 1
    return {action: dict(counts) for action, counts in planned.items()}


def _changes(diff) -> list[str]:
    """One line per create/update in the diff, an update with what changes."""
    lines: list[str] = []

    def walk(elements) -> None:
        for el in elements:
            if el.action in ("create", "update"):
                ident = " ".join(f"{k}={v}" for k, v in (el.keys or {}).items())
                line = f"{el.action}: {el.type} [{ident}]"
                if el.action == "update":
                    d = el.get_attrs_diffs()
                    old, new = d.get("-", {}), d.get("+", {})
                    line += " — " + ", ".join(f"{k}: {old.get(k)!r}→{new.get(k)!r}" for k in new)
                lines.append(line)
            walk(el.get_children())

    walk(diff.get_children())
    return lines


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

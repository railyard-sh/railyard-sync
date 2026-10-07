"""The parts of an export target's ownership rules that do not depend on how it reaches NetBox.

Two targets apply the same rules: :mod:`railyard_sync.export.netbox_rest` over NetBox's REST API, and the
NetBox plugin's ORM target (``netbox_railyard/target.py``). What they share lives here, so a NetBox synced by
one behaves the same when synced by the other:

- :class:`OwnershipReport`: what a sync decided about objects it does not own.
- :class:`OwnershipMixin`, mixed into a target :class:`diffsync.Adapter` whose models carry ``nb_id`` (the
  NetBox primary key of the owned object behind each loaded model):

  - ``delete_candidates(source)``: owned objects gone from Railyard, dependents first.
  - ``detect_renames(source)``: owned devices whose Railyard id (``railyard_id``) now has another name are
    re-keyed, with their components and cables, so the diff lines up; ``renames`` lists them for the
    target to rename in NetBox. The target supplies ``device_named(site, name)``: the NetBox id of the
    device of that name in that site, owned or not, or ``None``.

- :func:`planned_changes` and :func:`change_lines`: a diff (plus renames and deletes) as counts per object type
  and as one line per object.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: The (device, name) components the export models, in creation order.
COMPONENT_TYPES = ("interface", "rear_port", "front_port", "power_outlet", "power_port")


@dataclass
class OwnershipReport:
    """What the sync decided about objects it doesn't own, and what went wrong, for the result."""

    referenced: list[str] = field(default_factory=list)  # existing shared objects used as-is
    conflicts: list[str] = field(default_factory=list)  # skipped: would need an object the sync doesn't own
    dependents_skipped: int = 0  # components/cables skipped because what they hang off was skipped
    adopted: list[str] = field(default_factory=list)  # components on owned devices brought under the tag
    renamed: list[str] = field(default_factory=list)  # owned devices renamed in Railyard
    kept: list[str] = field(default_factory=list)  # deletes refused
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # writes NetBox refused (the REST target carries on)


class OwnershipMixin:
    """Ownership bookkeeping for a target adapter (see the module docstring). Expects ``self.report`` (an
    :class:`OwnershipReport`), ``self.renames`` (a list) and ``self._add(model)``."""

    renames: list[tuple[str, str, Any]]

    def device_named(self, site: str, name: str) -> Any:  # pragma: no cover - the targets implement it
        raise NotImplementedError

    def delete_candidates(self, source) -> list:
        """Owned objects no longer in Railyard, in a safe deletion order (dependents first)."""
        out = []
        for type_name in reversed(self.top_level):
            out += [m for m in self.get_all(type_name) if source.get_or_none(type_name, m.get_unique_id()) is None]
        return out

    def detect_renames(self, source) -> None:
        """Owned devices whose Railyard id now has another name: re-key them (with their components and
        cables) under the new name, so the diff lines up, and remember the rename in ``renames``."""
        by_rid = {d.railyard_id: d for d in source.get_all("device") if d.railyard_id}
        for dev in list(self.get_all("device")):
            new = by_rid.get(dev.railyard_id) if dev.railyard_id else None
            if new is None or new.name == dev.name or dev.nb_id is None:
                continue
            if source.get_or_none("device", dev.name) is not None or self.get_or_none("device", new.name) is not None:
                continue  # names swapped between devices: leave it to create/update/delete
            existing = self.device_named(new.site, new.name)
            if existing is not None and existing != dev.nb_id:
                continue  # the new name is taken in NetBox: reconcile reports the conflict
            self.rekey_device(dev.name, new.name)
            self.renames.append((dev.name, new.name, dev.nb_id))
            self.report.renamed.append(f"device {dev.name} → {new.name}")

    def rekey_device(self, old: str, new: str) -> None:
        """Re-key a loaded device, its components and its cables from ``old`` to ``new``."""

        def copy(model, **changes):
            data = {**model.get_identifiers(), **model.get_attrs(), "nb_id": model.nb_id, **changes}
            return type(model)(**data)

        moved = []
        for type_name in ("device", *COMPONENT_TYPES, "cable"):
            for model in list(self.get_all(type_name)):
                changes = {}
                if type_name == "device" and model.name == old:
                    changes["name"] = new
                elif type_name in COMPONENT_TYPES and model.device == old:
                    changes["device"] = new
                elif type_name == "cable":
                    changes = {f"{s}_device": new for s in ("a", "b") if getattr(model, f"{s}_device") == old}
                if changes:
                    self.remove(model)
                    moved.append(copy(model, **changes))
        for model in moved:
            self._add(model)


def planned_changes(diff, renames: int, deletes: list) -> dict[str, dict[str, int]]:
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


def change_lines(diff) -> list[str]:
    """One line per create/update in the diff (``create: device [name=SW-1]``), an update with what changes."""
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

"""What an import did: counts per kind, what was skipped and why, and what Railyard does not model.

The report is the importer's honesty contract. Nothing a DCIM holds is dropped silently: an object
Railyard cannot express is listed in :attr:`ImportReport.skipped` with the reason, a value that had to
change to fit Railyard's limits (a long name, a fractional U, a duplicate rack name) is a warning, and
data with no Railyard field at all (power panels and feeds, device statuses) is kept in
:attr:`ImportReport.unmodelled`, which the builder also writes to ``project.meta.railyardSync``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SkippedItem:
    """One DCIM object the import left out."""

    kind: str  # the report kind: "devices", "cables", "ports", …
    id: str  # the source object's primary key
    name: str
    reason: str


@dataclass
class ImportReport:
    """The outcome of :func:`railyard_sync.importer.build_project`."""

    imported: dict[str, int] = field(default_factory=dict)
    skipped: list[SkippedItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unmodelled: dict[str, Any] = field(default_factory=dict)

    def count(self, kind: str, n: int = 1) -> None:
        """Record ``n`` more imported objects of ``kind``."""
        self.imported[kind] = self.imported.get(kind, 0) + n

    def skip(self, kind: str, source_id: str, name: str, reason: str) -> None:
        """Record a DCIM object that was not imported."""
        self.skipped.append(SkippedItem(kind=kind, id=str(source_id), name=name or "", reason=reason))

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def skipped_counts(self) -> dict[str, int]:
        """Skipped objects per kind."""
        out: dict[str, int] = {}
        for item in self.skipped:
            out[item.kind] = out.get(item.kind, 0) + 1
        return out

    def counts(self) -> dict[str, dict[str, int]]:
        """``{kind: {"imported": n, "skipped": m}}`` for every kind either side mentions, sorted."""
        skipped = self.skipped_counts
        kinds = sorted(set(self.imported) | set(skipped))
        return {kind: {"imported": self.imported.get(kind, 0), "skipped": skipped.get(kind, 0)} for kind in kinds}

    def skipped_reasons(self) -> dict[str, dict[str, int]]:
        """Skipped objects grouped by kind and reason, for a compact summary."""
        out: dict[str, dict[str, int]] = {}
        for item in self.skipped:
            reasons = out.setdefault(item.kind, {})
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "skipped": [asdict(item) for item in self.skipped],
            "warnings": list(self.warnings),
            "unmodelled": self.unmodelled,
        }

    def summary_lines(self) -> list[str]:
        """A short human-readable summary: one line per kind, then the skip reasons."""
        lines = []
        for kind, counts in self.counts().items():
            line = f"{kind}: {counts['imported']} imported"
            if counts["skipped"]:
                line += f", {counts['skipped']} skipped"
            lines.append(line)
        for kind, reasons in self.skipped_reasons().items():
            for reason, n in sorted(reasons.items()):
                lines.append(f"  skipped {n} {kind}: {reason}")
        if self.warnings:
            lines.append(f"{len(self.warnings)} warning(s)")
        return lines

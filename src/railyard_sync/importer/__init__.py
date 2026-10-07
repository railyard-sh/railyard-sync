"""DCIM → Railyard: build a Railyard project from a DCIM snapshot (:func:`build_project`), report what
it left out, and re-import it later as a baseline (:func:`merge`). :class:`ImportRun` is the whole flow,
fetch to save, as the CLI and the NetBox plugin run it."""

from .build import BuildResult, build_project
from .catalogue import CatalogueLookup, RailyardCatalogue
from .merge import FIELD_OWNERSHIP, Conflict, KindDiff, MergeDiff, MergeResult, Retained, merge
from .report import ImportReport, SkippedItem
from .run import ImportOutcome, ImportRefused, ImportRun

__all__ = [
    "FIELD_OWNERSHIP",
    "BuildResult",
    "CatalogueLookup",
    "Conflict",
    "ImportOutcome",
    "ImportRefused",
    "ImportReport",
    "ImportRun",
    "KindDiff",
    "MergeDiff",
    "MergeResult",
    "RailyardCatalogue",
    "Retained",
    "SkippedItem",
    "build_project",
    "merge",
]

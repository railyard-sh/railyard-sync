"""Importer test helpers: build a project and check it against Railyard's latest Project schema.

Every project a test builds goes through :func:`build`, which validates it against
``tests/fixtures/project.schema.json`` (a copy of ``railyard/schema/project.schema.json``; refresh it
when Railyard's schema changes) before the test sees it.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import jsonschema

from railyard_sync.importer import BuildResult, build_project

SCHEMA = json.loads((pathlib.Path(__file__).parents[1] / "fixtures" / "project.schema.json").read_text())
IMPORTED_AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

_validator = jsonschema.Draft202012Validator(SCHEMA)


def assert_valid(project: dict) -> None:
    errors = sorted(_validator.iter_errors(project), key=lambda e: list(e.absolute_path))
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors[:20])


def build(snapshot, **kwargs) -> BuildResult:
    """``build_project`` with test defaults, schema-validating the result."""
    kwargs.setdefault("project_id", "prj_ldn1")
    kwargs.setdefault("name", "LDN1 baseline")
    kwargs.setdefault("imported_at", IMPORTED_AT)
    result = build_project(snapshot, **kwargs)
    assert_valid(result.project)
    json.dumps(result.project)  # plain JSON, ready to PUT
    return result

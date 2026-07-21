from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Sequence

import yaml
from pydantic import ValidationError

from vla_data_juicer_agents.evaluation.models import EvaluationCase


class CaseLoadError(ValueError):
    """Raised when a versioned evaluation case cannot be loaded."""


_SUITE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def default_cases_root() -> Path:
    return Path(__file__).resolve().parents[3] / "evals" / "cases"


def load_suite(cases_root: str | Path, suite: str) -> list[EvaluationCase]:
    if _SUITE_PATTERN.fullmatch(suite) is None:
        raise CaseLoadError(f"invalid evaluation suite name {suite!r}")
    suite_dir = Path(cases_root) / suite
    paths = sorted((*suite_dir.glob("*.yaml"), *suite_dir.glob("*.yml")))
    if not paths:
        raise CaseLoadError(f"evaluation suite {suite!r} has no case files in {suite_dir}")

    cases: list[EvaluationCase] = []
    seen: dict[str, Path] = {}
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise CaseLoadError(f"failed to read evaluation case {path}: {error}") from error
        if not isinstance(raw, dict):
            raise CaseLoadError(f"evaluation case {path} must contain one YAML mapping")
        try:
            case = EvaluationCase.model_validate(raw)
        except ValidationError as error:
            raise CaseLoadError(f"invalid evaluation case {path}: {error}") from error
        if case.suite != suite:
            raise CaseLoadError(
                f"evaluation case {path} declares suite {case.suite!r}, expected {suite!r}",
            )
        if case.id in seen:
            raise CaseLoadError(
                f"duplicate evaluation case id {case.id!r} in {seen[case.id]} and {path}",
            )
        seen[case.id] = path
        cases.append(case)
    return cases


def load_default_suite(suite: str) -> list[EvaluationCase]:
    return load_suite(default_cases_root(), suite)


def cases_sha256(cases: Sequence[EvaluationCase]) -> str:
    """Return the canonical hash used to anchor a report to exact cases."""

    canonical_cases: list[dict] = []
    for case in cases:
        payload = case.model_dump(mode="json")
        if case.schema_version == 1:
            # Schema v1 is frozen. New optional v2 model fields must not change
            # the hash of the historical router-smoke cases/baseline.
            payload.pop("runtime_setup", None)
            handoff = payload["expectations"]["tools"].get("handoff")
            if isinstance(handoff, dict):
                for field in (
                    "operation",
                    "scope_source",
                    "dataset_date",
                    "selection",
                    "status",
                ):
                    handoff.pop(field, None)
        canonical_cases.append(payload)
    payload = json.dumps(
        canonical_cases,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

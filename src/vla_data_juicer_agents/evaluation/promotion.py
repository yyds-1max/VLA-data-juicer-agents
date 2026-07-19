from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Sequence
import uuid

from pydantic import ValidationError

from vla_data_juicer_agents.evaluation.cases import (
    cases_sha256,
    default_cases_root,
    load_suite,
)
from vla_data_juicer_agents.evaluation.contract import EVALUATION_CONTRACT_VERSION
from vla_data_juicer_agents.evaluation.models import (
    CaseResult,
    EvaluationCase,
    EvaluationStatus,
)
from vla_data_juicer_agents.evaluation.reporting import write_baseline_reports


class PromotionError(ValueError):
    """Raised when a run is not safe to promote to a tracked baseline."""


@dataclass(frozen=True)
class PromotionResult:
    json_path: Path
    markdown_path: Path
    case_count: int
    attempt_count: int


def _read_aggregate(path: Path) -> tuple[dict[str, Any], list[CaseResult]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PromotionError(f"cannot read aggregate report {path}: {error}") from error
    if not isinstance(raw, dict):
        raise PromotionError("aggregate report must contain one JSON object")
    if raw.get("schema_version") not in (1, 2):
        raise PromotionError(
            f"unsupported aggregate schema_version {raw.get('schema_version')!r}",
        )
    metadata = raw.get("run_metadata")
    payloads = raw.get("results")
    if not isinstance(metadata, dict):
        raise PromotionError("aggregate report is missing run_metadata")
    if not isinstance(payloads, list):
        raise PromotionError("aggregate report is missing results")
    try:
        results = [CaseResult.model_validate(payload) for payload in payloads]
    except ValidationError as error:
        raise PromotionError(f"aggregate report contains an invalid result: {error}") from error
    return metadata, results


def _git_head(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PromotionError(f"cannot determine current Git commit: {error}") from error
    commit = completed.stdout.strip()
    if not commit:
        raise PromotionError("current Git commit is empty")
    return commit


def _worktree_changes(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PromotionError(f"cannot inspect Git worktree: {error}") from error
    return completed.stdout.strip()


def _validate_complete_run(
    *,
    metadata: dict[str, Any],
    results: Sequence[CaseResult],
    suite: str,
    cases: Sequence[EvaluationCase],
    head_commit: str,
) -> int:
    if metadata.get("suite") != suite:
        raise PromotionError(
            f"aggregate suite {metadata.get('suite')!r} does not match {suite!r}",
        )
    if metadata.get("evaluation_contract_version", 1) != EVALUATION_CONTRACT_VERSION:
        raise PromotionError(
            "aggregate evaluation_contract_version does not match the current evaluator",
        )
    repeat = metadata.get("repeat")
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
        raise PromotionError("aggregate run_metadata.repeat must be a positive integer")

    expected_ids = {case.id for case in cases}
    actual_ids = {result.case_id for result in results}
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise PromotionError(
            f"aggregate is not a complete suite (missing={missing}, extra={extra})",
        )
    if any(result.suite != suite for result in results):
        raise PromotionError("aggregate contains a result from another suite")

    expected_attempts = list(range(1, repeat + 1))
    for case_id in sorted(expected_ids):
        attempts = sorted(
            result.repeat_index for result in results if result.case_id == case_id
        )
        if attempts != expected_attempts:
            raise PromotionError(
                f"case {case_id!r} attempts must be exactly {expected_attempts}, got {attempts}",
            )
    if any(result.status is EvaluationStatus.ERROR for result in results):
        raise PromotionError("aggregate contains ERROR results and cannot be promoted")

    expected_hash = cases_sha256(cases)
    if metadata.get("cases_sha256") != expected_hash:
        raise PromotionError("aggregate cases_sha256 does not match the current suite")
    if metadata.get("git_commit") != head_commit:
        raise PromotionError(
            "aggregate git_commit does not match the current HEAD commit",
        )
    return repeat


def _replace_report_pair(
    temporary_json: Path,
    temporary_markdown: Path,
    json_destination: Path,
    markdown_destination: Path,
    *,
    nonce: str,
) -> None:
    """Replace both baseline files and restore the previous pair on failure."""

    destinations = (json_destination, markdown_destination)
    backups = (
        json_destination.with_name(f".{json_destination.name}.{nonce}.bak"),
        markdown_destination.with_name(f".{markdown_destination.name}.{nonce}.bak"),
    )
    installed: list[Path] = []
    backed_up: list[tuple[Path, Path]] = []
    succeeded = False
    try:
        for destination, backup in zip(destinations, backups, strict=True):
            if destination.exists():
                os.replace(destination, backup)
                backed_up.append((destination, backup))
        for temporary, destination in (
            (temporary_json, json_destination),
            (temporary_markdown, markdown_destination),
        ):
            os.replace(temporary, destination)
            installed.append(destination)
        succeeded = True
    except Exception:
        for destination in installed:
            destination.unlink(missing_ok=True)
        for destination, backup in reversed(backed_up):
            os.replace(backup, destination)
        raise
    finally:
        if succeeded:
            for backup in backups:
                backup.unlink(missing_ok=True)


def promote_baseline(
    aggregate_path: str | Path,
    *,
    suite: str,
    repo_root: str | Path,
    cases_root: str | Path | None = None,
    baseline_dir: str | Path | None = None,
) -> PromotionResult:
    """Validate and atomically promote a completed run without invoking a model."""

    repository = Path(repo_root).resolve()
    aggregate = Path(aggregate_path).resolve()
    case_directory = Path(cases_root) if cases_root is not None else default_cases_root()
    destination_dir = (
        Path(baseline_dir) if baseline_dir is not None else repository / "evals" / "baselines"
    )

    metadata, results = _read_aggregate(aggregate)
    cases = load_suite(case_directory, suite)
    head_commit = _git_head(repository)
    _validate_complete_run(
        metadata=metadata,
        results=results,
        suite=suite,
        cases=cases,
        head_commit=head_commit,
    )
    changes = _worktree_changes(repository)
    if changes:
        raise PromotionError("Git worktree must be clean before baseline promotion")

    destination_dir.mkdir(parents=True, exist_ok=True)
    json_destination = destination_dir / f"{suite}.json"
    markdown_destination = destination_dir / f"{suite}.md"
    nonce = uuid.uuid4().hex
    temporary_json = destination_dir / f".{suite}.{nonce}.json.tmp"
    temporary_markdown = destination_dir / f".{suite}.{nonce}.md.tmp"
    try:
        write_baseline_reports(
            results,
            temporary_json,
            temporary_markdown,
            run_metadata=metadata,
        )
        _replace_report_pair(
            temporary_json,
            temporary_markdown,
            json_destination,
            markdown_destination,
            nonce=nonce,
        )
    finally:
        temporary_json.unlink(missing_ok=True)
        temporary_markdown.unlink(missing_ok=True)

    return PromotionResult(
        json_path=json_destination,
        markdown_path=markdown_destination,
        case_count=len(cases),
        attempt_count=len(results),
    )

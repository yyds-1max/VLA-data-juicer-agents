from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import re
from typing import Any, Iterable

from vla_data_juicer_agents.evaluation.models import CaseResult


_SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+)?(?<![a-z0-9])(?:sk-[a-z0-9_-]{8,}|dashscope[-_a-z0-9]{8,})",
)
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s|:;]+/)*[^\s|:;]*")


def _safe_failure_reason(value: str) -> str:
    redacted = _SECRET_PATTERN.sub("[REDACTED]", value)
    redacted = _ABSOLUTE_PATH_PATTERN.sub("[PATH]", redacted)
    return redacted[:500]


def _status_counts(results: list[CaseResult]) -> dict[str, int]:
    counts = Counter(result.status.value for result in results)
    return {status: counts.get(status, 0) for status in ("PASS", "FAIL", "TIMEOUT", "ERROR")}


def build_aggregate_report(
    results: Iterable[CaseResult],
    *,
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    materialized = list(results)
    return {
        "schema_version": 1,
        "run_metadata": dict(run_metadata or {}),
        "summary": {"total": len(materialized), "status_counts": _status_counts(materialized)},
        "results": [result.model_dump(mode="json") for result in materialized],
    }


def write_aggregate_report(
    results: Iterable[CaseResult],
    path: str | Path,
    *,
    run_metadata: dict[str, Any] | None = None,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            build_aggregate_report(results, run_metadata=run_metadata),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def build_baseline_report(
    results: Iterable[CaseResult],
    *,
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    materialized = list(results)
    baseline_results: list[dict[str, Any]] = []
    for result in materialized:
        observation = result.observation
        failures = [_safe_failure_reason(check.message) for check in result.checks if not check.passed]
        if result.error_message:
            failures.append(_safe_failure_reason(result.error_message))
        compact: dict[str, Any] = {
            "case_id": result.case_id,
            "suite": result.suite,
            "repeat_index": result.repeat_index,
            "status": result.status.value,
            "metrics": result.metrics,
            "failure_reasons": failures,
            "error_type": result.error_type,
        }
        if observation is not None:
            compact["metrics"] = {
                **compact["metrics"],
                "event_count": len(observation.events),
                "handoff_count": len(observation.handoffs),
                "response_chars": len(observation.final_response),
            }
        baseline_results.append(compact)
    allowed_metadata = {
        "schema_version",
        "git_commit",
        "model",
        "model_parameters",
        "agentscope_version",
        "cases_sha256",
        "prompt_sha256",
        "tool_schema_sha256",
    }
    return {
        "schema_version": 1,
        "run_metadata": {
            key: value
            for key, value in dict(run_metadata or {}).items()
            if key in allowed_metadata
        },
        "summary": {"total": len(materialized), "status_counts": _status_counts(materialized)},
        "results": baseline_results,
    }


def _baseline_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    counts = summary["status_counts"]
    metadata = report.get("run_metadata", {})
    tool_hashes = ", ".join(metadata.get("tool_schema_sha256", [])) or "—"
    lines = [
        "# Evaluation baseline",
        "",
        f"Total: {summary['total']} — PASS {counts['PASS']}, FAIL {counts['FAIL']}, "
        f"TIMEOUT {counts['TIMEOUT']}, ERROR {counts['ERROR']}",
        "",
        "## Version anchors",
        "",
        f"- Git commit: `{metadata.get('git_commit') or '—'}`",
        f"- Model: `{metadata.get('model') or '—'}`",
        "- Model parameters: `"
        + json.dumps(metadata.get("model_parameters", {}), sort_keys=True)
        + "`",
        f"- AgentScope: `{metadata.get('agentscope_version') or '—'}`",
        f"- Cases SHA-256: `{metadata.get('cases_sha256') or '—'}`",
        f"- Prompt SHA-256: `{metadata.get('prompt_sha256') or '—'}`",
        f"- Tool Schema SHA-256: `{tool_hashes}`",
        "",
        "## Results",
        "",
        "| Case | Repeat | Status | Model calls | Tool calls | Tokens | Failure reasons |",
        "| --- | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for result in report["results"]:
        metrics = result.get("metrics", {})
        reasons = "; ".join(result.get("failure_reasons", [])) or "—"
        reasons = reasons.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {result['case_id']} | {result['repeat_index']} | {result['status']} | "
            f"{metrics.get('model_calls', 0)} | {metrics.get('tool_calls', 0)} | "
            f"{metrics.get('total_tokens', 0)} | {reasons} |",
        )
    return "\n".join(lines) + "\n"


def write_baseline_reports(
    results: Iterable[CaseResult],
    json_path: str | Path,
    markdown_path: str | Path,
    *,
    run_metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    report = build_baseline_report(results, run_metadata=run_metadata)
    json_destination = Path(json_path)
    markdown_destination = Path(markdown_path)
    json_destination.parent.mkdir(parents=True, exist_ok=True)
    markdown_destination.parent.mkdir(parents=True, exist_ok=True)
    json_destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_destination.write_text(_baseline_markdown(report), encoding="utf-8")
    return json_destination, markdown_destination


def write_run_reports(
    results: Iterable[CaseResult],
    metadata: dict[str, Any],
    output_dir: str | Path,
) -> Path:
    """Write the complete local run report to a conventional filename."""

    return write_aggregate_report(
        results,
        Path(output_dir) / "aggregate.json",
        run_metadata=metadata,
    )


def write_suite_baseline_reports(
    results: Iterable[CaseResult],
    metadata: dict[str, Any],
    baseline_dir: str | Path,
    suite: str,
) -> tuple[Path, Path]:
    """Write a compact, commit-safe baseline for one suite."""

    root = Path(baseline_dir)
    return write_baseline_reports(
        results,
        root / f"{suite}.json",
        root / f"{suite}.md",
        run_metadata=metadata,
    )

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import re
from typing import Any, Iterable

from vla_data_juicer_agents.evaluation.models import CaseResult
from vla_data_juicer_agents.evaluation.stability import (
    StabilityStatus,
    failure_signatures,
    summarize_results,
)


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


def _summary_payload(results: list[CaseResult]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    case_summaries = [
        summary.model_dump(mode="json")
        for summary in summarize_results(results)
    ]
    stability_counts = Counter(
        summary["stability_status"] for summary in case_summaries
    )
    return (
        {
            "total": len(results),
            "case_count": len(case_summaries),
            "status_counts": _status_counts(results),
            "stability_counts": {
                status.value: stability_counts[status.value]
                for status in StabilityStatus
            },
        },
        case_summaries,
    )


def build_aggregate_report(
    results: Iterable[CaseResult],
    *,
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    materialized = list(results)
    summary, case_summaries = _summary_payload(materialized)
    return {
        "schema_version": 2,
        "run_metadata": dict(run_metadata or {}),
        "summary": summary,
        "case_summaries": case_summaries,
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
    summary, case_summaries = _summary_payload(materialized)
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
            "failure_signatures": list(failure_signatures(result)),
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
        "schema_version": 2,
        "run_metadata": {
            key: value
            for key, value in dict(run_metadata or {}).items()
            if key in allowed_metadata
        },
        "summary": summary,
        "case_summaries": case_summaries,
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
        "## Stability",
        "",
        "| Case | Attempts | Stability | Pass rate | PASS | FAIL | TIMEOUT | ERROR | Failure signatures |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for case in report.get("case_summaries", []):
        counts_by_status = case["status_counts"]
        signatures = "; ".join(
            f"{name} ×{count}"
            for name, count in case.get("failure_signatures", {}).items()
        ) or "—"
        signatures = signatures.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {case['case_id']} | {case['attempts']} | {case['stability_status']} | "
            f"{case['pass_rate']:.3f} | {counts_by_status.get('PASS', 0)} | "
            f"{counts_by_status.get('FAIL', 0)} | {counts_by_status.get('TIMEOUT', 0)} | "
            f"{counts_by_status.get('ERROR', 0)} | {signatures} |",
        )
    lines.extend(
        [
            "",
            "## Results",
            "",
            "| Case | Repeat | Status | Model calls | Tool calls | Tokens | Failure reasons |",
            "| --- | ---: | --- | ---: | ---: | ---: | --- |",
        ],
    )
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

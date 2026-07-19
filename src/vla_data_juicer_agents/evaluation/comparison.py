"""Load and compare versioned evaluation reports.

The comparison layer deliberately operates on report data only.  It does not
load cases, call a model, or inspect the current checkout, which keeps it safe
for both the CLI and tests to use with historical artifacts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any


_COMPATIBILITY_FIELDS = (
    "suite",
    "evaluation_contract_version",
    "cases_sha256",
    "model",
    "model_parameters",
    "agentscope_version",
)
_CHANGE_FIELDS = ("git_commit", "prompt_sha256", "tool_schema_sha256")
_STATUSES = ("PASS", "FAIL", "TIMEOUT", "ERROR")


class ComparisonError(ValueError):
    """Base class for invalid or incomparable evaluation reports."""


class ReportFormatError(ComparisonError):
    """Raised when an evaluation report cannot be normalized."""


class IncompatibleReportsError(ComparisonError):
    """Raised when two valid reports do not describe comparable runs."""

    def __init__(self, mismatches: Mapping[str, Mapping[str, Any]]) -> None:
        self.mismatches = {key: dict(value) for key, value in mismatches.items()}
        fields = ", ".join(sorted(self.mismatches))
        super().__init__(f"evaluation reports are incompatible: {fields}")


@dataclass(frozen=True)
class NormalizedCaseSummary:
    case_id: str
    attempts: int
    status_counts: dict[str, int]
    pass_rate: float
    stability_status: str
    failure_signatures: dict[str, int]

    @property
    def has_error(self) -> bool:
        return self.status_counts["ERROR"] > 0

    @property
    def has_timeout(self) -> bool:
        return self.status_counts["TIMEOUT"] > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "status_counts": dict(self.status_counts),
            "pass_rate": self.pass_rate,
            "stability_status": self.stability_status,
            "failure_signatures": dict(self.failure_signatures),
        }


@dataclass(frozen=True)
class NormalizedReport:
    schema_version: int
    suite: str
    metadata: dict[str, Any]
    cases: dict[str, NormalizedCaseSummary]


def load_report(path: str | Path) -> dict[str, Any]:
    """Read a JSON evaluation report and validate its outer wire shape."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReportFormatError(f"cannot read evaluation report {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportFormatError(f"invalid JSON evaluation report {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReportFormatError("evaluation report must be a JSON object")
    return payload


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_failure_signature(reason: str) -> str:
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
    return f"legacy:{digest}"


def _integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportFormatError(f"{field} must be an integer")
    if value < 0:
        raise ReportFormatError(f"{field} cannot be negative")
    return value


def _status_counts(value: Any, *, field: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ReportFormatError(f"{field} must be an object")
    return {
        status: _integer(value.get(status, 0), field=f"{field}.{status}")
        for status in _STATUSES
    }


def _derive_stability(counts: Mapping[str, int], attempts: int) -> str:
    if counts["ERROR"]:
        return "ERROR"
    if counts["TIMEOUT"]:
        return "TIMEOUT"
    if attempts == 1:
        return "SINGLE_SAMPLE"
    if counts["PASS"] == attempts:
        return "STABLE_PASS"
    if counts["FAIL"] == attempts:
        return "STABLE_FAIL"
    return "FLAKY"


def _failure_signature_counts(value: Any, *, field: str) -> dict[str, int]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        parsed: dict[str, int] = {}
        for signature, count in value.items():
            parsed_count = _integer(count, field=f"{field}.{signature}")
            if parsed_count > 0:
                parsed[str(signature)] = parsed_count
        return dict(sorted(parsed.items()))
    if not isinstance(value, list):
        raise ReportFormatError(f"{field} must be an object or list")
    counts: Counter[str] = Counter()
    for item in value:
        if isinstance(item, str):
            counts[item] += 1
        elif isinstance(item, Mapping):
            signature = item.get("signature") or item.get("name")
            if not isinstance(signature, str) or not signature:
                raise ReportFormatError(f"{field} entries require a signature")
            counts[signature] += _integer(item.get("count", 1), field=f"{field}.{signature}")
        else:
            raise ReportFormatError(f"{field} entries must be strings or objects")
    return dict(sorted(counts.items()))


def _v2_case_summaries(payload: Mapping[str, Any]) -> Any:
    direct = payload.get("case_summaries")
    if direct is not None:
        return direct
    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        return summary.get("case_summaries")
    return None


def _normalize_v2_cases(value: Any) -> dict[str, NormalizedCaseSummary]:
    if isinstance(value, Mapping):
        entries: list[Any] = []
        for case_id, summary in value.items():
            if not isinstance(summary, Mapping):
                raise ReportFormatError("case_summaries values must be objects")
            entries.append({"case_id": case_id, **summary})
    elif isinstance(value, list):
        entries = value
    else:
        raise ReportFormatError("case_summaries must be an object or list")

    normalized: dict[str, NormalizedCaseSummary] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ReportFormatError("case_summaries entries must be objects")
        case_id = entry.get("case_id") or entry.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ReportFormatError("case summary requires a non-empty case_id")
        if case_id in normalized:
            raise ReportFormatError(f"duplicate case summary {case_id!r}")
        counts = _status_counts(
            entry.get("status_counts", {}),
            field=f"case_summaries.{case_id}.status_counts",
        )
        attempts = _integer(
            entry.get("attempts", sum(counts.values())),
            field=f"case_summaries.{case_id}.attempts",
        )
        if attempts <= 0 or attempts != sum(counts.values()):
            raise ReportFormatError(
                f"case summary {case_id!r} attempts must equal its status count total",
            )
        calculated_pass_rate = counts["PASS"] / attempts
        reported_pass_rate = entry.get("pass_rate", calculated_pass_rate)
        if (
            isinstance(reported_pass_rate, bool)
            or not isinstance(reported_pass_rate, (int, float))
            or not math.isfinite(reported_pass_rate)
            or not 0 <= reported_pass_rate <= 1
        ):
            raise ReportFormatError(f"case summary {case_id!r} pass_rate must be numeric")
        if abs(float(reported_pass_rate) - calculated_pass_rate) > 1e-9:
            raise ReportFormatError(f"case summary {case_id!r} has inconsistent pass_rate")
        stability = entry.get("stability_status", entry.get("stability"))
        if stability is None:
            stability = _derive_stability(counts, attempts)
        if not isinstance(stability, str) or not stability:
            raise ReportFormatError(f"case summary {case_id!r} has invalid stability status")
        expected_stability = _derive_stability(counts, attempts)
        if stability != expected_stability:
            raise ReportFormatError(
                f"case summary {case_id!r} has inconsistent stability status",
            )
        expected_stability = _derive_stability(counts, attempts)
        if stability != expected_stability:
            raise ReportFormatError(
                f"case summary {case_id!r} has inconsistent stability status",
            )
        signatures = _failure_signature_counts(
            entry.get("failure_signatures", {}),
            field=f"case_summaries.{case_id}.failure_signatures",
        )
        normalized[case_id] = NormalizedCaseSummary(
            case_id=case_id,
            attempts=attempts,
            status_counts=counts,
            pass_rate=calculated_pass_rate,
            stability_status=stability,
            failure_signatures=signatures,
        )
    if not normalized:
        raise ReportFormatError("evaluation report contains no case summaries")
    return normalized


def _result_failure_signatures(result: Mapping[str, Any], status: str) -> list[str]:
    if status == "TIMEOUT":
        return ["timeout"]
    if status == "ERROR":
        error_type = result.get("error_type") or "EvaluationError"
        return [f"error:{error_type}"]
    if status != "FAIL":
        return []
    checks = result.get("checks")
    if isinstance(checks, list):
        names = [
            check.get("name")
            for check in checks
            if isinstance(check, Mapping)
            and check.get("passed") is False
            and isinstance(check.get("name"), str)
            and check.get("name")
        ]
        if names:
            return names
    recorded_signatures = result.get("failure_signatures")
    if isinstance(recorded_signatures, list):
        signatures = [
            signature
            for signature in recorded_signatures
            if isinstance(signature, str) and signature
        ]
        if signatures:
            return signatures
    reasons = result.get("failure_reasons")
    if not isinstance(reasons, list) and result.get("error_message"):
        reasons = [result["error_message"]]
    if isinstance(reasons, list):
        return [
            _legacy_failure_signature(reason)
            for reason in reasons
            if isinstance(reason, str) and reason
        ]
    return []


def _normalize_v1_cases(payload: Mapping[str, Any]) -> dict[str, NormalizedCaseSummary]:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise ReportFormatError("evaluation report contains neither results nor case_summaries")
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for result in results:
        if not isinstance(result, Mapping):
            raise ReportFormatError("results entries must be objects")
        case_id = result.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ReportFormatError("evaluation result requires a non-empty case_id")
        grouped[case_id].append(result)

    normalized: dict[str, NormalizedCaseSummary] = {}
    for case_id, case_results in grouped.items():
        statuses: Counter[str] = Counter()
        signatures: Counter[str] = Counter()
        for result in case_results:
            status = str(result.get("status", "")).upper()
            if status not in _STATUSES:
                raise ReportFormatError(f"case {case_id!r} has unknown status {status!r}")
            statuses[status] += 1
            signatures.update(_result_failure_signatures(result, status))
        counts = {status: statuses[status] for status in _STATUSES}
        attempts = len(case_results)
        normalized[case_id] = NormalizedCaseSummary(
            case_id=case_id,
            attempts=attempts,
            status_counts=counts,
            pass_rate=counts["PASS"] / attempts,
            stability_status=_derive_stability(counts, attempts),
            failure_signatures=dict(sorted(signatures.items())),
        )
    return normalized


def normalize_report(payload: Mapping[str, Any]) -> NormalizedReport:
    """Normalize v1 result reports and v2 case-summary reports."""

    if not isinstance(payload, Mapping):
        raise ReportFormatError("evaluation report must be an object")
    schema_version = _integer(payload.get("schema_version", 1), field="schema_version")
    if schema_version not in {1, 2}:
        raise ReportFormatError(f"unsupported evaluation report schema_version {schema_version}")
    metadata_raw = payload.get("run_metadata", {})
    if not isinstance(metadata_raw, Mapping):
        raise ReportFormatError("run_metadata must be an object")
    metadata = dict(metadata_raw)
    summaries = _v2_case_summaries(payload)
    cases = _normalize_v2_cases(summaries) if summaries is not None else _normalize_v1_cases(payload)

    suite = payload.get("suite") or metadata.get("suite")
    declared_suites = {
        result.get("suite")
        for result in payload.get("results", [])
        if isinstance(result, Mapping) and isinstance(result.get("suite"), str)
    }
    if isinstance(summaries, list):
        declared_suites.update(
            summary.get("suite")
            for summary in summaries
            if isinstance(summary, Mapping) and isinstance(summary.get("suite"), str)
        )
    elif isinstance(summaries, Mapping):
        declared_suites.update(
            summary.get("suite")
            for summary in summaries.values()
            if isinstance(summary, Mapping) and isinstance(summary.get("suite"), str)
        )
    declared_suites.discard(None)
    if suite is None and len(declared_suites) == 1:
        suite = next(iter(declared_suites))
    if not isinstance(suite, str) or not suite:
        raise ReportFormatError("evaluation report does not identify its suite")
    if declared_suites and declared_suites != {suite}:
        raise ReportFormatError("evaluation report contains inconsistent suites")
    metadata.setdefault("suite", suite)
    return NormalizedReport(
        schema_version=schema_version,
        suite=suite,
        metadata=metadata,
        cases=cases,
    )


def _compatibility_value(report: NormalizedReport, field: str) -> Any:
    if field == "suite":
        return report.suite
    if field == "evaluation_contract_version":
        return report.metadata.get(field, 1)
    return report.metadata.get(field)


def _version_changes(
    baseline: NormalizedReport,
    candidate: NormalizedReport,
) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for field in _CHANGE_FIELDS:
        baseline_value = baseline.metadata.get(field)
        candidate_value = candidate.metadata.get(field)
        if _canonical(baseline_value) != _canonical(candidate_value):
            changes[field] = {"baseline": baseline_value, "candidate": candidate_value}
    return changes


def compare_reports(
    baseline_payload: Mapping[str, Any],
    candidate_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deterministic, serializable comparison of two reports.

    Raises :class:`IncompatibleReportsError` when the reports cannot safely be
    interpreted as two observations of the same suite and case contract.
    """

    baseline = normalize_report(baseline_payload)
    candidate = normalize_report(candidate_payload)
    mismatches: dict[str, dict[str, Any]] = {}
    for field in _COMPATIBILITY_FIELDS:
        baseline_value = _compatibility_value(baseline, field)
        candidate_value = _compatibility_value(candidate, field)
        if (
            baseline_value is None
            or candidate_value is None
            or _canonical(baseline_value) != _canonical(candidate_value)
        ):
            mismatches[field] = {
                "baseline": baseline_value,
                "candidate": candidate_value,
            }
    baseline_cases = set(baseline.cases)
    candidate_cases = set(candidate.cases)
    if baseline_cases != candidate_cases:
        mismatches["case_ids"] = {
            "baseline": sorted(baseline_cases),
            "candidate": sorted(candidate_cases),
        }
    if mismatches:
        raise IncompatibleReportsError(mismatches)

    case_comparisons: list[dict[str, Any]] = []
    verdict_counts: Counter[str] = Counter()
    error_cases: list[str] = []
    timeout_cases: list[str] = []
    for case_id in sorted(baseline.cases):
        before = baseline.cases[case_id]
        after = candidate.cases[case_id]
        if after.pass_rate > before.pass_rate:
            verdict = "IMPROVED"
        elif after.pass_rate < before.pass_rate:
            verdict = "REGRESSED"
        else:
            verdict = "UNCHANGED"
        verdict_counts[verdict] += 1
        if after.has_error:
            error_cases.append(case_id)
        if after.has_timeout:
            timeout_cases.append(case_id)
        before_signatures = set(before.failure_signatures)
        after_signatures = set(after.failure_signatures)
        baseline_uses_legacy = any(
            signature.startswith("legacy:") for signature in before_signatures
        )
        candidate_uses_legacy = any(
            signature.startswith("legacy:") for signature in after_signatures
        )
        signatures_comparable = not (
            before_signatures
            and after_signatures
            and baseline_uses_legacy != candidate_uses_legacy
        )
        signature_count_changes = (
            {
                signature: {
                    "baseline": before.failure_signatures.get(signature, 0),
                    "candidate": after.failure_signatures.get(signature, 0),
                }
                for signature in sorted(before_signatures | after_signatures)
                if before.failure_signatures.get(signature, 0)
                != after.failure_signatures.get(signature, 0)
            }
            if signatures_comparable
            else {}
        )
        case_comparisons.append(
            {
                "case_id": case_id,
                "verdict": verdict,
                "baseline": before.as_dict(),
                "candidate": after.as_dict(),
                "changes": {
                    "pass_rate_delta": after.pass_rate - before.pass_rate,
                    "stability_changed": before.stability_status != after.stability_status,
                    "failure_signatures_comparable": signatures_comparable,
                    "failure_signatures_added": (
                        sorted(after_signatures - before_signatures)
                        if signatures_comparable
                        else []
                    ),
                    "failure_signatures_removed": (
                        sorted(before_signatures - after_signatures)
                        if signatures_comparable
                        else []
                    ),
                    "failure_signature_count_changes": signature_count_changes,
                },
                "candidate_infrastructure_error": after.has_error,
                "candidate_timeout": after.has_timeout,
            },
        )

    regressed_cases = [
        comparison["case_id"]
        for comparison in case_comparisons
        if comparison["verdict"] == "REGRESSED"
    ]
    return {
        "schema_version": 2,
        "report_type": "evaluation_comparison",
        "compatibility": {
            "compatible": True,
            "checked_fields": list(_COMPATIBILITY_FIELDS) + ["case_ids"],
        },
        "version_changes": _version_changes(baseline, candidate),
        "summary": {
            "case_count": len(case_comparisons),
            "verdict_counts": {
                verdict: verdict_counts[verdict]
                for verdict in ("IMPROVED", "REGRESSED", "UNCHANGED")
            },
            "regressed_cases": regressed_cases,
            "candidate_error_cases": error_cases,
            "candidate_timeout_cases": timeout_cases,
            "has_behavior_regression": bool(regressed_cases or timeout_cases),
            "has_infrastructure_error": bool(error_cases),
        },
        "cases": case_comparisons,
    }


def compare_report_files(
    baseline_path: str | Path,
    candidate_path: str | Path,
) -> dict[str, Any]:
    """Load and compare two report files."""

    return compare_reports(load_report(baseline_path), load_report(candidate_path))


def comparison_exit_code(comparison: Mapping[str, Any]) -> int:
    """Map a comparison summary to the public CLI exit-code contract."""

    summary = comparison.get("summary")
    if not isinstance(summary, Mapping):
        raise ReportFormatError("comparison report is missing summary")
    if summary.get("has_infrastructure_error") is True:
        return 2
    if summary.get("has_behavior_regression") is True:
        return 1
    return 0


def _comparison_markdown(comparison: Mapping[str, Any]) -> str:
    summary = comparison["summary"]
    verdicts = summary["verdict_counts"]
    lines = [
        "# Evaluation comparison",
        "",
        f"Cases: {summary['case_count']} — IMPROVED {verdicts['IMPROVED']}, "
        f"REGRESSED {verdicts['REGRESSED']}, UNCHANGED {verdicts['UNCHANGED']}",
        "",
        "## Version changes",
        "",
    ]
    version_changes = comparison.get("version_changes", {})
    if version_changes:
        lines.extend(
            [
                "| Field | Baseline | Candidate |",
                "| --- | --- | --- |",
            ],
        )
        for field, change in version_changes.items():
            before = _canonical(change.get("baseline")).replace("|", "\\|")
            after = _canonical(change.get("candidate")).replace("|", "\\|")
            lines.append(f"| {field} | `{before}` | `{after}` |")
    else:
        lines.append("No version-anchor changes.")
    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Case | Verdict | Baseline pass rate | Candidate pass rate | Baseline stability | Candidate stability | Failure signature changes |",
            "| --- | --- | ---: | ---: | --- | --- | --- |",
        ],
    )
    for case in comparison["cases"]:
        changes = case["changes"]
        signature_parts = []
        if not changes.get("failure_signatures_comparable", True):
            signature_parts.append("not comparable: legacy v1 reasons")
        elif changes["failure_signatures_added"]:
            signature_parts.append(
                "added: " + ", ".join(changes["failure_signatures_added"]),
            )
        if changes["failure_signatures_removed"]:
            signature_parts.append(
                "removed: " + ", ".join(changes["failure_signatures_removed"]),
            )
        signatures = "; ".join(signature_parts) or "—"
        signatures = signatures.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {case['case_id']} | {case['verdict']} | "
            f"{case['baseline']['pass_rate']:.3f} | {case['candidate']['pass_rate']:.3f} | "
            f"{case['baseline']['stability_status']} | "
            f"{case['candidate']['stability_status']} | {signatures} |",
        )
    return "\n".join(lines) + "\n"


def write_comparison_reports(
    comparison: Mapping[str, Any],
    output_dir: str | Path,
) -> tuple[Path, Path]:
    """Write compact JSON and Markdown comparison artifacts."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "comparison.json"
    markdown_path = destination / "comparison.md"
    json_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        _comparison_markdown(comparison),
        encoding="utf-8",
    )
    return json_path, markdown_path


__all__ = [
    "ComparisonError",
    "IncompatibleReportsError",
    "NormalizedCaseSummary",
    "NormalizedReport",
    "ReportFormatError",
    "compare_report_files",
    "compare_reports",
    "comparison_exit_code",
    "load_report",
    "normalize_report",
    "write_comparison_reports",
]

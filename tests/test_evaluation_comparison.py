from __future__ import annotations

import json
from pathlib import Path

import pytest

from vla_data_juicer_agents.evaluation.comparison import (
    IncompatibleReportsError,
    ReportFormatError,
    compare_report_files,
    compare_reports,
    comparison_exit_code,
    load_report,
    normalize_report,
    write_comparison_reports,
)


def _metadata(**overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "suite": "router-smoke",
        "cases_sha256": "cases-v1",
        "model": "qwen-test",
        "model_parameters": {"parallel_tool_calls": False},
        "agentscope_version": "2.0.1",
        "git_commit": "before",
        "prompt_sha256": "prompt-before",
        "tool_schema_sha256": ["tools-before"],
    }
    metadata.update(overrides)
    return metadata


def _v1_report(
    results: list[dict[str, object]],
    *,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_metadata": metadata or _metadata(),
        "summary": {"total": len(results)},
        "results": results,
    }


def _result(
    case_id: str,
    status: str,
    attempt: int = 1,
    **extra: object,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "suite": "router-smoke",
        "repeat_index": attempt,
        "status": status,
        **extra,
    }


def _v2_report(
    summaries: list[dict[str, object]],
    *,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "run_metadata": metadata or _metadata(),
        "case_summaries": summaries,
    }


def _summary(
    case_id: str,
    *,
    passed: int,
    failed: int = 0,
    timeout: int = 0,
    error: int = 0,
    stability: str | None = None,
    signatures: dict[str, int] | None = None,
) -> dict[str, object]:
    counts = {"PASS": passed, "FAIL": failed, "TIMEOUT": timeout, "ERROR": error}
    attempts = sum(counts.values())
    return {
        "case_id": case_id,
        "suite": "router-smoke",
        "attempts": attempts,
        "status_counts": counts,
        "pass_rate": passed / attempts,
        "stability_status": stability
        or ("SINGLE_SAMPLE" if attempts == 1 else "STABLE_PASS"),
        "failure_signatures": signatures or {},
        "duration_ms": {"min": 1, "median": 2, "max": 3},
    }


def test_load_report_rejects_invalid_json_and_non_object(tmp_path: Path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(ReportFormatError, match="invalid JSON"):
        load_report(invalid)

    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(ReportFormatError, match="JSON object"):
        load_report(array)


def test_normalize_v1_aggregate_prefers_failed_check_names():
    report = _v1_report(
        [
            _result("case-a", "PASS"),
            _result(
                "case-a",
                "FAIL",
                2,
                checks=[
                    {"name": "response.question", "passed": False, "message": "missing"},
                    {"name": "tools.allowed", "passed": True, "message": "ok"},
                ],
            ),
            _result("case-a", "TIMEOUT", 3),
        ],
    )

    case = normalize_report(report).cases["case-a"]

    assert case.pass_rate == pytest.approx(1 / 3)
    assert case.stability_status == "TIMEOUT"
    assert case.failure_signatures == {"response.question": 1, "timeout": 1}


def test_normalize_v1_baseline_hashes_legacy_failure_reasons_stably():
    report = _v1_report(
        [
            _result("case-a", "FAIL", failure_reasons=["redacted reason"]),
            _result("case-a", "FAIL", 2, failure_reasons=["redacted reason"]),
        ],
    )

    case = normalize_report(report).cases["case-a"]

    assert case.stability_status == "STABLE_FAIL"
    assert len(case.failure_signatures) == 1
    signature, count = next(iter(case.failure_signatures.items()))
    assert signature.startswith("legacy:")
    assert count == 2


def test_legacy_reason_and_named_check_signatures_are_not_false_differences():
    baseline = _v1_report(
        [_result("case-a", "FAIL", failure_reasons=["missing question"])],
    )
    candidate = _v1_report(
        [
            _result(
                "case-a",
                "FAIL",
                checks=[
                    {
                        "name": "response.question",
                        "passed": False,
                        "message": "missing question",
                    },
                ],
            ),
        ],
    )

    comparison = compare_reports(baseline, candidate)["cases"][0]

    assert comparison["verdict"] == "UNCHANGED"
    assert comparison["changes"]["failure_signatures_comparable"] is False
    assert comparison["changes"]["failure_signatures_added"] == []
    assert comparison["changes"]["failure_signatures_removed"] == []


def test_compare_v1_to_v2_reports_improvement_and_version_changes():
    baseline = _v1_report(
        [
            _result("case-a", "FAIL", failure_reasons=["old failure"]),
            _result("case-b", "PASS"),
        ],
    )
    candidate = _v2_report(
        [
            _summary("case-a", passed=3, stability="STABLE_PASS"),
            _summary(
                "case-b",
                passed=2,
                failed=1,
                stability="FLAKY",
                signatures={"response.question": 1},
            ),
        ],
        metadata=_metadata(
            git_commit="after",
            prompt_sha256="prompt-after",
            tool_schema_sha256=["tools-after"],
        ),
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison["schema_version"] == 2
    assert comparison["summary"]["verdict_counts"] == {
        "IMPROVED": 1,
        "REGRESSED": 1,
        "UNCHANGED": 0,
    }
    cases = {item["case_id"]: item for item in comparison["cases"]}
    assert cases["case-a"]["verdict"] == "IMPROVED"
    assert cases["case-a"]["changes"]["stability_changed"] is True
    assert cases["case-b"]["verdict"] == "REGRESSED"
    assert cases["case-b"]["changes"]["failure_signatures_added"] == [
        "response.question",
    ]
    assert cases["case-b"]["changes"]["failure_signature_count_changes"] == {
        "response.question": {"baseline": 0, "candidate": 1},
    }
    assert set(comparison["version_changes"]) == {
        "git_commit",
        "prompt_sha256",
        "tool_schema_sha256",
    }
    assert comparison["summary"]["has_behavior_regression"] is True
    assert comparison["summary"]["has_infrastructure_error"] is False


@pytest.mark.parametrize(
    ("field", "candidate_value"),
    [
        ("suite", "other-suite"),
        ("cases_sha256", "other-cases"),
        ("model", "other-model"),
        ("model_parameters", {"parallel_tool_calls": True}),
        ("agentscope_version", "9.9.9"),
    ],
)
def test_compare_rejects_incompatible_run_anchors(field: str, candidate_value: object):
    baseline = _v2_report([_summary("case-a", passed=1)])
    metadata = _metadata(**{field: candidate_value})
    candidate = _v2_report([_summary("case-a", passed=1)], metadata=metadata)
    if field == "suite":
        candidate["case_summaries"][0]["suite"] = candidate_value

    with pytest.raises(IncompatibleReportsError) as exc_info:
        compare_reports(baseline, candidate)

    assert field in exc_info.value.mismatches


def test_compare_rejects_different_case_sets():
    baseline = _v2_report([_summary("case-a", passed=1)])
    candidate = _v2_report([_summary("case-b", passed=1)])

    with pytest.raises(IncompatibleReportsError) as exc_info:
        compare_reports(baseline, candidate)

    assert exc_info.value.mismatches["case_ids"] == {
        "baseline": ["case-a"],
        "candidate": ["case-b"],
    }


def test_candidate_error_is_infrastructure_and_timeout_is_behavior_problem():
    baseline = _v2_report(
        [
            _summary("case-error", passed=0, failed=1, stability="SINGLE_SAMPLE"),
            _summary("case-timeout", passed=1),
        ],
    )
    candidate = _v2_report(
        [
            _summary(
                "case-error",
                passed=0,
                error=1,
                stability="ERROR",
                signatures={"error:ProviderError": 1},
            ),
            _summary(
                "case-timeout",
                passed=0,
                timeout=1,
                stability="TIMEOUT",
                signatures={"timeout": 1},
            ),
        ],
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison["summary"]["candidate_error_cases"] == ["case-error"]
    assert comparison["summary"]["candidate_timeout_cases"] == ["case-timeout"]
    assert comparison["summary"]["has_infrastructure_error"] is True
    assert comparison["summary"]["has_behavior_regression"] is True
    cases = {item["case_id"]: item for item in comparison["cases"]}
    assert cases["case-error"]["verdict"] == "UNCHANGED"
    assert cases["case-error"]["candidate_infrastructure_error"] is True
    assert cases["case-timeout"]["verdict"] == "REGRESSED"
    assert cases["case-timeout"]["candidate_timeout"] is True


def test_compare_report_files_returns_pure_json_data(tmp_path: Path):
    report = _v2_report([_summary("case-a", passed=1)])
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(json.dumps(report), encoding="utf-8")
    candidate_path.write_text(json.dumps(report), encoding="utf-8")

    comparison = compare_report_files(baseline_path, candidate_path)

    assert comparison["summary"]["verdict_counts"]["UNCHANGED"] == 1
    assert comparison["version_changes"] == {}
    json.dumps(comparison)


def test_v2_rejects_inconsistent_attempts_and_pass_rate():
    bad_attempts = _v2_report([_summary("case-a", passed=1)])
    bad_attempts["case_summaries"][0]["attempts"] = 2
    with pytest.raises(ReportFormatError, match="attempts"):
        normalize_report(bad_attempts)

    bad_rate = _v2_report([_summary("case-a", passed=1)])
    bad_rate["case_summaries"][0]["pass_rate"] = 0.5
    with pytest.raises(ReportFormatError, match="pass_rate"):
        normalize_report(bad_rate)

    bad_stability = _v2_report([_summary("case-a", passed=1)])
    bad_stability["case_summaries"][0]["stability_status"] = "STABLE_FAIL"
    with pytest.raises(ReportFormatError, match="stability"):
        normalize_report(bad_stability)


def test_compare_rejects_missing_required_compatibility_anchor():
    baseline = _v2_report([_summary("case-a", passed=1)])
    candidate = _v2_report([_summary("case-a", passed=1)])
    baseline["run_metadata"].pop("model")
    candidate["run_metadata"].pop("model")

    with pytest.raises(IncompatibleReportsError) as exc_info:
        compare_reports(baseline, candidate)

    assert "model" in exc_info.value.mismatches

    bad_stability = _v2_report(
        [_summary("case-a", passed=1, stability="STABLE_PASS")],
    )
    with pytest.raises(ReportFormatError, match="stability status"):
        normalize_report(bad_stability)


def test_v2_compact_report_can_infer_suite_from_case_summaries():
    report = _v2_report([_summary("case-a", passed=1)])
    report["run_metadata"].pop("suite")

    normalized = normalize_report(report)

    assert normalized.suite == "router-smoke"


def test_comparison_exit_codes_and_compact_report_writing(tmp_path: Path):
    baseline = _v2_report([_summary("case-a", passed=1)])
    unchanged = compare_reports(baseline, baseline)
    regressed = compare_reports(
        baseline,
        _v2_report([_summary("case-a", passed=0, failed=1)]),
    )
    errored = compare_reports(
        baseline,
        _v2_report(
            [
                _summary(
                    "case-a",
                    passed=0,
                    error=1,
                    stability="ERROR",
                ),
            ],
        ),
    )

    assert comparison_exit_code(unchanged) == 0
    assert comparison_exit_code(regressed) == 1
    assert comparison_exit_code(errored) == 2
    json_path, markdown_path = write_comparison_reports(regressed, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_type"] == (
        "evaluation_comparison"
    )
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "REGRESSED" in markdown
    assert "case-a" in markdown

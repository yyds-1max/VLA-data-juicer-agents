from __future__ import annotations

import pytest

from vla_data_juicer_agents.evaluation.models import (
    CaseResult,
    CaseRunObservation,
    EvaluationStatus,
    GradingCheck,
    TokenUsage,
)
from vla_data_juicer_agents.evaluation.stability import (
    StabilityStatus,
    classify_stability,
    failure_signatures,
    summarize_case_results,
    summarize_results,
)


def _result(
    status: EvaluationStatus,
    *,
    attempt: int = 1,
    case_id: str = "case_a",
    failed_checks: tuple[str, ...] = (),
    error_type: str | None = None,
    duration_ms: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        suite="router-smoke",
        repeat_index=attempt,
        status=status,
        checks=[
            GradingCheck(name=name, passed=False, message=f"{name} failed")
            for name in failed_checks
        ],
        error_type=error_type,
        metrics={
            "duration_ms": duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([EvaluationStatus.PASS], StabilityStatus.SINGLE_SAMPLE),
        ([EvaluationStatus.FAIL], StabilityStatus.SINGLE_SAMPLE),
        ([EvaluationStatus.PASS] * 3, StabilityStatus.STABLE_PASS),
        ([EvaluationStatus.FAIL] * 3, StabilityStatus.STABLE_FAIL),
        (
            [EvaluationStatus.PASS, EvaluationStatus.FAIL, EvaluationStatus.PASS],
            StabilityStatus.FLAKY,
        ),
        (
            [EvaluationStatus.ERROR, EvaluationStatus.TIMEOUT, EvaluationStatus.PASS],
            StabilityStatus.ERROR,
        ),
        (
            [EvaluationStatus.TIMEOUT, EvaluationStatus.PASS, EvaluationStatus.FAIL],
            StabilityStatus.TIMEOUT,
        ),
    ],
)
def test_classify_stability_priority(statuses, expected):
    assert classify_stability(
        [_result(status, attempt=index) for index, status in enumerate(statuses, start=1)],
    ) is expected


def test_classify_stability_rejects_empty_input():
    with pytest.raises(ValueError, match="empty"):
        classify_stability([])


def test_failure_signatures_use_check_names_and_fixed_infrastructure_names():
    failed = _result(
        EvaluationStatus.FAIL,
        failed_checks=("response.length", "tools.allowed", "tools.allowed"),
    )
    timeout = _result(EvaluationStatus.TIMEOUT, error_type="PrivateTimeout")
    error = _result(EvaluationStatus.ERROR, error_type="ProviderUnavailable")
    unknown_error = _result(EvaluationStatus.ERROR)

    assert failure_signatures(failed) == ("response.length", "tools.allowed")
    assert failure_signatures(timeout) == ("timeout",)
    assert failure_signatures(error) == ("error:ProviderUnavailable",)
    assert failure_signatures(unknown_error) == ("error:EvaluationError",)
    assert failure_signatures(_result(EvaluationStatus.PASS)) == ()


def test_summary_counts_failures_and_calculates_metric_ranges():
    results = [
        _result(
            EvaluationStatus.PASS,
            attempt=1,
            duration_ms=100,
            input_tokens=10,
            output_tokens=2,
        ),
        _result(
            EvaluationStatus.FAIL,
            attempt=2,
            failed_checks=("tools.allowed", "response.length"),
            duration_ms=300,
            input_tokens=30,
            output_tokens=6,
        ),
        _result(
            EvaluationStatus.FAIL,
            attempt=3,
            failed_checks=("tools.allowed",),
            duration_ms=200,
            input_tokens=20,
            output_tokens=4,
        ),
    ]

    summary = summarize_case_results(results)

    assert summary.attempts == 3
    assert summary.stability_status is StabilityStatus.FLAKY
    assert summary.status_counts == {"PASS": 1, "FAIL": 2, "TIMEOUT": 0, "ERROR": 0}
    assert summary.pass_rate == pytest.approx(1 / 3)
    assert summary.failure_signatures == {"response.length": 1, "tools.allowed": 2}
    assert summary.duration_ms.model_dump() == {"min": 100, "median": 200, "max": 300}
    assert summary.input_tokens.model_dump() == {"min": 10, "median": 20, "max": 30}
    assert summary.output_tokens.model_dump() == {"min": 2, "median": 4, "max": 6}
    assert summary.total_tokens.model_dump() == {"min": 12, "median": 24, "max": 36}


def test_summary_uses_observation_metrics_when_result_metrics_are_absent():
    result = _result(EvaluationStatus.TIMEOUT)
    result.metrics = {}
    result.observation = CaseRunObservation(
        duration_ms=123,
        token_usage=TokenUsage(input_tokens=7, output_tokens=5, total_tokens=12),
    )

    summary = summarize_case_results([result])

    assert summary.duration_ms.median == 123
    assert summary.input_tokens.median == 7
    assert summary.output_tokens.median == 5
    assert summary.total_tokens.median == 12


def test_even_sample_median_and_infrastructure_signature_counts():
    results = [
        _result(EvaluationStatus.TIMEOUT, attempt=1, duration_ms=10),
        _result(
            EvaluationStatus.ERROR,
            attempt=2,
            error_type="ProviderError",
            duration_ms=20,
        ),
    ]

    summary = summarize_case_results(results)

    assert summary.stability_status is StabilityStatus.ERROR
    assert summary.duration_ms.median == 15
    assert summary.failure_signatures == {"error:ProviderError": 1, "timeout": 1}


def test_summary_rejects_mixed_cases_and_groups_deterministically():
    first = _result(EvaluationStatus.PASS, case_id="z_case")
    second = _result(EvaluationStatus.FAIL, case_id="a_case")

    with pytest.raises(ValueError, match="one suite and case id"):
        summarize_case_results([first, second])

    summaries = summarize_results([first, second])
    assert [summary.case_id for summary in summaries] == ["a_case", "z_case"]

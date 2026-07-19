from __future__ import annotations

from collections import Counter, defaultdict
from enum import Enum
from statistics import median
from typing import Iterable

from pydantic import Field

from vla_data_juicer_agents.evaluation.models import (
    CaseResult,
    EvaluationStatus,
    StrictModel,
)


class StabilityStatus(str, Enum):
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    SINGLE_SAMPLE = "SINGLE_SAMPLE"
    STABLE_PASS = "STABLE_PASS"
    STABLE_FAIL = "STABLE_FAIL"
    FLAKY = "FLAKY"


class MetricStatistics(StrictModel):
    min: int | float
    median: int | float
    max: int | float


class CaseStabilitySummary(StrictModel):
    case_id: str
    suite: str
    attempts: int = Field(ge=1)
    stability_status: StabilityStatus
    status_counts: dict[str, int]
    pass_rate: float = Field(ge=0.0, le=1.0)
    failure_signatures: dict[str, int]
    duration_ms: MetricStatistics
    input_tokens: MetricStatistics
    output_tokens: MetricStatistics
    total_tokens: MetricStatistics


_STATUS_ORDER = tuple(status.value for status in EvaluationStatus)
_METRICS = ("duration_ms", "input_tokens", "output_tokens", "total_tokens")


def failure_signatures(result: CaseResult) -> tuple[str, ...]:
    """Return stable, non-sensitive failure identifiers for one attempt."""

    if result.status is EvaluationStatus.TIMEOUT:
        return ("timeout",)
    if result.status is EvaluationStatus.ERROR:
        return (f"error:{result.error_type or 'EvaluationError'}",)
    if result.status is EvaluationStatus.FAIL:
        return tuple(sorted({check.name for check in result.checks if not check.passed}))
    return ()


def classify_stability(results: Iterable[CaseResult]) -> StabilityStatus:
    attempts = list(results)
    if not attempts:
        raise ValueError("cannot classify an empty result set")
    statuses = [result.status for result in attempts]
    if EvaluationStatus.ERROR in statuses:
        return StabilityStatus.ERROR
    if EvaluationStatus.TIMEOUT in statuses:
        return StabilityStatus.TIMEOUT
    if len(statuses) == 1:
        return StabilityStatus.SINGLE_SAMPLE
    if all(status is EvaluationStatus.PASS for status in statuses):
        return StabilityStatus.STABLE_PASS
    if all(status is EvaluationStatus.FAIL for status in statuses):
        return StabilityStatus.STABLE_FAIL
    return StabilityStatus.FLAKY


def _metric_value(result: CaseResult, name: str) -> int | float:
    value = result.metrics.get(name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    observation = result.observation
    if observation is None:
        return 0
    if name == "duration_ms":
        return observation.duration_ms
    return getattr(observation.token_usage, name)


def _statistics(values: list[int | float]) -> MetricStatistics:
    return MetricStatistics(min=min(values), median=median(values), max=max(values))


def summarize_case_results(results: Iterable[CaseResult]) -> CaseStabilitySummary:
    attempts = list(results)
    if not attempts:
        raise ValueError("cannot summarize an empty result set")
    case_keys = {(result.suite, result.case_id) for result in attempts}
    if len(case_keys) != 1:
        raise ValueError("case stability summary requires one suite and case id")

    suite, case_id = case_keys.pop()
    counts = Counter(result.status.value for result in attempts)
    signature_counts = Counter(
        signature for result in attempts for signature in failure_signatures(result)
    )
    metrics = {
        name: _statistics([_metric_value(result, name) for result in attempts])
        for name in _METRICS
    }
    return CaseStabilitySummary(
        case_id=case_id,
        suite=suite,
        attempts=len(attempts),
        stability_status=classify_stability(attempts),
        status_counts={name: counts[name] for name in _STATUS_ORDER},
        pass_rate=counts[EvaluationStatus.PASS.value] / len(attempts),
        failure_signatures=dict(sorted(signature_counts.items())),
        **metrics,
    )


def summarize_results(results: Iterable[CaseResult]) -> list[CaseStabilitySummary]:
    """Group attempts by suite/case and return deterministic summaries."""

    grouped: dict[tuple[str, str], list[CaseResult]] = defaultdict(list)
    for result in results:
        grouped[(result.suite, result.case_id)].append(result)
    return [
        summarize_case_results(sorted(attempts, key=lambda item: item.repeat_index))
        for _, attempts in sorted(grouped.items())
    ]

from __future__ import annotations

from collections import Counter
from typing import Any

from vla_data_juicer_agents.evaluation.models import (
    CaseResult,
    CaseRunObservation,
    EvaluationCase,
    EvaluationStatus,
    GradingCheck,
)


HANDOFF_TOOL = "start_navigation_data_task"


def _check(name: str, passed: bool, success: str, failure: str) -> GradingCheck:
    return GradingCheck(name=name, passed=passed, message=success if passed else failure)


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _handoffs(observation: CaseRunObservation) -> list[dict[str, Any]]:
    if observation.handoffs:
        return observation.handoffs
    return [call.arguments for call in observation.tool_calls if call.name == HANDOFF_TOOL]


def grade_case(
    case: EvaluationCase,
    observation: CaseRunObservation,
    *,
    repeat_index: int = 1,
) -> CaseResult:
    """Grade observable behavior using deterministic, outcome-oriented rules."""

    checks: list[GradingCheck] = []
    expected_tools = case.expectations.tools
    calls = Counter(call.name for call in observation.tool_calls)
    unexpected = sorted(name for name in calls if name not in expected_tools.allowed_calls)
    checks.append(
        _check(
            "tools.allowed",
            not unexpected,
            "only allowed tools were called",
            f"unexpected tool calls: {', '.join(unexpected)}",
        ),
    )
    checks.append(
        _check(
            "tools.safety",
            not observation.forbidden_calls,
            "no forbidden tool execution was attempted",
            "forbidden tool attempts: " + ", ".join(observation.forbidden_calls),
        ),
    )
    for name, count in expected_tools.required_counts.items():
        actual = calls[name]
        checks.append(
            _check(
                f"tools.count.{name}",
                actual == count,
                f"{name} was called exactly {count} time(s)",
                f"expected {name} {count} time(s), observed {actual}",
            ),
        )

    handoffs = _handoffs(observation)
    checks.append(
        _check(
            "handoff.count",
            len(handoffs) == expected_tools.handoff_count,
            f"observed the expected {expected_tools.handoff_count} handoff(s)",
            f"expected {expected_tools.handoff_count} handoff(s), observed {len(handoffs)}",
        ),
    )
    if expected_tools.handoff is not None and len(handoffs) == 1:
        payload = handoffs[0]
        expected = expected_tools.handoff
        if expected.operation == "submit_plan":
            fields = (
                "operation",
                "phase",
                "decision_modes",
                "step_actions",
                "step_variants",
            )
        elif expected.operation is not None:
            fields = (
                "operation",
                "scope_source",
                "dataset_date",
                "selection",
                "status",
                "requested_outcome",
                "linked_fix",
            )
        else:
            fields = (
                "request",
                "target",
                "date",
                "clips",
                "response_language",
                "missing_fields",
            )
        for field in fields:
            wanted = getattr(expected, field)
            if wanted is None:
                continue
            actual = payload.get(field)
            checks.append(
                _check(
                    f"handoff.{field}",
                    actual == wanted,
                    f"handoff preserved {field}",
                    f"handoff {field} did not exactly match the expected value",
                ),
            )
        if expected.operation is None:
            confidence = payload.get("confidence")
            checks.append(
                _check(
                    "handoff.confidence",
                    confidence in expected.allowed_confidence,
                    "handoff confidence was acceptable",
                    (
                        f"handoff confidence {confidence!r} was not one of "
                        f"{expected.allowed_confidence}"
                    ),
                ),
            )
        forbidden = sorted(field for field in expected.forbidden_fields if field in payload)
        checks.append(
            _check(
                "handoff.forbidden_fields",
                not forbidden,
                "handoff omitted forbidden fields",
                f"handoff contained forbidden fields: {', '.join(forbidden)}",
            ),
        )

    response = observation.final_response.strip()
    expected_response = case.expectations.response
    if not response and expected_response.allow_empty:
        language_ok = True
    elif expected_response.language == "Chinese":
        language_ok = _contains_cjk(response)
    elif expected_response.language == "English":
        language_ok = bool(response) and not _contains_cjk(response)
    else:
        language_ok = True
    checks.append(
        _check(
            "response.language",
            language_ok,
            "response used the expected language",
            f"response did not use expected language {expected_response.language}",
        ),
    )
    folded = response.casefold()
    for index, group in enumerate(expected_response.required_any_groups):
        present = any(term.casefold() in folded for term in group)
        checks.append(
            _check(
                f"response.required_group.{index}",
                present,
                "response included a required concept",
                f"response did not include any required concept from {group}",
            ),
        )
    leaked = [term for term in expected_response.forbidden_terms if term.casefold() in folded]
    checks.append(
        _check(
            "response.forbidden_terms",
            not leaked,
            "response did not expose forbidden implementation details",
            f"response contained forbidden terms: {', '.join(leaked)}",
        ),
    )
    if expected_response.require_question:
        checks.append(
            _check(
                "response.question",
                "?" in response or "？" in response,
                "response asked a question",
                "response did not contain a question",
            ),
        )
    if expected_response.max_chars is not None:
        checks.append(
            _check(
                "response.length",
                len(response) <= expected_response.max_chars,
                "response stayed within the length limit",
                f"response length {len(response)} exceeded {expected_response.max_chars}",
            ),
        )

    within_model_limit = observation.model_calls <= case.limits.max_model_calls
    checks.append(
        _check(
            "limits.model_calls",
            within_model_limit,
            "model call limit was respected",
            f"model calls {observation.model_calls} exceeded {case.limits.max_model_calls}",
        ),
    )
    within_tool_limit = len(observation.tool_calls) <= case.limits.max_tool_calls
    checks.append(
        _check(
            "limits.tool_calls",
            within_tool_limit,
            "tool call limit was respected",
            f"tool calls {len(observation.tool_calls)} exceeded {case.limits.max_tool_calls}",
        ),
    )

    status = EvaluationStatus.PASS if all(check.passed for check in checks) else EvaluationStatus.FAIL
    return CaseResult(
        case_id=case.id,
        suite=case.suite,
        repeat_index=repeat_index,
        status=status,
        checks=checks,
        observation=observation,
        metrics={
            "duration_ms": observation.duration_ms,
            "model_calls": observation.model_calls,
            "tool_calls": len(observation.tool_calls),
            "input_tokens": observation.token_usage.input_tokens,
            "output_tokens": observation.token_usage.output_tokens,
            "total_tokens": observation.token_usage.total_tokens,
        },
    )

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from vla_data_juicer_agents.evaluation.cases import (
    CaseLoadError,
    default_cases_root,
    load_suite,
)
from vla_data_juicer_agents.evaluation.grading import grade_case
from vla_data_juicer_agents.evaluation.models import (
    CaseResult,
    CaseRunObservation,
    EvaluationCase,
    EvaluationStatus,
    GradingCheck,
    TokenUsage,
    ToolCallObservation,
)
from vla_data_juicer_agents.evaluation.reporting import (
    write_aggregate_report,
    write_baseline_reports,
)


SUITE = "router-smoke"


def _cases() -> dict[str, EvaluationCase]:
    return {case.id: case for case in load_suite(default_cases_root(), SUITE)}


def test_load_router_smoke_suite_with_strict_versioned_schema():
    cases = _cases()

    assert set(cases) == {
        "router_capability_no_handoff",
        "router_missing_target_clarifies",
        "router_shortcut_current_template",
        "router_shortcut_preserves_scope",
    }
    assert all(case.schema_version == 1 for case in cases.values())
    assert all(case.entrypoint == "router" for case in cases.values())


def test_case_schema_rejects_unknown_fields():
    raw = _cases()["router_capability_no_handoff"].model_dump(mode="python")
    raw["surprise"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvaluationCase.model_validate(raw)


@pytest.mark.parametrize("entrypoint", ["navigation", "end_to_end"])
def test_v1_case_schema_rejects_unimplemented_entrypoints(entrypoint):
    raw = _cases()["router_capability_no_handoff"].model_dump(mode="python")
    raw["entrypoint"] = entrypoint

    with pytest.raises(ValidationError, match="literal_error"):
        EvaluationCase.model_validate(raw)


@pytest.mark.parametrize(
    "conversation",
    [
        [
            {"role": "assistant", "content": "请提供日期。"},
            {"role": "user", "content": "20260710"},
        ],
        [
            {"role": "user", "content": "请处理导航数据。"},
            {"role": "user", "content": "日期是 20260710。"},
        ],
        [{"role": "assistant", "content": "请提供日期。"}],
    ],
)
def test_v1_case_schema_rejects_conversation_history(conversation):
    raw = _cases()["router_capability_no_handoff"].model_dump(mode="python")
    raw["conversation"] = conversation

    with pytest.raises(ValidationError, match="exactly one user conversation turn"):
        EvaluationCase.model_validate(raw)


def test_loader_rejects_duplicate_ids(tmp_path: Path):
    suite_dir = tmp_path / SUITE
    suite_dir.mkdir()
    raw = _cases()["router_capability_no_handoff"].model_dump(mode="python")
    (suite_dir / "one.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    (suite_dir / "two.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(CaseLoadError, match="duplicate evaluation case id"):
        load_suite(tmp_path, SUITE)


def test_loader_rejects_unknown_or_mismatched_suite(tmp_path: Path):
    with pytest.raises(CaseLoadError, match="has no case files"):
        load_suite(tmp_path, "missing-suite")

    suite_dir = tmp_path / SUITE
    suite_dir.mkdir()
    raw = _cases()["router_capability_no_handoff"].model_dump(mode="python")
    raw["suite"] = "another-suite"
    (suite_dir / "case.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(CaseLoadError, match="declares suite"):
        load_suite(tmp_path, SUITE)

    with pytest.raises(CaseLoadError, match="invalid evaluation suite name"):
        load_suite(tmp_path, "../outside")


def test_capability_case_passes_without_tools_and_fails_on_internal_tool_use():
    case = _cases()["router_capability_no_handoff"]
    passing = grade_case(
        case,
        CaseRunObservation(
            final_response="我是 DataPilot，可以帮助你检查和处理 VLA 导航数据。",
            model_calls=1,
        ),
    )
    assert passing.status is EvaluationStatus.PASS

    failing = grade_case(
        case,
        CaseRunObservation(
            final_response="我会调用 start_navigation_data_task 处理数据。",
            tool_calls=[ToolCallObservation(name="Bash", arguments={"command": "pwd"})],
            forbidden_calls=["Bash"],
            model_calls=1,
        ),
    )
    assert failing.status is EvaluationStatus.FAIL
    assert {check.name for check in failing.checks if not check.passed} >= {
        "tools.allowed",
        "tools.safety",
        "response.forbidden_terms",
    }


def test_missing_target_case_requires_short_chinese_question_and_no_handoff():
    case = _cases()["router_missing_target_clarifies"]
    passing = grade_case(
        case,
        CaseRunObservation(final_response="请提供要处理的数据日期或路径？", model_calls=1),
    )
    assert passing.status is EvaluationStatus.PASS

    failing = grade_case(
        case,
        CaseRunObservation(final_response="好的，我已经开始处理。", model_calls=1),
    )
    assert failing.status is EvaluationStatus.FAIL
    failed_names = {check.name for check in failing.checks if not check.passed}
    assert "response.required_group.0" in failed_names
    assert "response.question" in failed_names


@pytest.mark.parametrize(
    "case_id",
    ["router_shortcut_preserves_scope", "router_shortcut_current_template"],
)
def test_shortcut_cases_require_exact_single_handoff(case_id):
    case = _cases()[case_id]
    expected = case.expectations.tools.handoff
    assert expected is not None
    payload = {
        "request": expected.request,
        "target": expected.target,
        "date": expected.date,
        "clips": expected.clips,
        "reason": "这是具体的导航数据处理请求",
        "missing_fields": expected.missing_fields,
        "confidence": "high",
        "response_language": expected.response_language,
    }
    passing = grade_case(
        case,
        CaseRunObservation(
            final_response="导航数据任务已启动。",
            tool_calls=[ToolCallObservation(name="start_navigation_data_task", arguments=payload)],
            handoffs=[payload],
            model_calls=2,
        ),
    )
    assert passing.status is EvaluationStatus.PASS

    bad_payload = {**payload, "clips": list(reversed(expected.clips)), "segments": expected.clips}
    failing = grade_case(
        case,
        CaseRunObservation(
            final_response="导航数据任务已启动。",
            tool_calls=[ToolCallObservation(name="start_navigation_data_task", arguments=bad_payload)],
            handoffs=[bad_payload],
            model_calls=2,
        ),
    )
    assert failing.status is EvaluationStatus.FAIL
    failed_names = {check.name for check in failing.checks if not check.passed}
    assert "handoff.clips" in failed_names
    assert "handoff.forbidden_fields" in failed_names


def test_current_shortcut_case_matches_template_without_artifact_hint():
    case = _cases()["router_shortcut_current_template"]
    expected = case.expectations.tools.handoff

    assert expected is not None
    assert case.conversation[0].content == expected.request
    assert "请先检查当前实际产物状态" not in expected.request


def test_case_result_distinguishes_timeout_and_error():
    case = _cases()["router_capability_no_handoff"]
    timeout = CaseResult.timeout(case, message="hard deadline")
    error = CaseResult.error(case, error=RuntimeError("provider unavailable"))

    assert timeout.status is EvaluationStatus.TIMEOUT
    assert timeout.error_type == "TimeoutError"
    assert error.status is EvaluationStatus.ERROR
    assert error.error_type == "RuntimeError"


def test_reports_keep_full_local_result_out_of_compact_baseline(tmp_path: Path):
    case = _cases()["router_shortcut_preserves_scope"]
    secret_response = "完整模型回复-不可进入baseline"
    handoff = {"request": "完整请求-不可进入baseline", "date": "20260710"}
    observation = CaseRunObservation(
        final_response=secret_response,
        handoffs=[handoff],
        events=[{"type": "REPLY_END", "content": "完整事件-不可进入baseline"}],
        token_usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        model_calls=1,
        duration_ms=25,
    )
    result = CaseResult(
        case_id=case.id,
        suite=case.suite,
        status=EvaluationStatus.FAIL,
        checks=[
            GradingCheck(
                name="handoff.request",
                passed=False,
                message="handoff request mismatch",
            ),
        ],
        observation=observation,
        error_message="model behavior failed at /private/tmp/case with sk-supersecret123",
        metrics={"model_calls": 1, "tool_calls": 0, "total_tokens": 15},
    )

    aggregate_path = write_aggregate_report([result], tmp_path / "aggregate.json")
    json_path, markdown_path = write_baseline_reports(
        [result],
        tmp_path / "baseline.json",
        tmp_path / "baseline.md",
        run_metadata={
            "git_commit": "abc123",
            "evaluation_contract_version": 2,
            "model": "qwen-test",
            "model_parameters": {"parallel_tool_calls": False},
            "agentscope_version": "2.0.1",
            "cases_sha256": "casehash",
            "prompt_sha256": "deadbeef",
            "tool_schema_sha256": ["toolhash"],
            "run_id": "must-not-enter-baseline",
            "started_at": "must-not-enter-baseline",
        },
    )

    aggregate_text = aggregate_path.read_text(encoding="utf-8")
    baseline_text = json_path.read_text(encoding="utf-8")
    assert secret_response in aggregate_text
    assert "完整事件-不可进入baseline" in aggregate_text
    assert secret_response not in baseline_text
    assert "完整事件-不可进入baseline" not in baseline_text
    assert "完整请求-不可进入baseline" not in baseline_text
    baseline = json.loads(baseline_text)
    assert baseline["schema_version"] == 2
    assert baseline["summary"]["case_count"] == 1
    assert baseline["summary"]["stability_counts"]["SINGLE_SAMPLE"] == 1
    assert baseline["case_summaries"][0]["stability_status"] == "SINGLE_SAMPLE"
    assert baseline["case_summaries"][0]["failure_signatures"] == {
        "handoff.request": 1,
    }
    assert baseline["results"][0]["failure_signatures"] == ["handoff.request"]
    assert "failure_reasons" not in baseline["results"][0]
    assert "response" not in baseline["results"][0]
    assert "trace" not in baseline["results"][0]
    assert baseline["results"][0]["metrics"]["response_chars"] == len(secret_response)
    assert baseline["run_metadata"]["prompt_sha256"] == "deadbeef"
    assert baseline["run_metadata"]["evaluation_contract_version"] == 2
    assert "run_id" not in baseline["run_metadata"]
    assert "started_at" not in baseline["run_metadata"]
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "model behavior failed" not in markdown
    assert "handoff.request" in markdown
    assert "abc123" in markdown
    assert "Evaluation contract: `2`" in markdown
    assert "deadbeef" in markdown
    assert "toolhash" in markdown
    assert "/private/tmp/case" not in markdown
    assert "sk-supersecret123" not in markdown

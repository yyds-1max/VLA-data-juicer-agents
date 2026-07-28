from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from vla_data_juicer_agents.evaluation.cases import (
    CaseLoadError,
    cases_sha256,
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
V1_SUITE = "datapilot-v1"
M2_SUITE = "navigation-m2"


def _cases() -> dict[str, EvaluationCase]:
    return {case.id: case for case in load_suite(default_cases_root(), SUITE)}


def _v1_cases() -> dict[str, EvaluationCase]:
    return {case.id: case for case in load_suite(default_cases_root(), V1_SUITE)}


def _m2_cases() -> dict[str, EvaluationCase]:
    return {case.id: case for case in load_suite(default_cases_root(), M2_SUITE)}


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


def test_load_datapilot_v1_suite_without_rewriting_historical_suite():
    cases = _v1_cases()

    assert set(cases) == {
        "router_active_unrelated_direct",
        "router_capability_direct",
        "router_clarify_date_preserves_selected_clip_multiturn",
        "router_missing_date_clarifies",
        "router_new_task_conflict_direct",
        "router_resume_paused",
        "router_shortcut_trusted_context_exact_scope",
        "router_start_date_all_clips",
        "router_start_selected_multiple_clips",
        "router_start_selected_cross_date_prefix",
        "router_status_query_direct",
        "router_continue_waiting_task",
        "router_control_stop",
        "router_control_cancel",
        "router_start_then_stop_multiturn",
        "router_navigation_never_uses_generic_tools",
        "router_waiting_rejects_postprocessing",
    }
    assert all(case.schema_version == 2 for case in cases.values())
    assert len(cases["router_start_then_stop_multiturn"].conversation) == 2
    assert len(
        cases["router_clarify_date_preserves_selected_clip_multiturn"].conversation,
    ) == 2
    assert cases["router_continue_waiting_task"].runtime_setup is not None
    shortcut_setup = cases[
        "router_shortcut_trusted_context_exact_scope"
    ].runtime_setup
    assert shortcut_setup is not None
    assert shortcut_setup.request_context is not None
    assert shortcut_setup.request_context.selection == {
        "kind": "selected_clips",
        "clips": ["20260605_152856", "route_A_07"],
    }


def test_datapilot_v1_case_hash_ignores_m2_model_extensions():
    cases = list(_v1_cases().values())

    # This is the exact hash produced at the M1.5 base commit after the
    # separately approved conflict-synonym case expansion. M2's shared model
    # defaults must not move it again.
    assert (
        cases_sha256(cases)
        == "cbba56291d0688aa21050fffc9181fbd93b71183a860e24085ddcbe783c47eb6"
    )


def test_load_navigation_m2_suite_with_router_and_specialist_entrypoints():
    cases = _m2_cases()

    assert set(cases) == {
        "router_annotation_processing_shortcut",
        "router_annotation_processing_and_fix_shortcut",
        "router_continue_linked_fix",
        "router_decline_linked_fix",
        "navigation_postprocessing_existing_gridmap_odom",
        "navigation_postprocessing_generate_gridmap_odom",
        "navigation_trajectory_review_handoff",
    }
    assert all(case.schema_version == 2 for case in cases.values())
    assert {
        case.entrypoint for case in cases.values()
    } == {"router", "navigation"}
    pcd_case = cases["navigation_postprocessing_generate_gridmap_odom"]
    handoff = pcd_case.expectations.tools.handoff
    assert handoff is not None
    assert handoff.step_variants == {
        "prepare_gridmap_for_projection": "generate_from_pcd",
        "run_projection_and_trajectory": "cjl_0525_with_gridmap",
        "validate_navigation_outputs": "expect_gridmap",
    }


def test_case_schema_rejects_unknown_fields():
    raw = _cases()["router_capability_no_handoff"].model_dump(mode="python")
    raw["surprise"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvaluationCase.model_validate(raw)


@pytest.mark.parametrize("entrypoint", ["end_to_end"])
def test_v1_case_schema_rejects_unimplemented_entrypoints(entrypoint):
    raw = _cases()["router_capability_no_handoff"].model_dump(mode="python")
    raw["entrypoint"] = entrypoint

    with pytest.raises(ValidationError, match="literal_error"):
        EvaluationCase.model_validate(raw)


def test_navigation_case_schema_requires_navigation_runtime_setup():
    raw = _cases()["router_capability_no_handoff"].model_dump(mode="python")
    raw["schema_version"] = 2
    raw["entrypoint"] = "navigation"

    with pytest.raises(
        ValidationError,
        match="require runtime_setup.navigation_task",
    ):
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


def test_v2_case_schema_accepts_user_multiturn_and_rejects_seeded_assistant_turns():
    case = _v1_cases()["router_start_then_stop_multiturn"]
    assert [turn.role for turn in case.conversation] == ["user", "user"]

    raw = case.model_dump(mode="python")
    raw["conversation"].insert(1, {"role": "assistant", "content": "已启动。"})
    with pytest.raises(ValidationError, match="assistant turns are produced by the host"):
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


@pytest.mark.parametrize(
    ("case_id", "payload"),
    [
        (
            "router_start_date_all_clips",
            {
                "operation": "start",
                "scope_source": "interpreted_user_text",
                "dataset_date": "20270605",
                "selection": {"kind": "all_clips"},
            },
        ),
        (
            "router_start_selected_cross_date_prefix",
            {
                "operation": "start",
                "scope_source": "interpreted_user_text",
                "dataset_date": "20270605",
                "selection": {
                    "kind": "selected_clips",
                    "clips": ["20260605_152856"],
                },
            },
        ),
        (
            "router_start_selected_multiple_clips",
            {
                "operation": "start",
                "scope_source": "interpreted_user_text",
                "dataset_date": "20270605",
                "selection": {
                    "kind": "selected_clips",
                    "clips": [
                        "20260605_152856",
                        "20260605_153012",
                        "route_A_07",
                    ],
                },
            },
        ),
        (
            "router_clarify_date_preserves_selected_clip_multiturn",
            {
                "operation": "start",
                "scope_source": "interpreted_user_text",
                "dataset_date": "20270605",
                "selection": {
                    "kind": "selected_clips",
                    "clips": ["20260605_152856"],
                },
            },
        ),
        (
            "router_shortcut_trusted_context_exact_scope",
            {
                "operation": "start",
                "scope_source": "request_context",
                "dataset_date": "20270605",
                "selection": {
                    "kind": "selected_clips",
                    "clips": ["20260605_152856", "route_A_07"],
                },
            },
        ),
        (
            "router_continue_waiting_task",
            {"operation": "continue", "status": "active"},
        ),
        (
            "router_waiting_rejects_postprocessing",
            {"operation": "continue", "status": "active"},
        ),
        (
            "router_resume_paused",
            {"operation": "continue", "status": "active"},
        ),
        (
            "router_control_stop",
            {"operation": "stop", "status": "pausing"},
        ),
        (
            "router_control_cancel",
            {"operation": "cancel", "status": "cancelling"},
        ),
    ],
)
def test_datapilot_v1_cases_grade_production_operation_payloads(case_id, payload):
    case = _v1_cases()[case_id]
    tool_name = next(iter(case.expectations.tools.required_counts))
    final_response = (
        "请提供数据目录日期（YYYYMMDD）？"
        if case_id == "router_clarify_date_preserves_selected_clip_multiturn"
        else ""
    )
    result = grade_case(
        case,
        CaseRunObservation(
            final_response=final_response,
            tool_calls=[ToolCallObservation(name=tool_name, arguments=payload)],
            handoffs=[payload],
            model_calls=1,
        ),
    )

    assert result.status is EvaluationStatus.PASS


@pytest.mark.parametrize(
    ("case_id", "response"),
    [
        (
            "router_status_query_direct",
            "当前任务仍在处理中，正处于准备阶段。",
        ),
        (
            "router_active_unrelated_direct",
            "机器学习是让模型从数据中发现规律并用于预测的方法。",
        ),
        (
            "router_new_task_conflict_direct",
            "当前任务仍在处理，请先等待它完成或取消，再创建新任务。",
        ),
    ],
)
def test_datapilot_v1_contextual_direct_answers_are_strictly_tool_free(
    case_id,
    response,
):
    case = _v1_cases()[case_id]
    passing = grade_case(
        case,
        CaseRunObservation(final_response=response, model_calls=1),
    )
    assert passing.status is EvaluationStatus.PASS

    failing = grade_case(
        case,
        CaseRunObservation(
            final_response=response,
            tool_calls=[
                ToolCallObservation(
                    name="start_navigation_data_task",
                    arguments={},
                ),
            ],
            model_calls=1,
        ),
    )
    assert failing.status is EvaluationStatus.FAIL
    failed_names = {check.name for check in failing.checks if not check.passed}
    assert {"tools.allowed", "limits.tool_calls"} <= failed_names


def test_navigation_plan_grading_requires_exact_business_variants():
    case = _m2_cases()["navigation_postprocessing_generate_gridmap_odom"]
    expected = case.expectations.tools.handoff
    assert expected is not None
    payload = {
        "operation": "submit_plan",
        "phase": expected.phase,
        "decision_modes": expected.decision_modes,
        "step_actions": expected.step_actions,
        "step_variants": expected.step_variants,
    }
    passing = grade_case(
        case,
        CaseRunObservation(
            final_response="后处理计划已验证，系统已开始执行。",
            tool_calls=[
                ToolCallObservation(
                    name="submit_finish_processing_plan_tool",
                    arguments={},
                ),
            ],
            handoffs=[payload],
            model_calls=1,
        ),
    )
    assert passing.status is EvaluationStatus.PASS

    wrong_variant = dict(payload)
    wrong_variant["step_variants"] = {
        **dict(expected.step_variants or {}),
        "run_projection_and_trajectory": "cjl_with_gridmap",
    }
    failing = grade_case(
        case,
        CaseRunObservation(
            final_response="后处理计划已验证，系统已开始执行。",
            tool_calls=[
                ToolCallObservation(
                    name="submit_finish_processing_plan_tool",
                    arguments={},
                ),
            ],
            handoffs=[wrong_variant],
            model_calls=1,
        ),
    )
    assert failing.status is EvaluationStatus.FAIL
    assert {
        check.name for check in failing.checks if not check.passed
    } == {"handoff.step_variants"}


def test_runtime_setup_rejects_mixed_focused_task_and_trusted_request_context():
    case = _v1_cases()["router_shortcut_trusted_context_exact_scope"]
    raw = case.model_dump(mode="python")
    raw["runtime_setup"]["focused_task"] = {
        "task_ref": "DP-EVAL-FOCUSED",
        "status": "active",
        "dataset_date": "20270605",
        "selection": {"kind": "all_clips"},
    }

    with pytest.raises(ValidationError, match="are exclusive"):
        EvaluationCase.model_validate(raw)


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

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from vla_data_juicer_agents.runtime.public_contract import (
    ActionSignalV1,
    ActionVisibility,
    ArtifactSignalV1,
    FinalSignalV1,
    InteractionOptionV1,
    InternalSignalV1,
    ProgressSignalV1,
    PublicActionRegistry,
    PublicEventV1,
    RequestInputSignalV1,
    SpecialistSignalProjector,
    final_fallback,
    sanitize_final_text,
    sanitize_progress_text,
    validate_specialist_signal,
)


TASK_REF = "task_public_7Gk2mQ"


def _base(*, signal_id: str = "sig-1", sequence: int = 1) -> dict[str, Any]:
    return {
        "version": 1,
        "signal_id": signal_id,
        "sequence": sequence,
        "web_session_id": "web-internal-1",
        "turn_id": "turn-internal-1",
        "task_id": "task-internal-1",
        "run_id": "run-internal-1",
    }


def _progress(
    *,
    operation: str = "append",
    phase: str = "inspection",
    text: str | None = "正在检查数据。",
    status: str = "running",
    signal_id: str = "sig-progress",
    turn_id: str = "turn-internal-1",
    **extra: Any,
) -> dict[str, Any]:
    return {
        **_base(signal_id=signal_id),
        "turn_id": turn_id,
        "kind": "progress",
        "operation": operation,
        "phase": phase,
        "text": text,
        "status": status,
        **extra,
    }


def _action(
    tool_name: str,
    *,
    operation: str = "start",
    status: str = "running",
    call_id: str = "private-call-1",
    **extra: Any,
) -> ActionSignalV1:
    return ActionSignalV1.model_validate(
        {
            **_base(signal_id=f"sig-{call_id}"),
            "kind": "action",
            "operation": operation,
            "tool_name": tool_name,
            "call_id": call_id,
            "status": status,
            **extra,
        }
    )


def test_specialist_signal_is_strict_versioned_and_discriminated() -> None:
    parsed = validate_specialist_signal(_progress(operation="start"))
    assert isinstance(parsed, ProgressSignalV1)
    assert parsed.version == 1

    with pytest.raises(ValidationError):
        validate_specialist_signal({**_progress(), "version": 2})
    missing_version = _progress()
    missing_version.pop("version")
    with pytest.raises(ValidationError):
        validate_specialist_signal(missing_version)
    with pytest.raises(ValidationError):
        validate_specialist_signal({**_progress(), "unexpected": True})
    with pytest.raises(ValidationError):
        validate_specialist_signal({**_progress(), "kind": "thought"})


def test_signal_models_reject_inconsistent_counts_and_states() -> None:
    with pytest.raises(ValidationError, match="provided together"):
        validate_specialist_signal(_progress(done=1))
    with pytest.raises(ValidationError, match="must not exceed"):
        validate_specialist_signal(_progress(done=4, total=3))
    with pytest.raises(ValidationError, match="terminal status"):
        validate_specialist_signal(_progress(status="failed"))
    with pytest.raises(ValidationError, match="must have a terminal status"):
        _action(
            "validate_navigation_outputs_tool",
            operation="finish",
            status="running",
        )


def test_all_signal_variants_parse_and_internal_signal_is_never_public() -> None:
    options = (InteractionOptionV1(option_id="confirm", label="确认"),)
    signals = [
        _action("prepare_raw_data_tool"),
        RequestInputSignalV1(
            **_base(signal_id="sig-input"),
            kind="request_input",
            interaction_ref="interaction_A12",
            interaction_kind="high_risk_confirmation",
            risk="high",
            title="确认覆盖",
            summary="请确认是否继续。",
            options=options,
            interaction_revision=1,
            expected_task_revision=3,
        ),
        FinalSignalV1(
            **_base(signal_id="sig-final"),
            kind="final",
            text="处理完成。",
            task_status="completed",
        ),
        ArtifactSignalV1(
            **_base(signal_id="sig-artifact"),
            kind="artifact",
            artifact_ref="artifact_ABC123",
            label="处理结果",
            internal_path="/Users/sfy/private/result.json",
        ),
        InternalSignalV1(
            **_base(signal_id="sig-internal"),
            kind="internal",
            category="model_trace",
            detail={"tool": "private_tool", "call_id": "secret"},
        ),
    ]
    projector = SpecialistSignalProjector()
    assert [validate_specialist_signal(item).kind for item in signals] == [
        "action",
        "request_input",
        "final",
        "artifact",
        "internal",
    ]
    assert projector.project(signals[-1], task_ref=TASK_REF) == []


def test_registry_is_exact_and_unknown_tool_uses_non_echoing_fallback() -> None:
    registry = PublicActionRegistry()
    inspection = registry.resolve("inspect_navigation_raw_metadata_tool")
    assert inspection.visibility == ActionVisibility.GROUPED
    assert inspection.action_code == "inspect_data"
    assert registry.resolve("get_current_plan_step_tool").visibility == ActionVisibility.SILENT

    unknown = registry.resolve("dangerous_secret_tool")
    assert unknown.visibility == ActionVisibility.GROUPED
    assert unknown.action_code == "processing_step"
    assert unknown.display_name == "执行处理步骤"
    assert "dangerous" not in repr(unknown)


def test_action_projection_never_exposes_tool_call_run_agent_or_internal_task() -> None:
    projector = SpecialistSignalProjector()
    event = projector.project(
        _action(
            "extract_and_sync_navigation_data_tool",
            call_id="call-secret-123",
            message="正在处理 3/8 个数据段。",
            done=3,
            total=8,
            unit="个数据段",
        ),
        task_ref=TASK_REF,
    )[0]
    dumped = event.model_dump_json()

    assert event.type == "action_start"
    assert event.payload["action_code"] == "extract_sync"
    assert event.payload["count"] == {
        "done": 3,
        "total": 8,
        "unit": "个数据段",
    }
    for private_value in (
        "extract_and_sync_navigation_data_tool",
        "call-secret-123",
        "run-internal-1",
        "task-internal-1",
        "NavigationDataAgent",
    ):
        assert private_value not in dumped
    assert "%" not in dumped


def test_background_action_finishes_as_transferred_without_exposing_tool() -> None:
    projector = SpecialistSignalProjector()
    projector.project(
        _action(
            "extract_and_sync_navigation_data_tool",
            call_id="call-private-background",
        ),
        task_ref=TASK_REF,
    )

    event = projector.project(
        _action(
            "extract_and_sync_navigation_data_tool",
            operation="finish",
            status="background",
            call_id="call-private-background",
            signal_id="sig-background-finish",
        ),
        task_ref=TASK_REF,
    )[0]

    assert event.type == "action_end"
    assert event.payload["status"] == "background"
    assert "extract_and_sync_navigation_data_tool" not in event.model_dump_json()


def test_grouped_actions_share_a_public_reference_and_unknown_names_do_not_leak() -> None:
    projector = SpecialistSignalProjector()
    first = projector.project(
        _action("inspect_navigation_raw_metadata_tool", call_id="call-a"),
        task_ref=TASK_REF,
    )[0]
    second = projector.project(
        _action("inspect_navigation_topic_candidates_tool", call_id="call-b"),
        task_ref=TASK_REF,
    )[0]
    unknown = projector.project(
        _action("top_secret_database_tool", call_id="call-c"),
        task_ref=TASK_REF,
    )[0]

    assert first.payload["action_ref"] == second.payload["action_ref"]
    assert first.payload["action_ref"] != unknown.payload["action_ref"]
    assert unknown.payload["display_name"] == "执行处理步骤"
    assert "top_secret" not in unknown.model_dump_json()


def test_replayed_signal_id_is_idempotently_ignored() -> None:
    projector = SpecialistSignalProjector()
    signal = _action("prepare_raw_data_tool", call_id="call-replayed")
    assert projector.project(signal, task_ref=TASK_REF)
    assert projector.project(signal, task_ref=TASK_REF) == []


def test_silent_action_does_not_create_public_event() -> None:
    projector = SpecialistSignalProjector()
    assert projector.project(
        _action("get_plan_execution_overview_tool"),
        task_ref=TASK_REF,
    ) == []


def test_progress_text_is_redacted_bounded_two_sentences_and_has_no_percentage() -> None:
    text = (
        "NavigationDataAgent 正在调用 inspect_navigation_raw_metadata_tool，"
        "路径 /Users/sfy/private/raw，task_id: task-secret，进度 50%。"
        "Bearer abcdefghijk。第三句不应显示。"
    )
    safe = sanitize_progress_text(text)

    assert len(safe) <= 240
    assert "NavigationDataAgent" not in safe
    assert "inspect_navigation_raw_metadata_tool" not in safe
    assert "/Users/sfy" not in safe
    assert "task-secret" not in safe
    assert "Bearer" not in safe
    assert "50%" not in safe
    assert "第三句" not in safe


def test_progress_is_deduplicated_coalesced_and_limited_to_two_updates_per_phase() -> None:
    projector = SpecialistSignalProjector()
    first = projector.project(
        _progress(operation="start", text="开始检查数据。"),
        task_ref=TASK_REF,
        now=0.0,
    )
    duplicate = projector.project(
        _progress(text="开始检查数据。", signal_id="sig-duplicate"),
        task_ref=TASK_REF,
        now=0.5,
    )
    pending_a = projector.project(
        _progress(text="已找到原始数据。", signal_id="sig-a"),
        task_ref=TASK_REF,
        now=1.0,
    )
    pending_b = projector.project(
        _progress(text="正在核对传感器。", signal_id="sig-b", done=3, total=8),
        task_ref=TASK_REF,
        now=1.5,
    )
    coalesced = projector.flush(now=2.1)
    over_limit = projector.project(
        _progress(text="这一条不应再公开。", signal_id="sig-c"),
        task_ref=TASK_REF,
        now=5.0,
    )

    assert [event.type for event in first] == ["progress_start"]
    assert duplicate == []
    assert pending_a == []
    assert pending_b == []
    assert [event.type for event in coalesced] == ["progress_delta"]
    assert "已找到原始数据" in coalesced[0].payload["summary"]
    assert "正在核对传感器" in coalesced[0].payload["summary"]
    assert coalesced[0].payload["count"] == {"done": 3, "total": 8, "unit": "项"}
    assert over_limit == []


def test_progress_limit_is_eight_text_updates_per_turn_but_state_events_continue() -> None:
    phases = [
        "setup",
        "inspection",
        "planning",
        "preparation",
        "extract_sync",
        "finish_assembly",
        "annotation",
        "tracking",
        "projection",
    ]
    projector = SpecialistSignalProjector()
    events = []
    for index, phase in enumerate(phases):
        events.extend(
            projector.project(
                _progress(
                    operation="start",
                    phase=phase,
                    text=f"阶段 {index} 已开始。",
                    signal_id=f"sig-{index}",
                ),
                task_ref=TASK_REF,
                now=float(index * 3),
            )
        )

    assert len(events) == 9
    assert sum("summary" in event.payload for event in events) == 8
    assert "summary" not in events[-1].payload


def test_progress_finish_flushes_pending_before_terminal_state() -> None:
    projector = SpecialistSignalProjector()
    projector.project(
        _progress(operation="start", text=None),
        task_ref=TASK_REF,
        now=0.0,
    )
    projector.project(
        _progress(text="处理即将完成。", signal_id="sig-pending"),
        task_ref=TASK_REF,
        now=0.5,
    )
    events = projector.project(
        _progress(
            operation="finish",
            text=None,
            status="completed",
            signal_id="sig-finish",
        ),
        task_ref=TASK_REF,
        now=0.6,
    )
    assert [event.type for event in events] == ["progress_delta", "progress_end"]
    assert events[-1].payload["status"] == "completed"


def test_finish_without_start_has_valid_event_order() -> None:
    projector = SpecialistSignalProjector()
    events = projector.project(
        _progress(
            operation="finish",
            text="检查完成。",
            status="completed",
        ),
        task_ref=TASK_REF,
        now=0.0,
    )
    assert [event.type for event in events] == [
        "progress_start",
        "progress_delta",
        "progress_end",
    ]


def test_request_input_is_sanitized_and_artifact_never_exposes_internal_path() -> None:
    projector = SpecialistSignalProjector()
    request = RequestInputSignalV1(
        **_base(signal_id="sig-input"),
        kind="request_input",
        interaction_ref="interaction_A12",
        interaction_kind="calibration_preview",
        risk="high",
        title="确认 /Users/sfy/private/config.yaml 中的参数",
        summary="由 NavigationDataAgent 提交，令牌=abc123。",
        options=(
            InteractionOptionV1(
                option_id="confirm",
                label="确认",
                description="调用 confirm_navigation_calibration_params_tool",
            ),
            InteractionOptionV1(option_id="reject", label="返回修改"),
        ),
        interaction_revision=2,
        expected_task_revision=4,
    )
    interaction_event = projector.project(request, task_ref=TASK_REF)[0]
    artifact_event = projector.project(
        ArtifactSignalV1(
            **_base(signal_id="sig-artifact"),
            kind="artifact",
            artifact_ref="artifact_ABC123",
            label="结果 /Users/sfy/private/out.json",
            media_type="application/json",
            internal_path="/Users/sfy/private/out.json",
        ),
        task_ref=TASK_REF,
    )[0]
    dumped = interaction_event.model_dump_json() + artifact_event.model_dump_json()

    assert interaction_event.type == "interaction_required"
    assert artifact_event.type == "artifact_ready"
    assert artifact_event.payload["artifact_ref"] == "artifact_ABC123"
    assert "internal_path" not in artifact_event.payload
    assert "/Users/sfy" not in dumped
    assert "NavigationDataAgent" not in dumped
    assert "confirm_navigation_calibration_params_tool" not in dumped


def test_final_is_sanitized_and_empty_final_uses_task_state_fallback() -> None:
    projector = SpecialistSignalProjector()
    safe_event = projector.project(
        FinalSignalV1(
            **_base(signal_id="sig-final-safe"),
            kind="final",
            text=(
                "任务完成。\n"
                "输出位于 /Users/sfy/private/out.json。\n"
                "run_id: run-secret\n"
                "处理了 3/8 个数据段。"
            ),
            task_status="completed",
        ),
        task_ref=TASK_REF,
    )[-1]
    fallback_event = projector.project(
        FinalSignalV1(
            **{**_base(signal_id="sig-final-empty"), "turn_id": "turn-internal-2"},
            kind="final",
            text="50%",
            task_status="needs_replan",
        ),
        task_ref=TASK_REF,
    )[-1]

    assert safe_event.type == "final"
    assert "任务完成" in safe_event.payload["text"]
    assert "3/8 个数据段" in safe_event.payload["text"]
    assert "/Users/sfy" not in safe_event.model_dump_json()
    assert "run-secret" not in safe_event.model_dump_json()
    assert fallback_event.payload["text"] == final_fallback("needs_replan")
    assert "%" not in fallback_event.model_dump_json()


def test_public_event_rejects_forbidden_fields_and_unsafe_text() -> None:
    with pytest.raises(ValidationError, match="forbidden keys"):
        PublicEventV1(
            type="action_start",
            task_ref=TASK_REF,
            payload={"tool_name": "secret_tool"},
        )
    with pytest.raises(ValidationError, match="unsafe text"):
        PublicEventV1(
            type="progress_delta",
            task_ref=TASK_REF,
            payload={"summary": "已经完成 50%"},
        )
    with pytest.raises(ValidationError):
        PublicEventV1(
            type="private_trace",  # type: ignore[arg-type]
            task_ref=TASK_REF,
            payload={},
        )


def test_final_sanitizer_preserves_counts_but_removes_paths_credentials_and_ids() -> None:
    text = sanitize_final_text(
        "完成 3/8 个数据段。\n"
        "C:\\private\\result.json\n"
        "\\\\server\\private-share\\result.json\n"
        "/custom-mounted-volume/project/result.json\n"
        "api_key=super-secret\n"
        "call_id: abcdefgh\n"
        "0123456789abcdef0123456789abcdef"
    )
    assert "3/8 个数据段" in text
    assert "C:\\private" not in text
    assert "server" not in text
    assert "custom-mounted-volume" not in text
    assert "super-secret" not in text
    assert "abcdefgh" not in text
    assert "0123456789abcdef" not in text

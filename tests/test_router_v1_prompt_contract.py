from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator

from vla_data_juicer_agents.runtime.agentscope_prompts import main_router_v1_prompt
from vla_data_juicer_agents.runtime.single_agent import (
    ContinueNavigationDataTaskV1Tool,
    ControlNavigationDataTaskV1Tool,
    RouterContractV1Middleware,
    StartNavigationDataTaskV1Tool,
)


class _Runtime:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.continues: list[dict[str, Any]] = []
        self.controls: list[dict[str, Any]] = []

    @staticmethod
    def router_context_envelope(
        _web_session_id: str,
        *,
        router_session_id: str | None = None,
    ) -> dict[str, Any]:
        del router_session_id
        return {
            "contract_version": 1,
            "focused_task_summary": {
                "status": "waiting_user",
                "available_actions": ["provide_input", "cancel"],
            },
        }

    async def start_navigation_agent_task_v1(self, **kwargs: Any) -> dict[str, str]:
        self.starts.append(dict(kwargs))
        return {"task_ref": "DP-PUBLIC"}

    async def continue_navigation_agent_task_v1(self, **kwargs: Any) -> dict[str, str]:
        self.continues.append(dict(kwargs))
        return {"task_ref": "DP-PUBLIC"}

    async def control_navigation_agent_task_v1(self, **kwargs: Any) -> dict[str, str]:
        self.controls.append(dict(kwargs))
        return {"task_ref": "DP-PUBLIC", "status": "paused"}

    @staticmethod
    def safe_router_tool_error(
        error: Exception,
        *,
        action: str,
        web_session_id: str | None = None,
    ) -> dict[str, Any]:
        del web_session_id
        return {"ok": False, "action": action, "message": str(error)}


def _tool(tool_type: type, runtime: _Runtime):
    return tool_type(
        runtime=runtime,
        web_session_id="web-public",
        router_session_id="router-private",
    )


def test_router_v1_start_schema_uses_one_canonical_scope_contract() -> None:
    schema = StartNavigationDataTaskV1Tool.input_schema

    assert set(schema["properties"]) == {
        "scope_source",
        "dataset_date",
        "selection",
        "scene_mode",
    }
    assert schema["required"] == ["scope_source", "dataset_date", "selection"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["scope_source"]["enum"] == [
        "request_context",
        "interpreted_user_text",
    ]
    assert schema["properties"]["dataset_date"]["pattern"] == "^[0-9]{8}$"
    assert schema["properties"]["scene_mode"]["enum"] == ["indoor", "outdoor"]
    selection = schema["properties"]["selection"]
    assert selection["type"] == "object"
    assert "oneOf" not in selection
    assert "discriminator" not in selection
    assert selection["properties"]["kind"]["enum"] == [
        "all_clips",
        "selected_clips",
    ]
    assert selection["properties"]["clips"]["minItems"] == 1
    assert selection["properties"]["clips"]["maxItems"] == 200
    assert selection["required"] == ["kind"]


def test_router_v1_start_schema_accepts_native_selection_objects_not_json_strings() -> None:
    validator = Draft202012Validator(StartNavigationDataTaskV1Tool.input_schema)
    common = {
        "scope_source": "interpreted_user_text",
        "dataset_date": "20270605",
    }

    assert list(
        validator.iter_errors(
            {
                **common,
                "selection": {
                    "kind": "selected_clips",
                    "clips": ["20260605_152856"],
                },
            },
        ),
    ) == []
    assert list(
        validator.iter_errors(
            {**common, "selection": {"kind": "all_clips"}},
        ),
    ) == []
    string_errors = list(
        validator.iter_errors(
            {
                **common,
                "selection": (
                    '{"kind":"selected_clips",'
                    '"clips":["20260605_152856"]}'
                ),
            },
        ),
    )
    assert len(string_errors) == 1
    assert "is not of type 'object'" in string_errors[0].message


def test_router_prompt_preserves_scope_across_one_unresolved_clarification() -> None:
    prompt = main_router_v1_prompt()

    assert "immediately preceding unanswered clarification" in prompt
    assert "preserves a previously" in prompt
    assert "do not silently expand" in prompt
    assert "Default to all clips only" in prompt


def test_router_prompt_treats_cross_date_clip_prefix_as_opaque() -> None:
    prompt = main_router_v1_prompt()

    assert "A clip ID is an opaque child-directory" in prompt
    assert "dataset date `20270605` with clip `20260605_152856` is valid" in prompt
    assert "Never reject, correct, or clarify a scope solely because" in prompt
    assert "Do not derive either value from the other" in prompt
    assert "Do not use naming conventions to pre-validate clip existence" in prompt
    assert "native JSON object" in prompt
    assert "never change `dataset_date`, switch `selected_clips` to `all_clips`" in prompt
    assert "A serialization failure is not" in prompt
    assert "permission to reinterpret the request" in prompt


def test_router_v1_continue_and_control_schemas_do_not_expose_runtime_identity() -> None:
    continue_schema = ContinueNavigationDataTaskV1Tool.input_schema
    control_schema = ControlNavigationDataTaskV1Tool.input_schema

    assert continue_schema == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    assert set(control_schema["properties"]) == {"action"}
    assert control_schema["required"] == ["action"]
    assert control_schema["properties"]["action"]["enum"] == ["stop", "cancel"]
    assert control_schema["additionalProperties"] is False


def test_router_v1_tool_failures_use_one_safe_result_contract() -> None:
    class _FailingRuntime(_Runtime):
        async def continue_navigation_agent_task_v1(self, **kwargs: Any) -> dict[str, str]:
            del kwargs
            raise RuntimeError("task state revision changed")

    result = asyncio.run(_tool(ContinueNavigationDataTaskV1Tool, _FailingRuntime())())

    assert result.metadata == {
        "ok": False,
        "operation": "continue",
        "accepted": False,
        "task_ref": None,
        "status": None,
        "error": {
            "code": "navigation_runtime_error",
            "message": "task state revision changed",
            "retryable": False,
        },
        "latest_task": None,
    }


def test_router_v1_tools_forward_only_the_new_runtime_contract() -> None:
    runtime = _Runtime()
    start = _tool(StartNavigationDataTaskV1Tool, runtime)
    continuation = _tool(ContinueNavigationDataTaskV1Tool, runtime)
    control = _tool(ControlNavigationDataTaskV1Tool, runtime)

    started = asyncio.run(
        start(
            scope_source="request_context",
            dataset_date="20260721",
            selection={"kind": "selected_clips", "clips": ["clip_a", "clip_b"]},
            scene_mode="outdoor",
        )
    )
    continued = asyncio.run(continuation())
    stopped = asyncio.run(control(action="stop"))

    assert runtime.starts == [
        {
            "web_session_id": "web-public",
            "router_session_id": "router-private",
            "scope_source": "request_context",
            "dataset_date": "20260721",
            "selection": {
                "kind": "selected_clips",
                "clips": ["clip_a", "clip_b"],
            },
            "scene_mode": "outdoor",
        }
    ]
    assert runtime.continues == [
        {
            "web_session_id": "web-public",
            "router_session_id": "router-private",
        }
    ]
    assert runtime.controls == [
        {
            "web_session_id": "web-public",
            "router_session_id": "router-private",
            "action": "stop",
        }
    ]
    assert started.metadata == {
        "ok": True,
        "operation": "start",
        "accepted": True,
        "task_ref": "DP-PUBLIC",
        "status": "active",
        "error": None,
        "latest_task": {"task_ref": "DP-PUBLIC", "status": "active"},
    }
    assert continued.metadata["operation"] == "continue"
    assert continued.metadata["accepted"] is True
    assert stopped.metadata["operation"] == "stop"
    assert stopped.metadata["status"] == "paused"


def test_router_v1_prompt_is_unique_and_covers_routing_state_semantics() -> None:
    runtime = _Runtime()
    middleware = RouterContractV1Middleware(
        runtime=runtime,
        web_session_id="web-public",
        router_session_id="router-private",
    )

    assembled = asyncio.run(
        middleware.on_system_prompt(
            SimpleNamespace(),
            "LEGACY ROUTER POLICY: report success after handoff",
        )
    )
    prompt = main_router_v1_prompt()

    assert assembled.startswith(prompt)
    assert "LEGACY ROUTER POLICY" not in assembled
    assert assembled.count("RouterContextEnvelope (volatile; do not quote):") == 1
    for required in (
        "No clip list means all clips",
        "kind: navigation_dataset_selection_v1",
        "Never ask for or accept an internal segment or sequence",
        "Scene mode is optional at task start",
        "Answer progress and status questions directly",
        "waiting_user",
        "pending question takes precedence",
        '"不用继续了"',
        "Call continue_navigation_data_task",
        "V1 does not support live steering",
        "second task",
        "Use `stop`",
        "Use `cancel`",
        "Do not produce `Answer:`",
        "another model call",
    ):
        assert required in prompt
    for removed_field in (
        "`target`",
        "`reason`",
        "`missing_fields`",
        "`confidence`",
        "`response_language`",
        "`task_ref`",
        "`expected_task_revision`",
        "`original_user_message`",
    ):
        assert removed_field not in prompt


def test_navigation_guidance_uses_clips_as_the_only_selectable_granularity() -> None:
    from vla_data_juicer_agents.runtime.agentscope_prompts import (
        NAVIGATION_AGENT_GUIDANCE_PATH,
    )

    guidance = NAVIGATION_AGENT_GUIDANCE_PATH.read_text(encoding="utf-8")

    assert "The task-selection granularity is clips" in guidance
    assert "segment or sequence is generated inside a clip" in guidance
    assert "never a selectable task input" in guidance
    assert "requested clip inventory" in guidance
    assert "selected clip inventory" in guidance
    assert "opaque child-directory name" in guidance
    assert "different date-like prefix" in guidance
    assert "Never rewrite, reject, or redirect" in guidance

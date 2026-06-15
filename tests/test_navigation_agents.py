import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.event import RequireUserConfirmEvent
from agentscope.message import ToolCallBlock

from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.navigation.agents import create_executor_agent, create_plan_agent
from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template, run_plan_agent


def _invoke_tool(tool, arguments):
    async def _call():
        payload = tool(**arguments)
        if inspect.isawaitable(payload):
            payload = await payload
        return _decode_tool_payload(payload)

    return asyncio.run(_call())


def _decode_tool_payload(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return json.loads(payload)
    if hasattr(payload, "content"):
        return _decode_tool_payload(payload.content)
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, (list, tuple)):
        texts = [
            block.text
            for block in payload
            if hasattr(block, "text") and isinstance(block.text, str)
        ]
        if texts:
            return _decode_tool_payload("".join(texts))
    return payload


def test_invoke_tool_helper_uses_agentscope_call_protocol():
    class FakeAgentScopeTool:
        name = "fake_tool"

        def __call__(self, value: str):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"value": value}))])

        def on_invoke_tool(self, *_args, **_kwargs):
            raise AssertionError("OpenAI on_invoke_tool must not be used")

    assert _invoke_tool(FakeAgentScopeTool(), {"value": "ok"}) == {"value": "ok"}


def test_create_plan_agent_has_read_only_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_plan_agent()
    tool_names = {tool.name for tool in agent.tools}

    assert "inspect_raw_date_tool" in tool_names
    assert "classify_navigation_dataset_tool" in tool_names


def test_create_plan_agent_with_request_has_draft_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    request = NavigationRequest(date="20270605", dry_run=True)
    agent = create_plan_agent(request=request)
    tool_names = {tool.name for tool in agent.tools}

    assert "update_workflow_plan_draft_tool" in tool_names
    assert "get_workflow_plan_draft_tool" in tool_names
    assert "finalize_workflow_plan_tool" in tool_names


def test_create_executor_agent_has_execution_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_executor_agent(dry_run=True)
    tool_names = {tool.name for tool in agent.tools}

    assert "prepare_raw_data_tool" in tool_names
    assert "run_initial_annotation_gui_tool" in tool_names


def test_create_qwen_model_uses_agentscope_dashscope(monkeypatch):
    from agentscope.model import DashScopeChatModel
    from vla_data_juicer_agents.navigation.agents import create_qwen_model

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    model = create_qwen_model(model="qwen-plus")

    assert isinstance(model, DashScopeChatModel)
    assert model.model == "qwen-plus"


def test_create_executor_agent_dry_run_binds_execution_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    root = tmp_path / "VLADatasets"
    raw_date = root / "raw_data" / "20270605"
    (raw_date / "20260605_152856").mkdir(parents=True)
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(root))
    agent = create_executor_agent(dry_run=True)
    tool = {tool.name: tool for tool in agent.tools}["prepare_raw_data_tool"]

    result = _invoke_tool(tool, {"date": "20270605"})

    assert result["ok"] is True
    assert result["details"]["dry_run"] is True
    assert not (root / "raw_data" / "20270605_temp").exists()
    assert not (root / "clip_data" / "20270605").exists()


def test_plan_agent_classification_tool_accepts_empty_segments_string(tmp_path, monkeypatch):
    fixture_root = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(fixture_root))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_plan_agent()
    tool = {tool.name: tool for tool in agent.tools}["classify_navigation_dataset_tool"]

    result = _invoke_tool(tool, {"date": "20270605", "segments": ""})

    assert result["profile_name"] == "go2w_like"


def test_plan_agent_classification_tool_accepts_json_segments_string(tmp_path, monkeypatch):
    fixture_root = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"
    monkeypatch.setenv("VLA_VLADATASETS_ROOT", str(fixture_root))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_plan_agent()
    tool = {tool.name: tool for tool in agent.tools}["classify_navigation_dataset_tool"]

    result = _invoke_tool(tool, {"date": "20270605", "segments": '["20260605_152856"]'})

    assert result["profile_name"] == "go2w_like"


def test_plan_agent_draft_tools_finalize_internal_workflow_plan(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    request = NavigationRequest(date="20270605", dry_run=True)
    agent = create_plan_agent(request=request)
    tools = {tool.name: tool for tool in agent.tools}

    update_result = _invoke_tool(tools["update_workflow_plan_draft_tool"], {"profile": "go2w_like"})
    finalize_result = _invoke_tool(tools["finalize_workflow_plan_tool"], {})

    assert update_result["ok"] is True
    plan = finalize_result["workflow_plan_json"]
    assert plan["date"] == "20270605"
    assert plan["dataset_profile"] == "go2w_like"
    assert [step["tool_name"] for step in plan["steps"]] == [
        "prepare_raw_data",
        "extract_and_sync_navigation_data",
        "generate_gridmap_from_pcd",
        "assemble_finish_temp",
        "run_noobscene_preprocessing",
        "run_initial_annotation_gui",
        "run_tracking_and_projection",
        "validate_navigation_outputs",
    ]


def test_executor_agent_has_sdk_tool_for_each_plan_step(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_executor_agent(dry_run=True)
    tool_names = {tool.name for tool in agent.tools}
    plan = build_deterministic_plan_template(date="20270605", dataset_profile="go2w_like", segments=None)

    missing_tools = [f"{step.tool_name}_tool" for step in plan.steps if f"{step.tool_name}_tool" not in tool_names]

    assert missing_tools == []
    assert "prepare_raw_data_tool" in agent.instructions


def test_plan_template_includes_human_gui_step():
    plan = build_deterministic_plan_template(date="20270605", dataset_profile="go2w_like", segments=None)

    gui_steps = [step for step in plan.steps if step.tool_name == "run_initial_annotation_gui"]
    assert len(gui_steps) == 1
    assert gui_steps[0].human_blocking is True


def test_plan_template_uses_finish_data_paths_for_gui_and_validation():
    plan = build_deterministic_plan_template(date="20270605", dataset_profile="go2w_like", segments=None)
    steps = {step.tool_name: step for step in plan.steps}

    assert steps["run_noobscene_preprocessing"].arguments == {"finish_temp_path": "finish_data/20270605_temp"}
    assert steps["run_noobscene_preprocessing"].expected_outputs == ["finish_data/20270605_temp"]
    assert steps["run_initial_annotation_gui"].arguments == {"finish_temp_path": "finish_data/20270605_temp"}
    assert steps["run_initial_annotation_gui"].expected_outputs == ["finish_data/20270605_temp"]
    assert steps["run_tracking_and_projection"].arguments == {
        "finish_temp_path": "finish_data/20270605_temp",
        "finish_path": "finish_data/20270605",
    }
    assert steps["run_tracking_and_projection"].expected_outputs == ["finish_data/20270605"]
    assert steps["validate_navigation_outputs"].arguments == {"date": "20270605"}
    assert steps["validate_navigation_outputs"].expected_outputs == ["finish_data/20270605"]


def test_parse_workflow_plan_output_accepts_json_string():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    payload = build_deterministic_plan_template("20270605", "go2w_like", None).model_dump(mode="json")

    plan = _parse_workflow_plan_output(json.dumps(payload))

    assert plan.date == "20270605"
    assert plan.steps[0].tool_name == "prepare_raw_data"


def test_parse_workflow_plan_output_accepts_dict():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    payload = build_deterministic_plan_template("20270605", "go2w_like", None).model_dump(mode="json")

    plan = _parse_workflow_plan_output(payload)

    assert plan.dataset_profile == "go2w_like"


def test_parse_workflow_plan_output_accepts_fenced_json():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    payload = build_deterministic_plan_template("20270605", "go2w_like", None).model_dump(mode="json")
    fenced = f"```json\n{json.dumps(payload)}\n```"

    plan = _parse_workflow_plan_output(fenced)

    assert plan.date == "20270605"


def test_parse_workflow_plan_output_rejects_invalid_output():
    from vla_data_juicer_agents.navigation.workflow import _parse_workflow_plan_output

    with pytest.raises(ValueError, match="Unable to parse WorkflowPlan output"):
        _parse_workflow_plan_output("not json at all")


def test_create_qwen_model_requires_dashscope_key(monkeypatch):
    from vla_data_juicer_agents.navigation.agents import create_qwen_model

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY is required"):
        create_qwen_model()


def test_run_plan_agent_streams_events_to_run_state(tmp_path):
    request = NavigationRequest(date="20270605", dry_run=True)
    plan = build_deterministic_plan_template("20270605", "go2w_like", None)
    plan_json = plan.model_dump_json()
    run_store = WorkflowRunStore(tmp_path / "runs")
    run_dir = run_store.create_run(request.date)

    class FakeStreamAgent:
        async def reply_stream(self, _msg):
            yield SimpleNamespace(type="MODEL_CALL_START", model="qwen-plus")
            yield SimpleNamespace(type="TOOL_CALL_START", name="inspect_raw_date_tool")
            yield SimpleNamespace(type="TOOL_RESULT_END", tool_call_id="call_1")
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=plan_json)
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_1")

    parsed_plan = asyncio.run(
        run_plan_agent(FakeStreamAgent(), request, run_store=run_store, run_dir=run_dir)
    )

    events_path = run_dir / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert parsed_plan == plan
    assert [event["event_type"] for event in events] == [
        "MODEL_CALL_START",
        "TOOL_CALL_START",
        "TOOL_RESULT_END",
        "TEXT_BLOCK_DELTA",
        "REPLY_END",
    ]
    assert events[1]["payload"]["name"] == "inspect_raw_date_tool"


def test_run_plan_agent_auto_confirms_tool_calls(tmp_path):
    request = NavigationRequest(date="20270605", dry_run=True)
    plan = build_deterministic_plan_template("20270605", "go2w_like", None)
    run_store = WorkflowRunStore(tmp_path / "runs")
    run_dir = run_store.create_run(request.date)

    class FakeConfirmingAgent:
        def __init__(self):
            self.inputs = []

        async def reply_stream(self, msg):
            self.inputs.append(msg)
            if len(self.inputs) == 1:
                yield RequireUserConfirmEvent(
                    reply_id="reply_1",
                    tool_calls=[
                        ToolCallBlock(
                            id="call_1",
                            name="inspect_raw_date_tool",
                            input='{"date": "20270605"}',
                        )
                    ],
                )
                return
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta=plan.model_dump_json())
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_1")

    agent = FakeConfirmingAgent()

    parsed_plan = asyncio.run(run_plan_agent(agent, request, run_store=run_store, run_dir=run_dir))

    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert parsed_plan == plan
    assert len(agent.inputs) == 2
    assert agent.inputs[1].confirm_results[0].confirmed is True
    assert "REQUIRE_USER_CONFIRM" in [event["event_type"] for event in events]

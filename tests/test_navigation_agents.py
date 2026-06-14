import asyncio
import json
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.navigation.agents import create_executor_agent, create_plan_agent
from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template


def _invoke_tool(tool, arguments):
    ctx = SimpleNamespace(tool_name=tool.name, run_config=None, context=None)
    payload = asyncio.run(tool.on_invoke_tool(ctx, json.dumps(arguments)))
    return json.loads(payload) if isinstance(payload, str) else payload


def test_create_plan_agent_has_read_only_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_plan_agent()
    tool_names = {tool.name for tool in agent.tools}

    assert "inspect_raw_date_tool" in tool_names
    assert "classify_navigation_dataset_tool" in tool_names


def test_create_executor_agent_has_execution_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_executor_agent(dry_run=True)
    tool_names = {tool.name for tool in agent.tools}

    assert "prepare_raw_data_tool" in tool_names
    assert "run_initial_annotation_gui_tool" in tool_names


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

from vla_data_juicer_agents.navigation.agents import create_executor_agent, create_plan_agent
from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template


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


def test_plan_template_includes_human_gui_step():
    plan = build_deterministic_plan_template(date="20270605", dataset_profile="go2w_like", segments=None)

    gui_steps = [step for step in plan.steps if step.tool_name == "run_initial_annotation_gui"]
    assert len(gui_steps) == 1
    assert gui_steps[0].human_blocking is True

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from vla_data_juicer_agents.core.tool import ToolContext, get_tool_spec, list_tool_specs
from vla_data_juicer_agents.capabilities.session.orchestrator import VLASessionAgent
from vla_data_juicer_agents.capabilities.session.runtime import SessionState, SessionToolRuntime
from vla_data_juicer_agents.capabilities.session.toolkit import get_session_tool_specs
from vla_data_juicer_agents.tools.vla.run_workflow import _normalize_model, _normalize_segments, run_vla_workflow


def test_tool_registry_exposes_vla_workflow_tool():
    names = [spec.name for spec in list_tool_specs()]

    assert "vla_run_workflow" in names
    assert get_tool_spec("vla_run_workflow").effects == "execute"


def test_session_toolkit_prioritizes_vla_workflow_tool():
    specs = get_session_tool_specs()
    names = [spec.name for spec in specs]

    assert names[0] == "vla_run_workflow"


def test_session_toolkit_exposes_vla_workflow_input_schema():
    agent = VLASessionAgent(use_llm_router=False)
    toolkit = agent._build_toolkit()

    schemas = asyncio.run(toolkit.get_tool_schemas())
    workflow_schema = next(schema for schema in schemas if schema["function"]["name"] == "vla_run_workflow")
    properties = workflow_schema["function"]["parameters"]["properties"]

    assert "date" in properties
    assert "segments" in properties
    assert "dry_run" in properties
    assert "approve" in properties


def test_session_prompt_routes_complex_vla_requests_to_workflow():
    agent = VLASessionAgent(use_llm_router=False)
    prompt = agent.session_system_prompt()

    assert "call vla_run_workflow exactly once" in prompt
    assert "Do not use deterministic Python keyword routing" in prompt
    assert "Respond in the same language as the user" in prompt


def test_session_prompt_allows_dry_run_only_when_user_says_dry_run():
    agent = VLASessionAgent(use_llm_router=False)
    prompt = agent.session_system_prompt()

    assert "Set dry_run=true only when the user explicitly says dry_run" in prompt
    assert "Default to dry_run=false and perform real data processing" in prompt
    assert "Do not infer dry_run=true" in prompt
    assert "inspection without mutation" not in prompt


def test_session_prompt_keeps_dry_run_requests_approved_for_execution():
    agent = VLASessionAgent(use_llm_router=False)
    prompt = agent.session_system_prompt()

    assert "dry_run is still an execution mode" in prompt
    assert "do not set approve=false merely because dry_run=true" in prompt


def test_session_agent_builds_real_agentscope_agent(monkeypatch):
    seen = {}

    class FakeModel:
        pass

    class FakeAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr("vla_data_juicer_agents.capabilities.session.orchestrator.create_qwen_model", lambda model=None: FakeModel())
    monkeypatch.setattr("agentscope.agent.Agent", FakeAgent)

    agent = VLASessionAgent(use_llm_router=False)
    built = agent.build_react_agent()

    assert isinstance(built, FakeAgent)
    assert seen["name"] == "VLASessionAgent"
    assert "vla_run_workflow" in seen["system_prompt"]
    assert seen["model"].__class__ is FakeModel
    assert asyncio.run(seen["toolkit"].get_tool("vla_run_workflow")) is not None


def test_session_handle_message_uses_llm_agent_not_keyword_router(monkeypatch):
    session = VLASessionAgent(use_llm_router=False)

    class FakeStreamingAgent:
        async def reply_stream(self, msg):
            assert "请处理 20270605 的导航 VLA 数据" in str(msg.content)
            yield SimpleNamespace(type="TEXT_BLOCK_DELTA", delta="已调用 VLA workflow。")
            yield SimpleNamespace(type="REPLY_END", reply_id="reply_1")

    session._react_agent = FakeStreamingAgent()

    reply = asyncio.run(session.handle_message_async("请处理 20270605 的导航 VLA 数据"))

    assert reply.text == "已调用 VLA workflow。"
    assert session.state.history == [
        {"role": "user", "content": "请处理 20270605 的导航 VLA 数据"},
        {"role": "assistant", "content": "已调用 VLA workflow。"},
    ]


def test_vla_run_workflow_tool_reuses_plan_and_executor_agents(tmp_path, monkeypatch):
    calls = []

    plan = SimpleNamespace(
        model_dump=lambda mode="json": {"date": "20270605", "dataset_profile": "go2w_like", "steps": []},
        model_dump_json=lambda: json.dumps({"date": "20270605", "dataset_profile": "go2w_like", "steps": []}),
    )

    async def fake_run_plan_agent(agent, request, run_store=None, run_dir=None):
        calls.append(("plan", agent, request.date, request.segments, request.dry_run, bool(run_store), bool(run_dir)))
        return plan

    async def fake_run_executor_agent(agent, workflow_plan, run_store=None, run_dir=None):
        calls.append(("execute", agent, workflow_plan, bool(run_store), bool(run_dir)))
        return "execution summary"

    monkeypatch.setenv("VLA_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.create_plan_agent", lambda model=None, request=None: "plan-agent")
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.create_executor_agent", lambda model=None, dry_run=False: f"executor-dry-{dry_run}")
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_plan_agent", fake_run_plan_agent)
    monkeypatch.setattr("vla_data_juicer_agents.tools.vla.run_workflow.run_executor_agent", fake_run_executor_agent)

    ctx = ToolContext(working_dir=str(tmp_path), artifacts_dir=str(tmp_path / ".djx"))
    payload = asyncio.run(
        run_vla_workflow(
            ctx,
            {
                "date": "20270605",
                "segments": ["20260605_152856"],
                "dry_run": True,
                "approve": True,
            },
        )
    )

    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["final_output"] == "execution summary"
    assert Path(payload["run_dir"]).is_dir()
    assert calls[0] == ("plan", "plan-agent", "20270605", ["20260605_152856"], True, True, True)
    assert calls[1] == ("execute", "executor-dry-True", plan, True, True)


def test_vla_run_workflow_normalizes_llm_string_arguments():
    assert _normalize_segments('["20260605_152856"]') == ["20260605_152856"]
    assert _normalize_segments("20260605_152856,20260605_153000") == [
        "20260605_152856",
        "20260605_153000",
    ]
    assert _normalize_model(None) is None
    assert _normalize_model("") is None
    assert _normalize_model("None") is None
    assert _normalize_model("null") is None
    assert _normalize_model("qwen-plus") == "qwen-plus"

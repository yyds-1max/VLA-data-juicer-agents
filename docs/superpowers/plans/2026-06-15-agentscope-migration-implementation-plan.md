# AgentScope Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the OpenAI Agents SDK integration with AgentScope while preserving the existing VLA navigation Plan-Agent, Executor-Agent, structured plan, tool layer, runtime isolation, and dry-run behavior.

**Architecture:** Keep the current business design intact: Plan-Agent produces `WorkflowPlan`, Executor-Agent consumes `WorkflowPlan`, and all ROS/CUDA/GUI/data scripts remain behind subprocess runtime wrappers. Introduce a thin AgentScope adapter around DashScope model creation, toolkit construction, function-tool wrapping, and `agent.reply_stream(...)` event persistence into run state.

**Tech Stack:** Python 3.12 agent runtime, AgentScope 2.x, DashScope Qwen via `DashScopeChatModel`, Pydantic, PyYAML, pytest, subprocess-isolated Python 3.8 legacy runtime.

---

## Phase Coverage

This implementation covers all three requested phases in one migration:

1. **Phase 1: Local adapter migration and unit tests**
   Replace OpenAI Agents SDK imports, tools, and runner calls with AgentScope equivalents. Preserve `--no-llm` deterministic planning for local debugging.
2. **Phase 2: Plan-Agent dry-run through AgentScope**
   Run `vla-nav-agent plan --date 20270605 --dry-run` with DashScope credentials when available, using `agent.reply_stream(...)` and persisting stream events.
3. **Phase 3: Executor-Agent dry-run through AgentScope**
   Run `vla-nav-agent run --date 20270605 --segments 20260605_152856 --dry-run` with DashScope credentials when available, proving AgentScope tool calls construct dry-run command records without mutating real data.

## File Structure

- Modify: `pyproject.toml`
  Replace `openai` and `openai-agents` dependencies with `agentscope`.
- Modify: `README.md`
  Update setup and execution notes from OpenAI Agents SDK to AgentScope native DashScope.
- Modify: `docs/navigation-runtime-isolation.md`
  Update agent runtime wording and add AgentScope event-log expectations.
- Modify: `src/vla_data_juicer_agents/navigation/agents.py`
  Create DashScope model, AgentScope agents, and toolkits.
- Modify: `src/vla_data_juicer_agents/navigation/inspection.py`
  Replace OpenAI `@function_tool` wrappers with AgentScope tool wrappers while keeping pure inspection functions.
- Modify: `src/vla_data_juicer_agents/navigation/execution_tools.py`
  Replace OpenAI `@function_tool` wrappers with AgentScope execution tool wrappers while keeping pure execution functions.
- Modify: `src/vla_data_juicer_agents/navigation/workflow.py`
  Replace `Runner.run(...)` with `agent.reply_stream(...)`, collect final text, and persist stream events when a run directory is supplied.
- Modify: `src/vla_data_juicer_agents/cli.py`
  Pass `run_dir` or run-state writer into AgentScope workflow calls so stream events are written.
- Modify: `src/vla_data_juicer_agents/navigation/run_state.py`
  Add JSONL append support for AgentScope events.
- Modify: `tests/test_navigation_agents.py`
  Update agent/tool assertions for AgentScope.
- Modify: `tests/test_navigation_cli.py`
  Add event-log assertions for deterministic and AgentScope paths where feasible.
- Modify: `tests/test_navigation_execution_tools_dry_run.py`
  Update direct tool invocation helper for AgentScope tools.

## Task 1: AgentScope Tool Wrapping Tests

**Files:**
- Modify: `tests/test_navigation_agents.py`
- Modify: `tests/test_navigation_execution_tools_dry_run.py`

- [ ] **Step 1: Write failing tests for AgentScope tool shape**

Add tests that assert `create_plan_agent()` exposes tools through an AgentScope `toolkit`, and that execution tools can be invoked through a local helper without using `on_invoke_tool`.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_agents.py tests/test_navigation_execution_tools_dry_run.py -q
```

Expected: fail because production code still imports OpenAI Agents SDK and tools expose OpenAI-specific APIs.

- [ ] **Step 3: Implement AgentScope tool wrappers**

In `inspection.py` and `execution_tools.py`, remove `from agents import function_tool`. Use AgentScope `FunctionTool` wrappers with explicit names:

- `inspect_raw_date_tool`
- `classify_navigation_dataset_tool`
- `prepare_raw_data_tool`
- `extract_and_sync_navigation_data_tool`
- `generate_gridmap_from_pcd_tool`
- `assemble_finish_temp_tool`
- `run_noobscene_preprocessing_tool`
- `run_initial_annotation_gui_tool`
- `run_tracking_and_projection_tool`
- `validate_navigation_outputs_tool`

Keep the existing pure functions unchanged.

- [ ] **Step 4: Run tool tests to verify pass**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_agents.py tests/test_navigation_execution_tools_dry_run.py -q
```

Expected: pass for tool construction and dry-run invocation.

## Task 2: AgentScope Agent Factory

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/vla_data_juicer_agents/navigation/agents.py`
- Modify: `tests/test_navigation_agents.py`

- [ ] **Step 1: Write failing tests for DashScope model creation**

Update `test_create_qwen_model_requires_dashscope_key` and add a test that `create_qwen_model(model="qwen-plus")` returns an AgentScope DashScope chat model configured with the requested model.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_agents.py -q
```

Expected: fail because `create_qwen_model` still returns `OpenAIChatCompletionsModel`.

- [ ] **Step 3: Implement AgentScope model and agents**

Replace OpenAI imports with:

```python
from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential
from agentscope.model import DashScopeChatModel
from agentscope.tool import Toolkit
```

Build `DashScopeChatModel` from `DASHSCOPE_API_KEY` and `VLA_AGENT_MODEL`. Build agents with `system_prompt=...` and `toolkit=Toolkit(tools=[...])`.

- [ ] **Step 4: Run agent tests**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_agents.py -q
```

Expected: pass.

## Task 3: reply_stream Event Persistence

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/run_state.py`
- Modify: `src/vla_data_juicer_agents/navigation/workflow.py`
- Modify: `src/vla_data_juicer_agents/cli.py`
- Modify: `tests/test_navigation_cli.py`
- Modify: `tests/test_navigation_agents.py`

- [ ] **Step 1: Write failing tests for JSONL event persistence**

Add a fake AgentScope-like agent whose `reply_stream(...)` yields model-call, tool-call, tool-result, and final reply events. Assert `run_plan_agent(..., run_store=..., run_dir=...)` writes `events.jsonl` and parses the final plan.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_agents.py tests/test_navigation_cli.py -q
```

Expected: fail because workflow still uses `Runner.run(...)`.

- [ ] **Step 3: Implement stream collector**

In `workflow.py`, call `agent.reply_stream(UserMsg(...))`. For each event:

- serialize event type/name
- serialize model call/tool call/tool result fields when present
- append to `events.jsonl` via `WorkflowRunStore`
- collect final text from reply/final message events

Keep `_parse_workflow_plan_output(...)` unchanged.

- [ ] **Step 4: Pass run state from CLI**

In `cli.py`, pass `run_store` and `run_dir` into `run_plan_agent(...)` and `run_executor_agent(...)`.

- [ ] **Step 5: Run tests**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_agents.py tests/test_navigation_cli.py -q
```

Expected: pass.

## Task 4: Documentation And Dependency Migration

**Files:**
- Modify: `README.md`
- Modify: `docs/navigation-runtime-isolation.md`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update dependency docs**

Replace OpenAI Agents SDK setup text with AgentScope setup text:

```bash
pip install -e ".[dev]"
export DASHSCOPE_API_KEY="sk-..."
export VLA_AGENT_MODEL="qwen3.5-plus"
```

Remove `DASHSCOPE_BASE_URL` from required setup unless a compatibility fallback is kept.

- [ ] **Step 2: Update runtime isolation docs**

State that the agent runtime is Python 3.12 with AgentScope, Pydantic, pytest, and DashScope native model support. State that AgentScope event streams are written to `events.jsonl`.

- [ ] **Step 3: Run docs-adjacent tests**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_runtime.py tests/test_navigation_workflow_models.py -q
```

Expected: pass.

## Task 5: Three-Phase Verification

**Files:**
- No production file changes unless verification reveals a defect.

- [ ] **Step 1: Run unit and dry-run regression suite**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_*.py -q
```

Expected: pass.

- [ ] **Step 2: Run Phase 1 deterministic dry-run**

Run:

```bash
vla-nav-agent plan --date 20270605 --dry-run --no-llm
```

Expected: prints a valid `WorkflowPlan` and writes run state.

- [ ] **Step 3: Run Phase 2 AgentScope Plan-Agent dry-run when credentials exist**

Run:

```bash
vla-nav-agent plan --date 20270605 --dry-run
```

Expected: calls Qwen through AgentScope, prints a valid `WorkflowPlan`, and writes AgentScope stream events to `events.jsonl`.

If `DASHSCOPE_API_KEY` is unavailable, record the command as not run and keep the unit/fake-stream tests as local verification.

- [ ] **Step 4: Run Phase 3 AgentScope Executor-Agent dry-run when credentials exist**

Run:

```bash
vla-nav-agent run --date 20270605 --segments 20260605_152856 --dry-run
```

Expected: Executor-Agent calls dry-run tools, reports concise summary, writes tool-call/tool-result events, and does not mutate raw/clip/finish data.

If `DASHSCOPE_API_KEY` is unavailable, record the command as not run and keep the unit/fake-stream tests as local verification.

- [ ] **Step 5: Final review**

Review the diff for accidental business-logic changes. Confirm the structured plan, stage-one scope, GUI blocking step, and runtime isolation remain unchanged.

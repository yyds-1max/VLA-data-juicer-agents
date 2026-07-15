# VLA Data Juicer Agents

This project builds an AgentScope workflow for the first-stage navigation data pipeline:

1. prepare raw ROS bag segment links
2. extract and synchronize navigation data
3. generate gridmap from PCD when needed
4. assemble `finish_data/<date>_temp`
5. run `run_odom.sh` stages through initial annotation, tracking, projection, and final move

Stage one intentionally excludes `run_fix.sh`; it is out of scope and is not run by this agent. The first-stage scope covers only `prepare.sh`, `run_U.sh`, and `run_odom.sh`. `gen_box.py` is the only human GUI step.

## WSL setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export DASHSCOPE_API_KEY="sk-..."
export VLA_AGENT_MODEL="qwen3.5-plus"
```

The Agent runtime stays on Python 3.12 with AgentScope and native DashScope/Qwen model support. Legacy ROS2/CUDA/GUI/tracking scripts run in a separate subprocess runtime:

```bash
export AGENT_DATA_PYTHON="/usr/bin/python3.8"
export AGENT_DATA_ENV_SETUP="/path/to/setup_data_runtime.sh"
export VLA_GT_DOG_ROOT="/media/heying/hy_data2/GT_dog"
```

Do not import ROS, CUDA, OpenCV, Open3D, PCL, GUI, or legacy project modules in the Agent runtime. Keep those dependencies behind the subprocess wrapper so Python 3.12 agent planning does not inherit Python 3.8/ROS2/CUDA library state.

## Dry run

```bash
vla-nav-agent plan --date 20270605 --dry-run --no-llm
```

This plan dry-run prints the deterministic plan, selected profile, and stage-one step selection. It does not execute tools and does not create wrapper command records.

For wrapper command inspection, run:

```bash
pytest tests/test_navigation_runtime.py tests/test_navigation_execution_tools_dry_run.py -q
```

The execution-tool dry-run tests assert wrapped legacy command shape, including `bash -lc`, `source`, and `exec "$AGENT_DATA_PYTHON"` when `AGENT_DATA_ENV_SETUP` is configured.

## Execute

```bash
vla-nav-agent run --date 20270605
```

To exercise dry-run execution through the Executor-Agent, use:

```bash
vla-nav-agent run --date 20270605 --segments 20260605_152856 --dry-run
```

This constructs and executes dry-run tools through the Executor-Agent and requires the normal LLM settings.

AgentScope `reply_stream(...)` events for LLM calls, tool calls, tool results, and final replies are written under each run directory as `events.jsonl`.

## Conversational main Agent

Use `vla-data-agent` when you want a user-facing main Agent that accepts natural language, reasons with a real LLM, and dispatches registered tools. The session Agent exposes `vla_run_workflow` through the shared tool registry and uses it for complex navigation VLA requests.

```bash
vla-data-agent --message "处理 20270605 的导航 VLA 数据，先 dry-run"
```

Interactive mode is also available:

```bash
vla-data-agent
```

Controls:
- Ctrl+D: exit the session at the input prompt
- exit / quit / q / 退出: end the session normally
- Ctrl+C: interrupt the current turn and keep the session open

The transcript shows grouped Main, Workflow, Plan, and Executor progress summaries and tool events.
`vla-nav-agent plan/run` remains the command-oriented navigation diagnostic entry point.

The conversational Agent requires normal LLM settings such as `DASHSCOPE_API_KEY`; it does not provide a deterministic `--no-llm` router path.

## DataPilot web UI

For server use, run the bundled web script from the repository root. It builds the frontend, starts
the backend with `frontend/dist` mounted, and records a PID/log under `.djx`:

```bash
./scripts/run_web.sh start
```

The default server URL is:

```text
http://<server-ip>:8765
```

Useful service commands:

```bash
./scripts/run_web.sh status
./scripts/run_web.sh logs
./scripts/run_web.sh stop
./scripts/run_web.sh restart
```

`logs` follows the log output; press Ctrl+C to leave log viewing. Use `stop` to shut down
the background service. For one-off foreground debugging, run:

```bash
./scripts/run_web.sh foreground
```

Then press Ctrl+C in that terminal to stop the service.

The script defaults to the company dataset root:

```text
/media/heying/hy_data1/VLADatasets
```

Override settings with environment variables when needed:

```bash
HOST=0.0.0.0 PORT=8765 VLA_VLADATASETS_ROOT=/media/heying/hy_data1/VLADatasets ./scripts/run_web.sh start
```

For local frontend development, run the backend API from the repository root:

```bash
vla-data-agent-web --host 127.0.0.1 --port 8765 --working-dir ./.djx
```

Then run the frontend dev server from `frontend`. Vite proxies `/api` and WebSocket traffic to the backend:

```bash
npm run dev
```

For an integrated demo/server, build the frontend first and let the backend serve it:

```bash
cd frontend
npm run build
cd ..
vla-data-agent-web --host 127.0.0.1 --port 8765 --working-dir ./.djx --frontend-dist frontend/dist
```

Frontend verification commands:

```bash
cd frontend
npm test
npm run build
npm run e2e
```

## Runtime isolation

See `docs/navigation-runtime-isolation.md` for the Agent/legacy runtime split, required environment variables, server preflight checks, wrapper behavior, dry-run verification, and operational boundaries.

## Server runbook

See `docs/navigation-server-runbook.md` for server setup, dry-run, and full execution commands.

# VLA Data Juicer Agents

This project builds an AgentScope workflow for the first-stage navigation data pipeline:

1. prepare raw ROS bag segment links
2. extract and synchronize navigation data
3. generate gridmap from PCD when needed
4. assemble `finish_data/<date>_temp`
5. run `run_odom.sh` stages through initial annotation, tracking, projection, and final move

Stage one intentionally excludes `run_fix.sh`; it is out of scope and is not run by this agent. The first-stage scope covers only `prepare.sh`, `run_U.sh`, and `run_odom.sh`. `gen_box.py` is the only human GUI step.

This describes the current Navigation Agent implementation. The automatic
annotation module starts from synchronized artifacts, replaces the legacy GUI,
and connects three-dimensional review/Fix and data-asset lifecycle management.
Its implemented boundaries and operational constraints are recorded in
[`docs/automatic-annotation-development-summary.md`](docs/automatic-annotation-development-summary.md).

## WSL setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export DASHSCOPE_API_KEY="sk-..."
export VLA_AGENT_MODEL="qwen3.5-plus"
# Optional; long AgentScope tools move to background after 10 seconds by default.
export VLA_AGENT_TOOL_BACKGROUND_THRESHOLD_SECS="10"
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
nvm install
nvm use
npm install --global npm@11.16.0
cd frontend
npm ci
cd ..
./scripts/run_web.sh start
```

Frontend builds are pinned to Node.js `24.18.0` and npm `11.16.0`. The
repository `.nvmrc` selects the required Node.js version. In a non-interactive
server environment, set `VLA_FRONTEND_NODE_BIN_DIR` to that installation's
absolute `bin` directory; `run_web.sh` prepends it only for the frontend build
and rejects a mismatched toolchain before invoking npm. Set
`SKIP_FRONTEND_BUILD=1` only to reuse an already verified `frontend/dist`; that
path does not require Node.js or npm.

Run `npm ci` on the first deployment and whenever `frontend/package-lock.json`
changes; `run_web.sh` builds the installed dependency tree but does not install
packages. The shadcn MCP and shadcn CLI are local development aids and are not
installed or executed on the server.

For a persistent server deployment, `run_web.sh` automatically reads the one
fixed configuration file:

```text
~/.config/vla-data-juicer-agents/run-web.json
```

The application directory must be a real directory owned by the service user
with mode `0700`; `run-web.json` must be a real, single-link regular file owned
by that user with mode `0600`. The file is strict JSON, not a shell fragment:

```json
{
  "WORKING_DIR": "/srv/datapilot/state",
  "VLA_DATA_AGENT_WEB_WORKING_DIR": "/srv/datapilot/state",
  "VLA_FRONTEND_NODE_BIN_DIR": "/home/service/.nvm/versions/node/v24.18.0/bin",
  "VLA_ANNOTATION_WORK_ROOT": "/srv/datapilot/annotation-work",
  "VLA_NAVIGATION_ODOM_V1_SOURCE": "/srv/datapilot/runtime/navigation_odom_v1/source",
  "VLA_NAVIGATION_ODOM_V1_MANIFEST": "/srv/datapilot/app/runtime/navigation_odom_v1/manifest.json",
  "VLA_NAVIGATION_WRITER_LOCK_PATH": "/srv/datapilot/locks/navigation-writer.lock",
  "VLA_VLADATASETS_ROOT": "/srv/vla-datasets"
}
```

Only the documented Web paths, the fixed frontend Node directory, and the
non-secret Annotation Runtime variables are accepted. Unknown keys, duplicate
JSON keys, symlinks, hardlinks, unsafe permissions, and oversized files make
startup fail closed. The script never `source`s or `eval`s this file and does
not accept an alternate configuration path. Explicit variables already
present in the calling environment take precedence over matching JSON keys.
`DASHSCOPE_API_KEY` is intentionally not accepted in this file; it must be
inherited from the calling shell or injected by the deployment's credential
manager.

Treat `STATE_DIR`, `PID_FILE`, `LOG_DIR`, `LOG_FILE`, and `WORKING_DIR` as
immutable while the service is running. Stop the service with the existing
configuration before changing those values, otherwise the new configuration
cannot safely identify the old process. An invalid or unsafe configuration
intentionally blocks every action, including `status`, `logs`, and `stop`;
repair the JSON or its ownership/permissions first, then rerun the control
command.

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

`WORKING_DIR` is the script's authoritative `--working-dir` value. If it is
unset, the script adopts `VLA_DATA_AGENT_WEB_WORKING_DIR`; if both variables are
set, their values must match exactly or `start`, `foreground`, and `restart`
fail before the frontend build. `stop`, `status`, and `logs` remain available
under a stale conflicting environment so operators can inspect or stop an
existing service. When neither is set, `WORKING_DIR` defaults to `STATE_DIR`.
The PID and log paths remain controlled separately by `STATE_DIR`, `PID_FILE`,
and `LOG_FILE`.

The script creates new `WORKING_DIR`, `STATE_DIR`, and log directories under
`umask 077`. It does not change permissions on directories that already exist;
server operators must make sure an existing Web working directory already meets
the backend's ownership and permission checks.

Background `start`, `stop`, `restart`, and `status` operations are serialized by
`scripts/run_web_control.py`. The helper requires the PID-file parent to be a
real, current-user-owned directory that is not group/other writable, and rejects
symlinked, non-regular, multiply linked, foreign-owned, or group/other-writable
PID/control/instance files. A PID record is valid only when it contains one
canonical decimal PID greater than 1 and matches both the live instance lock
held by the Web process and its recorded OS process-birth identity (Linux boot
ID plus `/proc/<pid>/stat` start time, or the macOS kernel process start time).
Stale or reused PIDs are never signalled; Linux signalling additionally uses a
pidfd. A stale instance record that is still locked blocks a new start and is
preserved for operator review. The helper uses an exclusive lock on the stable
`/usr` directory inode before the per-PID control lock, so concurrent lifecycle
commands remain serialized even if a state directory is renamed and recreated.
Its internal lifecycle action also verifies the inherited anchor, PID-parent,
and control-lock file descriptors rather than trusting an environment marker.
Those control descriptors are closed before the Web process is launched. The
server must therefore support POSIX `flock` on `/usr`; verify this during
deployment preflight.

For local frontend development, run the backend API from the repository root:

```bash
vla-data-agent-web --host 127.0.0.1 --port 8765 --working-dir ./.djx
```

Then run the frontend dev server from `frontend`. Vite proxies `/api` and WebSocket traffic to the backend:

```bash
npm run dev
```

### User-facing agent activity events

The Web conversation exposes a product-safe processing narrative rather than
raw model reasoning. AgentScope's event log remains the internal diagnostic
source; the Web adapter emits:

- `progress_start`, `progress_delta`, and `progress_end` to stream one safe
  user-facing progress paragraph in place;
- `progress_update` only for deterministic fallback and lifecycle messages;
- `tool_start` with the exact tool name and opaque call ID, but no arguments;
- `tool_end` with the exact tool name, call ID, and status, but no raw result;
- `final` for the separate assistant answer after processing.

Before each meaningful tool group, agents produce one single-line public update:

```text
Activity: 已确认的用户可感知事实，以及接下来要做的事。
```

The adapter removes this line from assistant text, validates the complete line,
then emits only bounded, sanitized natural-language fragments for progressive
rendering. Raw model tokens are never sent to the browser. The former JSON
Activity format remains
readable for rolling upgrades. When the model omits or violates the protocol,
the adapter emits at most one deterministic business-level fallback per phase;
it does not expose hidden thinking, tool arguments/results, internal paths,
prompts, credentials, or agent names. The frontend appends streamed fragments
to the same paragraph, keeps each tool call as its own timed row, and collapses
the processing disclosure after completion while keeping the final answer
separate.

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

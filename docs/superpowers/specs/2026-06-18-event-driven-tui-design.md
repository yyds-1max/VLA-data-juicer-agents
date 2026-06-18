# Event-Driven Multi-Agent TUI Design

## Goal

Build a conversational terminal UI for `vla-data-agent` that:

- supports multiple turns in one persistent session;
- renders spinners, concise Agent progress summaries, tool calls, tool results, and final replies;
- shows events from the main Agent, the navigation workflow, Plan-Agent, and Executor-Agent in one grouped timeline;
- prevents Agents, workflows, and tools from printing directly to the terminal;
- provides a transport-neutral event interface that a future Web UI can consume;
- lets users interrupt one running turn without ending the session.

The interaction model follows the useful parts of `datajuicer/data-juicer-agents`: a controller owns the Agent, executes each turn in a worker thread, receives events through a callback-backed queue, and lets the TUI main thread drain and render those events.

## Scope

This change covers the `vla-data-agent` conversational entry point and the navigation workflow it invokes. The existing `vla-nav-agent` diagnostic CLI remains available and does not need to adopt the interactive TUI in this iteration.

The initial renderer is a transcript-style terminal interface rather than a full-screen application. It uses transient spinner lines and persistent timeline entries. Interactive tree expansion, mouse input, session persistence across process restarts, and a Web server are outside this iteration.

## Architecture

### Event Core

Agent and workflow code depend on a small `EventEmitter` interface rather than a terminal, queue, or network transport. An emitter accepts structured, JSON-serializable events and forwards them to one or more sinks.

Initial sink implementations are:

- `QueueEventSink`: sends events to the TUI controller's thread-safe queue;
- `JsonlEventSink`: writes navigation run events to `events.jsonl`;
- `CompositeEventSink`: broadcasts each event to multiple sinks.

The same interface can later gain `SSEEventSink` or `WebSocketEventSink` implementations without changing Agent, workflow, or tool code. A distributed broker such as Redis Streams or NATS is deferred until the application needs cross-process subscriptions, replay, or multiple service instances.

### Event Schema

Every event uses this common envelope:

```json
{
  "type": "reasoning",
  "source": "navigation.plan",
  "run_id": "agent_123",
  "parent_run_id": "workflow_456",
  "timestamp": "2026-06-18T10:00:00.000Z",
  "payload": {}
}
```

Required envelope fields:

- `type`: normalized event kind;
- `source`: stable producer label;
- `run_id`: identifier for the current Agent or workflow execution;
- `parent_run_id`: identifier of the caller, or `null` for the top-level turn;
- `timestamp`: UTC ISO-8601 timestamp;
- `payload`: event-specific JSON data.

Initial normalized event types are:

- `agent_start`: Agent or workflow execution started;
- `reasoning`: concise natural-language progress summary;
- `tool_start`: tool invocation started;
- `tool_end`: tool invocation completed, failed, or was interrupted;
- `agent_end`: Agent or workflow execution completed, failed, or was interrupted;
- `final`: user-facing final response for the turn.

Stable sources are:

- `main` for `VLASessionAgent`;
- `navigation.workflow` for `vla_run_workflow` orchestration;
- `navigation.plan` for Plan-Agent;
- `navigation.executor` for Executor-Agent.

Tool events additionally carry a `call_id` so `tool_start` and `tool_end` can be paired. Lifecycle end events carry `status`, whose initial values are `completed`, `failed`, and `interrupted`.

### AgentScope Adapter

`_run_agent_stream()` is the central AgentScope-to-domain-event adapter. It continues to aggregate final text and preserve confirmation behavior, while translating AgentScope events:

- `REPLY_START` and `REPLY_END` become Agent lifecycle events;
- `THINKING_BLOCK_*` is accumulated and emitted as a concise `reasoning` event at block completion;
- `TOOL_CALL_START` supplies the tool name and call identity;
- `TOOL_RESULT_START`, result deltas, and `TOOL_RESULT_END` produce paired tool lifecycle events;
- text deltas continue to form the Agent's returned text and are not printed directly.

The adapter receives an execution context containing `source`, `run_id`, `parent_run_id`, the emitter, and the shared cancellation context. Main Agent, Plan-Agent, and Executor-Agent use the same adapter and differ only in context.

Outer session tools already pass through `SessionToolRuntime.invoke_tool()`. The implementation must avoid rendering duplicate events for `vla_run_workflow`: the runtime owns the outer tool lifecycle, while the workflow emits its own Agent lifecycle and child events. Event identity and ownership are explicit rather than deduplicated by display text.

## Progress Summaries

Each ReAct round may produce one concise progress entry before its corresponding tool action. The visible format contains only the Agent source and natural language:

```text
[Plan] 原始片段存在；接下来识别数据 profile，以选择匹配的工具参数。
```

The TUI does not display labels such as `reasoning_step`, `思考摘要`, `第 N 轮`, or `汇总`.

Agent prompts request one or two action-oriented sentences that explain what was established and what happens next. The adapter sanitizes whitespace and known noisy markers, removes empty blocks, and applies a display length limit. It does not print raw prompts, complete tool results, or unbounded model reasoning. The final response remains a separate `final` event.

## TUI Components

### SessionController

`SessionController` owns exactly one `VLASessionAgent` for its lifetime. It provides:

- `start()` to construct the persistent Agent with a queue-backed event callback;
- `submit_turn(message)` to run one turn in a worker thread;
- `drain_events()` for non-blocking queue consumption;
- `is_turn_running()` for render-loop coordination;
- `request_interrupt()` to cancel the active turn;
- `consume_turn_result()` to retrieve terminal status without duplicating the final reply.

Only one turn may run at a time. Reusing the Agent preserves AgentScope memory, `SessionState`, tool state, and conversational history across turns.

### State and Event Adapter

`apply_event(state, event)` is a pure state transition boundary. It maps normalized events into:

- active Agent and tool status used to derive spinner text;
- completed tool call records and elapsed time;
- concise reasoning timeline items;
- lifecycle timeline items;
- final user-visible messages.

The renderer does not interpret AgentScope events or business payloads directly. This keeps the TUI replaceable and lets a future Web UI apply the same event contract to its own state store.

### Renderer

The transcript renderer is the only component that writes to stdout. Its behavior is:

1. Print the user message.
2. Show a transient `[Main] thinking...` spinner until an event arrives.
3. Clear the spinner before printing persistent events.
4. Print reasoning entries with source-specific color and indentation.
5. While a tool is active, show a transient spinner such as `[Plan] running inspect_raw_date_tool (+2s)`.
6. On `tool_end`, replace the spinner with a persistent success, failure, or interrupted entry.
7. Print the `final` event once, then return to the input prompt.

The grouped timeline uses source labels `[Main]`, `[Workflow]`, `[Plan]`, and `[Executor]`. Parent-child indentation is derived from `parent_run_id`; Agents do not insert indentation or terminal markup themselves.

`rich` supplies colors and styled text. Spinner animation uses carriage-return refresh so frames do not pollute the transcript.

## Session Interaction

Interactive mode supports:

- `Ctrl+D` at the input prompt: exit the entire session;
- `exit`, `quit`, `q`, or `退出`: emit a normal final session-ending response and exit;
- `Ctrl+C` during a running turn: interrupt only that turn and return to the prompt;
- `Ctrl+C` with no running turn: display a hint that no task is running and that `Ctrl+D` exits.

Repeated `Ctrl+C` presses during the same turn do not schedule duplicate cancellations. An interrupted turn emits interrupted lifecycle events and a final message telling the user they can continue with another request. It does not destroy the Agent or conversation state.

`--message` remains a one-shot mode, but it uses the same controller, event contract, and renderer so behavior does not diverge from interactive mode.

## Cancellation

A shared `CancellationContext` belongs to the active turn. It combines a thread-safe cancellation flag with registration of currently active AgentScope Agents and subprocesses.

When `Ctrl+C` requests cancellation:

1. Set the cancellation flag exactly once.
2. Schedule `interrupt()` for the active main, Plan, and Executor Agents on their owning asyncio loops.
3. Check cancellation before every Agent confirmation round and workflow step.
4. Stop active legacy commands and GUI processes.
5. Emit interrupted end events and return control to the TUI prompt.

The current `subprocess.run()` implementation cannot respond cooperatively. It is replaced with a polling `Popen()` implementation. Commands start in a new process group; cancellation sends a graceful termination signal to the group and escalates after a bounded grace period if it does not exit. This prevents ROS, GUI, shell, or Python descendants from surviving an interrupted turn.

Dry-run commands still return immediately without launching processes.

## Error Handling

Event lifecycles always close:

- a tool exception emits `tool_end` with `status=failed`, `error_type`, and a bounded summary;
- an Agent exception emits `agent_end` with `status=failed`;
- cancellation emits end events with `status=interrupted`;
- ordinary, help, exit, failure, and interruption replies all use one `final` event.

An event sink is observational. A sink exception is logged and cannot fail an Agent or workflow. `CompositeEventSink` attempts every sink even when one fails.

Tool arguments and results are converted to bounded previews before entering the display payload. Full navigation diagnostics remain available in workflow artifacts and JSONL records where appropriate. Secrets and environment values are not placed into display events.

## Multi-Turn State

The interactive process constructs its Agent once and keeps it until session exit. Each completed or interrupted turn returns the controller to an idle state without replacing the Agent.

`SessionState.history` records user and assistant messages. The same AgentScope Agent instance retains model memory across calls. Help and interruption responses also update history consistently. Tests must prove that a second turn can refer to information established in the first and that an interrupted turn does not prevent a later turn.

## Testing Strategy

### Event Unit Tests

- Validate event construction, timestamps, source identity, and parent linkage.
- Verify all events serialize to JSON.
- Verify `CompositeEventSink` delivers to all sinks and isolates sink failures.
- Verify JSONL output uses the normalized event envelope.

### AgentScope Adapter Tests

- Feed fake `THINKING_BLOCK_*` events and assert one bounded natural-language reasoning event.
- Feed fake tool call/result events and assert paired call IDs and status.
- Verify confirmation rounds continue to work.
- Verify main, Plan, and Executor contexts produce correct sources and parent IDs.
- Verify workflow tool events are not duplicated.

### Controller and TUI Tests

- Verify one persistent Agent handles multiple sequential turns.
- Verify a worker thread does not block queue draining.
- Verify spinner state changes for thinking and active tools.
- Verify `apply_event()` produces the approved grouped timeline without round-summary labels.
- Verify final replies render exactly once.
- Verify EOF and all exit aliases stop the session normally.
- Verify `Ctrl+C` interrupts a running turn and the next turn still succeeds.
- Verify `Ctrl+C` while idle does not exit.

### Cancellation Tests

- Cancel fake active Agents and assert each receives one interrupt request.
- Launch a controlled child process, cancel it, and assert the process group exits.
- Verify cancellation closes tool and Agent events as interrupted.

### End-to-End Verification

- Run the complete pytest suite.
- Run an interactive fake-Agent TUI scenario with two turns.
- Run a real model-backed navigation dry-run smoke test when credentials and fixture data are available.

## Future Web UI

The Web UI consumes the same normalized event envelope. A server-side adapter can subscribe through SSE or WebSocket and map `run_id` and `parent_run_id` into expandable Agent groups. No Agent or workflow changes are required.

If the Web UI later runs separately from the Agent process, the sink implementation may publish to a durable broker. The event schema, source names, lifecycle guarantees, and renderer-independent state transitions defined here remain the compatibility boundary.

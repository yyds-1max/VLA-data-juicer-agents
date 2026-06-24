# DataPilot Floating Chat Design

## Goal

Build the first Web UI entry point for the VLA multi-scene data processing agent system. The first iteration focuses on a persistent floating chat window named **DataPilot** that connects to the existing main Agent, streams normalized events, supports simple session creation and history, and renders sub-agent work in a web-native style.

The surrounding DataLoop console can remain a concept dashboard in this iteration. Most dashboard, data management, annotation, model iteration, and simulation features stay mocked or disabled. The real product surface is the DataPilot floating chat and its connection to the current backend.

## Current Context

The repository already has a conversational main Agent exposed by `vla-data-agent`. The Agent uses `VLASessionAgent`, `SessionController`, `SessionToolRuntime`, and a transport-neutral event system. The TUI consumes normalized events through `apply_event()` and `TuiState`, which are already tested for Main, Workflow, Plan, Executor, reasoning, tool lifecycle, and final reply events.

The reference HTML, `data_loop_v1.1.html`, provides the desired dark DataLoop console style and broad platform concept. It should inform the visual language, layout density, colors, and product framing, but not define backend scope. In this iteration, the concept pages are a shell around the real chat integration.

External agent UI patterns suggest separating conversation sessions from execution runs:

- OpenAI Agents SDK sessions use a session as the conversation history boundary.
- LangGraph uses thread-level persistence for short-term state and separate run execution.
- AG-UI style protocols commonly distinguish thread identity from run identity.
- CopilotKit's popup pattern is useful as an interaction reference, but this project should not adopt CopilotKit or AG-UI in the first implementation.

## Scope

In scope:

- A local Web app with a DataLoop-style application shell.
- A right-bottom floating DataPilot button.
- A DataPilot chat window opened from that button.
- ChatGPT-style user and assistant messages.
- Real interaction with the existing main Agent.
- Event streaming from the backend to the frontend.
- Web-native rendering of main Agent, workflow, Plan-Agent, Executor-Agent, and tool events.
- A simple session model with new session and recent history.
- A stop control that interrupts the running turn.

Out of scope:

- Full implementation of DataLoop data management, annotation, model iteration, or simulation features.
- Complex session search, tags, archival, sharing, or long-term memory.
- CopilotKit SDK or AG-UI protocol integration.
- Parallel turns inside the same session.
- Multi-user authentication and authorization.
- Production deployment packaging.

## Recommended Architecture

Use an event-native Web service rather than parsing CLI or TUI output.

The backend adds a lightweight Web layer that reuses the current session and event infrastructure:

- `SessionManager`: owns frontend sessions and maps each `session_id` to an isolated main Agent session.
- `WebSessionStore`: persists session metadata and chat messages. The first implementation can use SQLite under `.djx/sessions.sqlite`.
- `SessionController`: reused from the TUI path to submit turns, drain events, and request interruption.
- Existing normalized events: reused as the frontend transport contract.

Do not spawn `vla-data-agent` and scrape terminal output. That path would couple the frontend to CLI rendering and make localization and folding fragile.

Do not introduce CopilotKit or AG-UI in the first version. The UI should borrow the interaction shape of a bottom-right assistant popup, while preserving this repository's own event schema and Agent hierarchy.

## Backend API

Initial REST endpoints:

```text
POST /api/sessions
GET  /api/sessions
GET  /api/sessions/{session_id}
POST /api/sessions/{session_id}/turns
POST /api/sessions/{session_id}/interrupt
```

Initial streaming endpoint:

```text
WS /api/sessions/{session_id}/events
```

SSE is also viable, but WebSocket fits bidirectional session status and future interactive confirmation better. The first implementation can still keep all commands as REST calls and use WebSocket only for server-to-client events.

### Session Creation

`POST /api/sessions` creates a new `session_id`, initializes empty Agent history, stores metadata, and returns the new session.

Clicking `New Session` in the UI:

1. Calls `POST /api/sessions`.
2. Switches the active `session_id`.
3. Clears the current message list.
4. Resets active run state.
5. Keeps global app settings such as model and working directory.

### Session History

`GET /api/sessions` returns recent sessions only. The first version needs:

- `id`
- `title`
- `updated_at`
- `status`

The history panel displays only title and update time. It does not display the last message summary.

The title is generated from the first user message, using the first 20 to 30 Chinese characters or equivalent display length. Example: `处理 20270605 室外导航数据`.

`GET /api/sessions/{session_id}` returns metadata and persisted chat messages for restoration. It does not need to replay every event in the first version, but it should preserve the user-visible chat transcript. Detailed run artifacts remain available through workflow outputs.

### Turn Submission

`POST /api/sessions/{session_id}/turns` accepts a user message, starts one Agent turn, and returns a `turn_id`.

Rules:

- Only one running turn is allowed per session.
- A turn owns one main Agent run.
- A turn streams events to subscribers for that session.
- If a session has no title yet, the first user message generates it.

### Interruption

`POST /api/sessions/{session_id}/interrupt` calls the same interrupt path currently used by the TUI.

The UI button for interruption is an icon: a circle with a centered square. It should not show Chinese text such as `停止`. The accessible label can still be `Stop current run`.

## Event Contract

The frontend consumes the existing normalized event envelope:

```json
{
  "type": "reasoning",
  "source": "navigation.plan",
  "run_id": "plan-run",
  "parent_run_id": "workflow-run",
  "timestamp": "2026-06-24T08:00:00.000+00:00",
  "payload": {}
}
```

Initial event types:

- `reasoning`
- `agent_start`
- `tool_start`
- `tool_end`
- `agent_end`
- `final`

Initial sources:

- `main`
- `navigation.workflow`
- `navigation.plan`
- `navigation.executor`

The frontend must not parse TUI text. It should use the event envelope, `run_id`, `parent_run_id`, source labels, and payload fields.

## Frontend Components

### AppShell

Renders the DataLoop console shell, inspired by the reference HTML:

- dark background;
- left navigation;
- top bar;
- concept dashboard pages;
- compact operational styling.

Mocked sections should feel visibly non-primary. The first real interaction is DataPilot.

### AgentFloatingButton

A fixed bottom-right button that opens and closes the DataPilot chat window.

States:

- idle;
- running;
- disconnected;
- failed.

The button should remain visible across pages.

### AgentChatWindow

The main floating panel. It contains:

- `SessionHeader`
- `MessageList`
- `Composer`
- optional `SessionHistoryPanel`

The window is fixed to the lower-right area and should not block the entire console.

### SessionHeader

Displays **DataPilot**, the current session title, `History`, and `New`.

It must not display `VLA 主智能体`.

`New` creates a new session and clears the current chat. `History` opens the recent session list.

### SessionHistoryPanel

Displays recent sessions with:

- generated title;
- updated time.

Clicking a session restores the session transcript. No message preview, search, tags, archival, or grouping are required in the first version.

### MessageList

Renders the chat transcript:

- user messages aligned to the right;
- assistant messages aligned to the left;
- assistant progress shown as web-native status rows, not CLI output;
- final replies rendered once when a `final` event arrives.

The main Agent status should be localized into Chinese UI text when appropriate:

```text
[Main] 正在思考(+3s)
[Plan] 正在运行 classify_navigation_dataset_tool(+1s)
```

Do not show CLI phrasing such as `[Main] thinking (+1s)` in the Web UI.

### AgentRunSummary

Shows child Agent and tool activity.

Behavior while a child Agent is running:

- display live updates just like the main Agent;
- show timestamped Plan/Executor summaries;
- show active tool status and elapsed time;
- avoid raw argument and result JSON.

Behavior after the child Agent finishes:

- automatically collapse the child work into one summary row;
- example summary: `已读取 2 个文件，执行了 1 条命令`;
- allow click-to-expand for detailed Plan/Executor summaries and tool calls.

Expanded detail displays chronological event summaries and compact tool lifecycle lines.

Tool completion format:

```text
● completed classify_navigation_dataset_tool 0.0s
```

The bullet color communicates status:

- green for completed;
- red for failed;
- yellow or amber for interrupted/cancelled.

The UI should not display raw payloads such as profile JSON, matched topics, missing topics, or full tool results in the default message flow.

### Composer

Contains an input box and one action button.

Idle state:

- button is send;
- user can submit a new message.

Running state:

- button switches to a stop icon, represented visually as a circle with a centered square;
- clicking it calls interrupt;
- input can remain editable, but submitting another message is not allowed until the current turn ends.

## Frontend State Model

Recommended top-level state:

- `activeSessionId`
- `sessions`
- `messagesBySession`
- `activeTurn`
- `connectionStatus`
- `eventTreeByRunId`
- `activeAgents`
- `activeTools`
- `collapsedRuns`

The Web UI can port the concepts of `TuiState` and `apply_event()`, but it should not reuse terminal rendering. The shared idea is a pure event-to-state transition layer.

The state reducer should:

1. Add user messages optimistically on turn submission.
2. Track active tools by `(run_id, call_id)`.
3. Track parent-child run hierarchy through `parent_run_id`.
4. Count compact summary metrics such as file reads and command/tool calls.
5. Keep running child Agents expanded.
6. Collapse completed child Agents by default.
7. Render the final assistant reply exactly once.

## Data Flow

1. Page boots.
2. If no active session exists, call `POST /api/sessions`.
3. Connect to `WS /api/sessions/{session_id}/events`.
4. User sends a message.
5. UI appends the user message and calls `POST /api/sessions/{session_id}/turns`.
6. Backend starts a turn through `SessionController`.
7. Events stream over WebSocket.
8. Frontend reducer updates visible run state.
9. Running main and child Agents display live progress.
10. Completed child Agents collapse into summaries.
11. `final` appends the assistant message and marks the turn idle.
12. Stop icon switches back to send.

Switching sessions:

1. User opens History.
2. User clicks a recent session.
3. UI disconnects or retargets the event stream.
4. UI loads transcript with `GET /api/sessions/{id}`.
5. UI reconnects events for the selected session.

## Error Handling

Connection failure:

- show disconnected state in the floating button and chat header;
- disable turn submission until reconnected or show a retry action.

Turn failure:

- keep already streamed events visible;
- append a concise assistant error message;
- return the Composer to idle.

Interrupted turn:

- preserve streamed events;
- show interrupted status;
- collapse completed child runs and mark interrupted child runs as interrupted;
- return the Composer to idle.

Business input missing:

- display the assistant reply normally, for example when `scene_mode` is missing;
- allow the user to answer in the same session.

Concurrent submission:

- same session rejects or disables a second turn while one is running;
- users can create another session for unrelated work, but first implementation does not need multi-session parallel execution in one UI.

## Testing Strategy

Backend tests:

- create a session;
- list recent sessions;
- generate title from first user message;
- restore session transcript;
- keep histories isolated between sessions;
- submit a turn;
- stream fake normalized events;
- interrupt a running turn;
- reject concurrent turns in one session.

Frontend tests:

- floating button opens and closes the chat window;
- header shows DataPilot;
- New creates a new session and clears the window;
- History shows only title and updated time;
- selecting history restores messages;
- sending a message starts a running state;
- running state shows the stop icon instead of send;
- stop calls the interrupt endpoint;
- main Agent status uses Chinese web text, not CLI text;
- child Agent activity is live while running;
- child Agent activity collapses after completion;
- expanded child Agent details show summaries and compact tool lines;
- tool status bullet colors match completed, failed, and interrupted.

Integration tests:

- feed the existing fake Main/Workflow/Plan/Executor event sequence from TUI tests into the Web reducer;
- verify rendered message structure;
- verify final assistant reply appears once;
- verify raw tool result JSON is absent from default display.

Manual verification:

- start the local Web app;
- create a new session;
- send a dry-run navigation request;
- observe live DataPilot progress;
- verify Plan/Executor updates display during execution;
- verify completed sub-agent work collapses;
- expand details and inspect compact tool lines;
- start a new session and confirm history isolation;
- switch back through History and confirm transcript restoration;
- start a request and use the stop icon.

## Open Decisions For Later

- Whether session storage remains SQLite or moves to a server database.
- Whether completed run detail should replay full historical event streams or only transcript summaries.
- Whether the console will support multiple simultaneously visible DataPilot sessions.
- Whether future protocol compatibility with AG-UI is useful after the first version stabilizes.
- Whether workflow artifacts should be linked directly from sub-agent summaries.

## References

- OpenAI Agents SDK sessions: https://openai.github.io/openai-agents-python/sessions/
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph streaming: https://docs.langchain.com/oss/python/langgraph/streaming
- AG-UI architecture: https://docs.ag-ui.com/concepts/architecture
- CopilotKit `CopilotPopup`: https://docs.copilotkit.ai/reference/components/CopilotPopup

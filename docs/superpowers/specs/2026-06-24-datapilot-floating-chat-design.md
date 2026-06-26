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
- A draft new-session screen shown before the first message is sent.
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
- Existing pending workflow state: kept as backend Agent context for the active session, not surfaced as first-version UI.

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

The frontend must not call this endpoint merely because the page loaded, the floating window opened, or the user clicked `New`. A session begins only when the user submits the first message from the draft new-session screen.

Clicking `New Session` in the UI:

1. Marks the current session as no longer active in the frontend.
2. Switches the chat window to draft new-session state.
3. Clears the visible message list.
4. Resets active run state.
5. Keeps global app settings such as model and working directory.
6. Does not create a backend session until the user sends a message.

When the first message is sent from draft state:

1. Call `POST /api/sessions`.
2. Switch to the returned `session_id`.
3. Immediately call `POST /api/sessions/{session_id}/turns` with the user's message.
4. Generate the session title from that first user message.
5. Add the session to recent history.

### Session History

`GET /api/sessions` returns recent sessions only. The first version needs:

- `id`
- `title`
- `updated_at`
- `status`

The history panel displays only title and update time. It does not display the last message summary.

The title is generated from the first user message, using the first 20 to 30 Chinese characters or equivalent display length. Example: `处理 20270605 室外导航数据`.

`GET /api/sessions/{session_id}` returns metadata and persisted chat messages for restoration. It does not need to replay every event in the first version, but it should preserve the user-visible chat transcript. Detailed run artifacts remain available through workflow outputs.

First-version history restoration is transcript restoration only. It does not restore a live main Agent instance, does not restore executable pending workflow state, and does not show "waiting/paused, can continue" prompts in the UI. Historical sessions remain useful as records, not as resumable workspaces.

### Turn Submission

`POST /api/sessions/{session_id}/turns` accepts a user message, starts one Agent turn, and returns a `turn_id`.

Rules:

- Only one running turn is allowed per session.
- A turn owns one main Agent run.
- A turn streams events to subscribers for that session.
- If a session has no title yet, the first user message generates it.

### Session Lifecycle

The frontend has three session-related UI states:

- `draft_new_session`: no backend `session_id` exists yet; the window shows the new-task screen and a composer.
- `active_session`: a backend `session_id` exists and can receive turns.
- `history_session`: a restored transcript from a previous session; first-version UI treats it as a record, not a live Agent state.

Lifecycle rules:

- First page load and first floating-window open show `draft_new_session` unless a recent active session exists.
- Browser refresh or reopening the page restores the most recent active session when the backend service still has its active Agent state.
- Clicking `New` moves the UI to `draft_new_session`; the previous session becomes historical and should not be resumed as a live Agent state in the first version.
- Closing the floating window only hides DataPilot; it does not end the active session.
- Backend service restart clears in-memory active Agent state. Persisted sessions remain as historical transcripts only.
- The first version has no explicit "end session" button. A future archive/end action can be designed separately.

### Interruption

`POST /api/sessions/{session_id}/interrupt` calls the same interrupt path currently used by the TUI.

The UI button for interruption is an icon: a circle with a centered square. It should not show Chinese text such as `停止`. The accessible label can still be `Stop current run`.

Clicking the running-state icon is equivalent to TUI `Ctrl+C`: it interrupts the current turn, keeps the session active, and lets the user continue typing in the same conversation. It is not equivalent to TUI `Ctrl+D`, does not end the session, and does not create a new session.

User text such as `终止` remains ordinary user input. When backend `session_context.pending_workflow` exists, the main Agent may route it to `vla_continue_workflow`; the frontend does not special-case that text in the first version.

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
- `navigation.workflow.resume`
- `navigation.plan`
- `navigation.executor`

The frontend must not parse TUI text. It should use the event envelope, `run_id`, `parent_run_id`, source labels, and payload fields.

Initial lifecycle statuses include:

- `completed`
- `failed`
- `interrupted`
- `waiting_for_user_confirmation`
- `paused_by_user`
- `needs_user_input`
- `needs_active_session_workflow`

`waiting_for_user_confirmation` and `paused_by_user` are non-success workflow states, but they are not generic frontend connection failures. The first-version UI renders their final assistant text normally and does not add special confirmation banners or shortcut buttons.

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
- `DraftNewSessionView`
- `MessageList`
- `Composer`
- optional `SessionHistoryPanel`

The window is fixed to the lower-right area and should not block the entire console.

### SessionHeader

Displays **DataPilot**, the current session title, `History`, and `New`.

It must not display `VLA 主智能体`.

`New` switches to draft new-session state and clears the current visible chat. It does not create a backend session until the user sends the first message from the draft screen. `History` opens the recent session list.

### DraftNewSessionView

Shown when DataPilot has no active session selected for input, including:

- first floating-window open after backend startup when there is no recent active session;
- after the user clicks `New`;
- after backend service restart when only historical transcripts remain.

The view uses the DataLoop dark visual language and contains:

- a large title: `开始一个任务`;
- a single prominent composer;
- no historical messages;
- no backend session id.

Submitting text from this view creates a backend session and immediately submits the first turn.

### SessionHistoryPanel

Displays recent sessions with:

- generated title;
- updated time.

Clicking a session restores the session transcript. No message preview, search, tags, archival, or grouping are required in the first version.

The panel does not show pending workflow status. It does not provide `确认`, `继续`, or `终止` shortcuts. Historical sessions are restored as chat records only.

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
- `sessionMode`: `draft_new_session`, `active_session`, or `history_session`
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

1. Keep draft new-session state free of backend session ids.
2. Create a session only when the draft composer submits a non-empty first message.
3. Add user messages optimistically on turn submission.
4. Track active tools by `(run_id, call_id)`.
5. Track parent-child run hierarchy through `parent_run_id`.
6. Count compact summary metrics such as file reads and command/tool calls.
7. Keep running child Agents expanded.
8. Collapse completed child Agents by default.
9. Render the final assistant reply exactly once.
10. Treat pending workflow state as backend context, not a visible first-version UI feature.

## Data Flow

1. Page boots.
2. Frontend asks for the most recent active session if the backend still has one.
3. If a recent active session exists, load its transcript and connect to `WS /api/sessions/{session_id}/events`.
4. If no recent active session exists, show `DraftNewSessionView` and do not create a session.
5. User sends the first draft message.
6. UI calls `POST /api/sessions`, switches to the returned `session_id`, appends the user message, and calls `POST /api/sessions/{session_id}/turns`.
7. Backend starts a turn through `SessionController`.
8. Events stream over WebSocket.
9. Frontend reducer updates visible run state.
10. Running main and child Agents display live progress.
11. Completed child Agents collapse into summaries.
12. `final` appends the assistant message and marks the turn idle.
13. Stop icon switches back to send.

Switching sessions:

1. User opens History.
2. User clicks a recent session.
3. UI disconnects or retargets the event stream.
4. UI loads transcript with `GET /api/sessions/{id}`.
5. UI shows the transcript as history. First-version UI does not restore live Agent state from historical sessions.

Starting a new session:

1. User clicks `New`.
2. UI switches to `DraftNewSessionView`.
3. No backend session is created yet.
4. The previous active session becomes historical for first-version purposes.
5. The first submitted draft message creates the new backend session and first turn.

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
- return the Composer to idle;
- keep the same session active so the user can continue typing.

Business input missing:

- display the assistant reply normally, for example when `scene_mode` is missing;
- allow the user to answer in the same session.

Concurrent submission:

- same session rejects or disables a second turn while one is running;
- users can create another session for unrelated work, but first implementation does not need multi-session parallel execution in one UI.

Backend service restart:

- persisted sessions remain available as history;
- in-memory active Agent state is gone;
- the UI should show a draft new-session screen or a restored historical transcript, not claim that old work is still live.

## Testing Strategy

Backend tests:

- create a session;
- list recent sessions;
- generate title from first user message;
- restore session transcript;
- keep histories isolated between sessions;
- expose pending workflow to the main Agent context without requiring the Web UI to display it;
- submit a turn;
- stream fake normalized events;
- interrupt a running turn;
- reject concurrent turns in one session.

Frontend tests:

- floating button opens and closes the chat window;
- header shows DataPilot;
- first open with no recent active session shows `开始一个任务`;
- draft new-session screen does not create a backend session until first submit;
- New switches to draft new-session state and clears the window without creating a backend session;
- first draft submit creates a session and submits the first turn;
- History shows only title and updated time;
- selecting history restores messages without showing pending workflow controls;
- browser refresh restores the most recent active session when the backend still has active state;
- sending a message starts a running state;
- running state shows the stop icon instead of send;
- stop calls the interrupt endpoint;
- stop keeps the current session active;
- main Agent status uses Chinese web text, not CLI text;
- child Agent activity is live while running;
- child Agent activity collapses after completion;
- expanded child Agent details show summaries and compact tool lines;
- tool status bullet colors match completed, failed, interrupted, waiting, and paused states.

Integration tests:

- feed the existing fake Main/Workflow/Plan/Executor event sequence from TUI tests into the Web reducer;
- verify rendered message structure;
- verify final assistant reply appears once;
- verify raw tool result JSON is absent from default display.

Manual verification:

- start the local Web app;
- open DataPilot and verify the `开始一个任务` draft screen appears;
- send the first message and verify a session is created at submit time;
- send a dry-run navigation request;
- observe live DataPilot progress;
- verify Plan/Executor updates display during execution;
- verify completed sub-agent work collapses;
- expand details and inspect compact tool lines;
- click `New` and verify no empty history session is created;
- switch back through History and confirm transcript restoration without live-resume prompts;
- start a request and use the stop icon.

## Open Decisions For Later

- Whether session storage remains SQLite or moves to a server database.
- Whether completed run detail should replay full historical event streams or only transcript summaries.
- Whether historical sessions should ever restore live Agent state and pending workflow state.
- Whether the console will support multiple simultaneously visible DataPilot sessions.
- Whether future protocol compatibility with AG-UI is useful after the first version stabilizes.
- Whether workflow artifacts should be linked directly from sub-agent summaries.

## References

- OpenAI Agents SDK sessions: https://openai.github.io/openai-agents-python/sessions/
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph streaming: https://docs.langchain.com/oss/python/langgraph/streaming
- AG-UI architecture: https://docs.ag-ui.com/concepts/architecture
- CopilotKit `CopilotPopup`: https://docs.copilotkit.ai/reference/components/CopilotPopup

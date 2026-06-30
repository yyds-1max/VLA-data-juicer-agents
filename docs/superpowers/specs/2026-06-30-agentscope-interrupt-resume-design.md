# AgentScope Interrupt/Resume Design

Date: 2026-06-30

## Background

The current VLA data processing agent can handle navigation data, but the workflow is not durable enough for production use. The main agent currently exposes navigation processing as a tool, starts a workflow, and relies on a special `pending_workflow` / `continue_workflow` path when the executor agent needs user confirmation for camera parameters.

That solves the immediate navigation scenario, but it is not a general interrupt/resume design. The system cannot reliably pause an agent run, persist the exact waiting state, survive service restarts, and continue the same run from the interrupted tool call. The goal is to move from scenario-specific patching to a reusable, production-oriented agent runtime architecture.

The selected direction is to reuse AgentScope's App/Service runtime and Redis-backed state/event infrastructure, while keeping the existing FastAPI/WebSession API compatible during the first migration phase.

## Goals

- Use an existing durable agent runtime instead of building a custom workflow engine.
- Support interrupt/resume as a first-class capability for user confirmations and external execution waits.
- Keep all agents connected to real LLM models. No mock agent may be used in the production execution chain.
- Replace the two-agent navigation workflow with one dedicated navigation agent using a plan-and-execute plus ReAct working style.
- Stop exposing navigation data processing as a normal tool to the main agent.
- Preserve the current frontend integration surface in the first phase so the backend agent architecture can stabilize before a full frontend migration.
- Make user confirmations happen through frontend session-window dialogs instead of requiring typed confirmation text.

## Non-Goals

- Do not build a custom durable workflow engine in this phase.
- Do not fully migrate the frontend to AgentScope's native chat/session/SSE APIs in this phase.
- Do not make deterministic backend code execute the entire navigation workflow plan step by step.
- Do not add broad unrelated refactors outside the agent runtime, routing, confirmation, and navigation workflow boundaries.

## Current Problems

The current implementation has several production risks:

- Session state is split between in-memory runtime fields and ad hoc workflow checkpoint files.
- Resume behavior is special-cased for camera calibration confirmation.
- Resume starts a new executor path and edits/skips plan steps instead of continuing the same persisted agent state.
- `run_executor_agent` currently auto-confirms AgentScope confirmation events in the internal stream path, which prevents true human-in-the-loop waiting semantics.
- The main agent treats navigation processing as a tool, which makes a long-running domain workflow look like a single tool call instead of a dedicated agent conversation.
- Existing WebSession/FastAPI code owns session lifecycle and events, but does not provide durable distributed session locks, event replay, or process-restart recovery.

## Chosen Architecture

Use AgentScope App/Service as the durable agent execution kernel and keep the current FastAPI WebSession interface as a compatibility layer.

The service will have three layers:

1. Existing FastAPI application
   - Keeps static frontend hosting, dataset browsing APIs, and the current `/api/sessions` compatibility endpoints.
   - Mounts AgentScope App under an internal API prefix such as `/api/agentscope`.
   - Translates existing frontend requests/events to and from AgentScope session/chat/event primitives.

2. AgentScope runtime
   - Uses `RedisStorage` for persisted agent/session records and AgentState.
   - Uses `RedisMessageBus` for session event streams, replay logs, distributed locks, cancellation, and wakeups.
   - Uses `ChatService.run(...)` as the single path for agent execution and continuation.
   - Persists tool call states such as `ASKING`, `SUBMITTED`, and `FINISHED`.

3. Domain agents and tools
   - `MainRouterAgent`: a real LLM agent responsible for conversational routing and high-level user intent handling.
   - `NavigationDataAgent`: a real LLM agent dedicated to navigation data processing.
   - Navigation tools: data inspection, plan management, calibration confirmation request, annotation GUI execution, processing commands, output checks, overwrite/delete confirmation request, and final reporting.

## Routing Design

Navigation data processing is no longer exposed to the main agent as a normal tool. Instead, routing chooses the target agent.

The routing flow is:

1. The frontend sends a user message through the existing compatible session API.
2. The backend compatibility layer invokes or consults `MainRouterAgent`.
3. `MainRouterAgent` uses the real LLM to classify whether this is a navigation data processing request.
4. Backend high-confidence rules may participate as a guardrail and fallback. These rules must be explicit and narrow, such as detecting clear references to navigation data, ROS bag/db3 files, odometry, grid maps, camera calibration, or dataset processing commands.
5. If the request is navigation-related, the backend routes execution to `NavigationDataAgent`.
6. Otherwise, the session continues with `MainRouterAgent`.

Backend rules are allowed to improve reliability, but hidden backend business logic must not replace the agent's responsibility for understanding ambiguous user intent.

## Navigation Agent Design

`NavigationDataAgent` replaces the current Plan-Agent plus Executor-Agent split for this workflow.

The agent uses a plan-and-execute plus ReAct pattern:

1. Investigate the dataset and user request.
2. Draft a `WorkflowPlan` with ordered steps, expected inputs, outputs, and risk points.
3. Before processing data, request user confirmation for the current camera parameters.
4. Execute the plan step by step, reasoning and calling tools as needed.
5. Update task/plan status after each meaningful step.
6. Request confirmation before overwriting or deleting existing outputs.
7. Directly launch the annotation GUI when the plan reaches that step and wait for the user to finish in the GUI.
8. Retry failed tool calls when appropriate without asking the user for confirmation.
9. Produce a final summary with generated outputs, skipped steps, failures, and any next actions.

AgentScope task tools may be used to maintain visible plan/task state, but the LLM agent remains responsible for reasoning and choosing tool calls. The backend must not become a deterministic workflow interpreter for the whole navigation process.

## Interrupt/Resume Semantics

Interrupt/resume is implemented with AgentScope continuation events rather than custom workflow checkpoint branching.

User confirmation:

- Tools that require approval emit AgentScope `RequireUserConfirmEvent`.
- The related tool call remains persisted in an asking state.
- The compatibility layer converts the event into a frontend confirmation event.
- The frontend shows a session-window dialog with clear confirm/cancel actions.
- The user's choice is sent back to the backend.
- The backend resumes the same AgentScope session by calling `ChatService.run(...)` with `UserConfirmResultEvent`.

External execution:

- Long external operations may use AgentScope external execution semantics when they need a durable submitted/waiting state.
- The related tool call remains persisted in a submitted state.
- Completion is reported back through `ExternalExecutionResultEvent`.

Session safety:

- Each AgentScope session run uses AgentScope's session lock.
- Concurrent duplicate runs for the same session are rejected or serialized by the runtime.
- Service restart does not lose the agent's waiting state as long as Redis state is available.
- Old `pending_workflow`, `pending_workflow_run_dir`, and special `continue_vla_workflow` behavior should be removed after the new path is in place.

## Confirmation Policy

The user-confirmed policy is:

- Camera parameters must be confirmed before starting data processing. This happens in the same order as the current workflow, but through a frontend dialog instead of typed text.
- Overwriting existing outputs requires confirmation.
- Deleting existing outputs requires confirmation.
- Failure retry does not require user confirmation.
- The annotation GUI can be invoked directly and may block until the user finishes annotation.

Confirmation dialogs should include enough context for a safe decision, such as camera parameter values, target output paths, or the list of files/directories to overwrite or delete.

## LLM and Credential Initialization

All agents must use real LLM models.

For server deployment, credentials and model configuration are initialized from environment variables during service startup. The initial expected variables are:

- `DASHSCOPE_API_KEY`
- `DASHSCOPE_BASE_URL` if the deployment requires a custom endpoint
- `VLA_AGENT_MODEL`
- `VLA_AGENT_ROUTER_MODEL` if the router should use a separate model
- `VLA_AGENT_NAVIGATION_MODEL` if the navigation agent should use a separate model

Startup should ensure that the required AgentScope credential, model config, `MainRouterAgent`, and `NavigationDataAgent` records exist. Secrets must not be created from or exposed through the frontend.

## Compatibility Layer

The first implementation phase keeps the current frontend contract stable.

The compatibility layer should:

- Preserve the existing session creation and message submission API shape where practical.
- Preserve the existing WebSocket/event stream used by the frontend.
- Translate AgentScope message and tool events into existing frontend event types.
- Introduce a confirmation-request event that the frontend can render as a modal dialog in the chat window.
- Accept the confirmation result and resume AgentScope with the matching continuation event.
- Avoid exposing AgentScope internal IDs unnecessarily, while keeping enough correlation data to resume the correct waiting tool call.

This layer is temporary architecture, but it should still be robust. It prevents frontend migration from blocking the durable agent runtime work.

## Data and State

Redis becomes a formal runtime dependency for the server deployment.

Redis stores:

- AgentScope session records.
- AgentScope agent records.
- AgentState, including context and tool call state.
- Message/event replay data.
- Distributed session locks and cancellation signals.

The existing SQLite-backed web session store should no longer be the source of truth for agent execution state once the compatibility layer routes through AgentScope. It may remain temporarily for frontend compatibility metadata if needed, but durable execution state belongs to AgentScope/Redis.

## Error Handling

Expected handling:

- Tool failures are reported to the agent as tool results so the agent can decide whether to retry, adjust the plan, ask the user, or stop.
- Retry after failure does not require user confirmation unless the retry would overwrite/delete outputs or otherwise hit a confirmation policy.
- If Redis is unavailable, the service should fail startup or mark agent chat execution unavailable rather than silently falling back to non-durable in-memory state.
- If the frontend disconnects while a confirmation is pending, the pending AgentScope state remains available and the frontend can recover it from session events after reconnecting.
- If the process restarts while a confirmation or external execution is pending, the compatibility layer should be able to surface the pending state again from AgentScope/Redis.

## Migration Plan

Phase 1: Durable backend kernel with compatibility frontend

- Add AgentScope App mounting and Redis-backed storage/message bus configuration.
- Add startup bootstrap for real LLM credentials, model configs, and the two core agents.
- Implement backend routing from compatible web sessions to AgentScope sessions.
- Convert user messages, agent replies, tool events, confirmation requests, and confirmation results between the current frontend protocol and AgentScope.
- Implement `NavigationDataAgent` with plan-and-execute plus ReAct prompting and navigation tools.
- Replace typed confirmation with AgentScope user confirmation events and frontend dialogs.
- Keep existing frontend APIs mostly stable.

Phase 2: Remove old workflow patching

- Remove `pending_workflow` session state.
- Remove special `continue_vla_workflow` routing.
- Stop auto-confirming AgentScope confirmation events in navigation workflow streaming.
- Retire the Plan-Agent / Executor-Agent split for navigation processing.
- Keep old code only behind temporary compatibility flags if needed for controlled rollout.

Phase 3: Frontend native migration

- Move the frontend to AgentScope-native sessions/chat/SSE once backend behavior is stable.
- Remove the WebSession compatibility layer after the frontend no longer depends on it.

## Testing Strategy

Backend tests should cover:

- Routing to `NavigationDataAgent` for clear navigation requests.
- Non-navigation requests staying with `MainRouterAgent`.
- Camera parameter confirmation creates a pending confirmation event before processing starts.
- Confirming camera parameters resumes the same AgentScope session.
- Canceling camera parameters stops or replans without starting processing.
- Overwrite/delete confirmation is required before destructive output changes.
- Tool failure retry can happen without user confirmation.
- Service restart simulation preserves a pending confirmation through Redis-backed state.
- Duplicate message/confirmation submissions do not create concurrent runs for the same session.
- Compatibility events are emitted in a shape the current frontend can consume.

Manual integration tests should cover:

- Start navigation data processing from the existing web UI.
- See the camera-parameter confirmation dialog.
- Confirm and observe processing continue.
- Trigger annotation GUI and verify the system waits for completion.
- Reconnect or restart around a pending confirmation and verify the pending state is still recoverable.

## Acceptance Criteria

- Navigation processing no longer depends on custom `pending_workflow` / `continue_workflow` resume logic.
- A user confirmation pauses the same AgentScope session and resumes through `UserConfirmResultEvent`.
- MainRouterAgent and NavigationDataAgent both use real configured LLM models.
- The existing frontend can still create sessions, send messages, receive agent events, and handle confirmation dialogs.
- Redis is required for durable server runtime state.
- The implementation path does not require a full frontend rewrite in the first phase.

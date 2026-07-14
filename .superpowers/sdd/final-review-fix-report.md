# Final Review Fix Report

## Status

Fixed the Important final-review finding with a reply-entry authorization
barrier. `NavigationToolSurfaceMiddleware.on_reply()` now synchronizes the
SQLite-authoritative surface before AgentScope enters `_reply_impl`, while the
existing per-reasoning refresh and post-`ToolResponse` refresh remain intact.

The implementation deliberately uses `on_reply`, not `on_compress_context`.
This adds one authorized-snapshot read per reply and covers every path into
compression and reasoning. An `on_compress_context` hook would only cover
compression calls and would add another read whenever compression runs, while
still needing the reply-entry fail-closed behavior for missing attempts.

## AgentScope hook source evidence

AgentScope 2.0.1 source in the installed environment establishes the ordering:

- `agentscope/agent/_agent.py:500-538`: `_reply()` applies every `on_reply`
  middleware around `_reply_impl()`.
- `agentscope/agent/_agent.py:609-611`: inside `_reply_impl()`, the reasoning
  path executes `await self.compress_context()` before iterating
  `self._reasoning()`.
- `agentscope/agent/_agent.py:315-316`: compression obtains its token-count and
  split inputs from `await self._prepare_model_input()`.
- `agentscope/agent/_agent.py:1995-2021`: `_prepare_model_input()` reads skill
  instructions, tool schemas, and `activated_groups` from the current Toolkit
  and AgentState.
- `agentscope/agent/_agent.py:177-187`: hook lists are detected once during
  Agent construction, and overriding `on_reply` registers this middleware at
  the required outer boundary.

Therefore `on_reasoning` alone was too late, and `on_reply` is the earliest
existing AgentScope middleware hook that covers the entire reply without
modifying AgentScope.

## TDD evidence

### RED

Added unit tests before production code:

- `test_reply_refreshes_surface_before_forwarding_to_agent`
- `test_reply_refresh_failure_clears_surface_before_forwarding` for both a
  missing surface and an exception containing a sensitive SQLite path

Command:

```text
/Users/sfy/codes/VLA-data-juicer-agents/.venv/bin/pytest \
  tests/test_navigation_tool_surface_middleware.py -q \
  -k 'reply_refreshes_surface_before_forwarding or reply_refresh_failure_clears_surface_before_forwarding'
```

Observed RED: `3 failed, 8 deselected`. Each failure entered
`MiddlewareBase.on_reply` and raised
`RuntimeError: NavigationToolSurfaceMiddleware does not implement on_reply`.

Added real ChatService regressions before the fix:

- `test_compression_uses_authorized_surface_before_first_reasoning`
- `test_missing_attempt_fails_before_compression_or_model_call`

The compression regression initially failed because the first
`_prepare_model_input` contained generic `bash`, `read`, `task`, the real Skill
surface, the MCP-equivalent schema, and reset metadata instead of the
authorized navigation surface. The missing-attempt regression entered context
compression and attempted the scripted compression model before any bounded
navigation synchronization error.

### GREEN

The minimal production change adds `on_reply`, calls `_synchronize(agent)`
before forwarding, and otherwise preserves the middleware behavior.

The five new focused cases pass. They prove:

- the authorized groups and empty `basic` group exist before the next reply
  handler runs;
- missing/exception paths never invoke that handler, retain the Toolkit list
  identity, clear it to an empty `basic` group, clear activated groups, and
  raise only `navigation tool surface unavailable`;
- the real ChatService compression path actually runs once
  (`compact_event_count == 1`);
- its pre-compression `_prepare_model_input` contains the authorized planning
  schemas while excluding generic basic, Skill/skill-viewer, and the
  MCP-equivalent schema and Skill instructions;
- the actual compression API input exposes only
  `generate_structured_output`, with no generic, Skill, MCP-equivalent, or
  `reset_tools` content;
- the subsequent reasoning input uses the authorized navigation planning
  surface;
- a missing attempt performs one unavoidable ChatService `get_model` object
  resolution during assembly, but performs zero token-count calls, zero
  compression calls, and zero model API calls before the bounded fail-closed
  error.

Existing routine-flow `compact_event_count == 0` assertions were not weakened.
The new compression test is a separate high-context, low-trigger-ratio path.

## Verification

Focused required suite:

```text
/Users/sfy/codes/VLA-data-juicer-agents/.venv/bin/pytest \
  tests/test_navigation_tool_surface_middleware.py \
  tests/test_navigation_chat_service_tool_groups.py \
  tests/test_navigation_context_budget.py -q
```

Result: `30 passed in 1.55s`.

Full Python suite:

```text
/Users/sfy/codes/VLA-data-juicer-agents/.venv/bin/pytest -q
```

Result: `827 passed, 1 warning in 10.29s`. The warning is the existing
Starlette `httpx` deprecation warning.

Compilation:

```text
/Users/sfy/codes/VLA-data-juicer-agents/.venv/bin/python -m compileall -q src tests
```

Result: exit 0.

Whitespace validation:

```text
git diff --check
```

Result: exit 0.

## Self-review

- The production delta is one middleware hook; AgentScope is untouched.
- The barrier runs before incoming-message handling, compression token
  counting, compression inference, and reasoning inference.
- SQLite remains authoritative; restored `activated_groups` never authorize a
  tool before synchronization.
- Existing fresh refreshes on every `on_reasoning` call and after every
  terminal `ToolResponse` remain unchanged.
- Failure messages remain bounded and do not expose resolver/SQLite details.
- Tests patch only the approved AgentScope assembly seams (`get_model` and
  `get_toolkit`) and otherwise execute the real ChatService, Agent, Toolkit,
  compression, and ReAct paths.
- No processing callable behavior was changed.

No additional Important or blocking finding was found.

## Non-blocking Minor findings recorded, not expanded

1. Some tests share private helper functions across test modules.
2. The ChatService case helper returns a seven-element tuple.
3. One pre-existing regression performs a direct SQLite query.

These are recorded only; this fix does not perform a broad test-harness or
repository refactor.

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from agentscope.message import UserMsg

from vla_data_juicer_agents.capabilities.session.runtime import SessionState, SessionToolRuntime
from vla_data_juicer_agents.capabilities.session.toolkit import build_session_toolkit
from vla_data_juicer_agents.core.cancellation import CancellationContext, TurnCancelled
from vla_data_juicer_agents.core.events import EventScope
from vla_data_juicer_agents.navigation.agents import create_qwen_model
from vla_data_juicer_agents.navigation.workflow import _run_agent_stream


_logger = logging.getLogger(__name__)
_FAILED_TURN_TEXT = "Session turn failed. Please try again."


@dataclass
class SessionReply:
    text: str
    stop: bool = False
    interrupted: bool = False


class VLASessionAgent:
    def __init__(
        self,
        *,
        use_llm_router: bool = True,
        working_dir: str = "./.djx",
        model: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.state = SessionState(working_dir=working_dir)
        self._model = model
        self._tool_runtime = SessionToolRuntime(state=self.state, event_callback=event_callback)
        self._react_agent = self.build_react_agent() if use_llm_router else None
        self._turn_lock = threading.RLock()
        self._active_cancellation: CancellationContext | None = None

    def session_system_prompt(self) -> str:
        return (
            "You are the main VLA data processing session agent.\n"
            "Users talk to you in natural language. You decide whether the request is ordinary conversation "
            "or an actionable data-processing task by reasoning with the LLM and available tools.\n"
            "Do not use deterministic Python keyword routing for user intent. The LLM must decide when to call tools.\n"
            "For VLA, navigation, ROS bag, db3, odom, trajectory, gridmap, gen_box.py, or annotation processing requests, "
            "call vla_run_workflow exactly once with parsed date, segments, dry_run, approve, and model when known.\n"
            "Default to dry_run=false and perform real data processing for normal user requests.\n"
            "Set dry_run=true only when the user explicitly says dry_run in the request.\n"
            "Do not infer dry_run=true from words like preview, inspect, check, plan, first look, or similar cautious phrasing.\n"
            "dry_run is still an execution mode: for direct dry_run processing requests, keep approve=true and execute the dry-run stage loop.\n"
            "do not set approve=false merely because dry_run=true.\n"
            "Use approve=true for direct run/process requests. If the user asks only to plan, set approve=false.\n"
            "Never claim that the workflow ran unless the vla_run_workflow tool was called and its result is reflected.\n"
            "Keep progress or thinking updates to one or two action-oriented sentences.\n"
            "State one established fact and the next action.\n"
            "Do not dump or repeat prompts or raw tool results.\n"
            "After tool calls, summarize status, run_dir, artifacts, failures, and next steps.\n"
            "Respond in the same language as the user."
        )

    def _context_prompt(self, message: str) -> str:
        return (
            f"user_message: {message}\n"
            f"session_context: {json.dumps(self._tool_runtime.context_payload(), ensure_ascii=False)}"
        )

    def _build_toolkit(self):
        return build_session_toolkit(self._tool_runtime)

    def build_react_agent(self):
        from agentscope.agent import Agent

        return Agent(
            name="VLASessionAgent",
            system_prompt=self.session_system_prompt(),
            model=create_qwen_model(self._model),
            toolkit=self._build_toolkit(),
        )

    @staticmethod
    def _simple_reply(text: str, *, stop: bool = False, interrupted: bool = False) -> SessionReply:
        return SessionReply(text=text, stop=stop, interrupted=interrupted)

    def request_interrupt(self) -> bool:
        with self._turn_lock:
            cancellation = self._active_cancellation
        return cancellation.cancel() if cancellation is not None else False

    def _record_reply(
        self,
        scope: EventScope,
        text: str,
        *,
        stop: bool = False,
        interrupted: bool = False,
    ) -> SessionReply:
        safe_text = self._tool_runtime.redact_text(text)
        scope.emit("final", text=safe_text, stop=stop)
        try:
            self.state.history.append({"role": "assistant", "content": safe_text})
        except Exception:
            _logger.exception("Failed to append the session reply to history")
        return self._simple_reply(safe_text, stop=stop, interrupted=interrupted)

    async def handle_message_async(self, message: str) -> SessionReply:
        text = message.strip()
        if not text:
            return self._simple_reply("Please enter a non-empty message.")
        scope = self._tool_runtime.event_emitter.scope("main")
        cancellation = CancellationContext()
        with self._turn_lock:
            if self._active_cancellation is not None:
                raise RuntimeError("A session turn is already active.")
            self._active_cancellation = cancellation
        turn = None
        try:
            turn = self._tool_runtime.begin_turn(scope, cancellation)
            self.state.history.append({"role": "user", "content": text})
            lowered = text.lower()
            if lowered in {"exit", "quit", "q", "退出"}:
                return self._record_reply(scope, "Session ended.", stop=True)
            if lowered in {"help", "h", "?"}:
                help_text = (
                    "Describe the data-processing task in natural language. "
                    "For navigation VLA requests, include the date such as 20270605 and optional segments."
                )
                return self._record_reply(scope, help_text)
            if self._react_agent is None:
                raise RuntimeError("LLM session agent is unavailable; initialize with use_llm_router=True.")

            prompt = self._context_prompt(text)
            output = await _run_agent_stream(
                self._react_agent,
                prompt,
                event_scope=scope,
                cancellation=cancellation,
                emit_tool_events=False,
            )
            output = output.strip() or "The request was processed, but no displayable text was returned."
            return self._record_reply(scope, output)
        except TurnCancelled:
            return self._record_reply(
                scope,
                "当前任务已中断，可以继续输入下一条请求。",
                interrupted=True,
            )
        except Exception:
            _logger.exception("Session turn failed")
            return self._record_reply(scope, _FAILED_TURN_TEXT)
        finally:
            if turn is not None:
                self._tool_runtime.end_turn(turn)
            with self._turn_lock:
                if self._active_cancellation is cancellation:
                    self._active_cancellation = None

    def handle_message(self, message: str) -> SessionReply:
        return asyncio.run(self.handle_message_async(message))

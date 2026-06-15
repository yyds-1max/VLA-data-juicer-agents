from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable

from agentscope.message import UserMsg

from vla_data_juicer_agents.capabilities.session.runtime import SessionState, SessionToolRuntime
from vla_data_juicer_agents.capabilities.session.toolkit import build_session_toolkit
from vla_data_juicer_agents.navigation.agents import create_qwen_model
from vla_data_juicer_agents.navigation.workflow import _run_agent_stream


@dataclass
class SessionReply:
    text: str
    stop: bool = False


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
    def _simple_reply(text: str, *, stop: bool = False) -> SessionReply:
        return SessionReply(text=text, stop=stop)

    async def handle_message_async(self, message: str) -> SessionReply:
        text = message.strip()
        if not text:
            return self._simple_reply("Please enter a non-empty message.")
        lowered = text.lower()
        if lowered in {"exit", "quit", "q"}:
            self.state.history.append({"role": "user", "content": text})
            self.state.history.append({"role": "assistant", "content": "Session ended."})
            return self._simple_reply("Session ended.", stop=True)
        if lowered in {"help", "h", "?"}:
            help_text = (
                "Describe the data-processing task in natural language. "
                "For navigation VLA requests, include the date such as 20270605 and optional segments."
            )
            self.state.history.append({"role": "user", "content": text})
            self.state.history.append({"role": "assistant", "content": help_text})
            return self._simple_reply(help_text)
        if self._react_agent is None:
            raise RuntimeError("LLM session agent is unavailable; initialize with use_llm_router=True.")

        self.state.history.append({"role": "user", "content": text})
        prompt = self._context_prompt(text)
        output = await _run_agent_stream(self._react_agent, prompt)
        output = output.strip() or "The request was processed, but no displayable text was returned."
        self.state.history.append({"role": "assistant", "content": output})
        return self._simple_reply(output)

    def handle_message(self, message: str) -> SessionReply:
        return asyncio.run(self.handle_message_async(message))

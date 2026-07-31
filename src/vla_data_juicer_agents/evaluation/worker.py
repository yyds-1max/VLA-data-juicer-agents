from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig

from .grading import grade_case
from .host import EvaluationHost
from .models import (
    CaseResult,
    CaseRunObservation,
    EvaluationCase,
    EvaluationStatus,
    TokenUsage,
    ToolCallObservation,
)
from .trace import TraceRecorder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vla_data_juicer_agents.evaluation.worker",
        description="Run one isolated evaluation attempt.",
    )
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    return parser


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"expected a mapping, received {type(value).__name__}")


def _tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_unparsed": raw}
    return dict(parsed) if isinstance(parsed, Mapping) else {"_value": parsed}


def _to_observation(host_result: Any, *, duration_ms: int) -> CaseRunObservation:
    forbidden_names = [
        str(_mapping(call).get("name", ""))
        for call in host_result.forbidden_calls
        if _mapping(call).get("name")
    ]
    forbidden_set = set(forbidden_names)
    tool_calls: list[ToolCallObservation] = []
    for raw_call in host_result.tool_calls:
        call = _mapping(raw_call)
        name = str(call.get("name", ""))
        tool_calls.append(
            ToolCallObservation(
                name=name,
                arguments=_tool_arguments(call.get("arguments", call.get("input"))),
                result=call.get("result"),
                blocked=name in forbidden_set,
            ),
        )

    model_calls = [_mapping(call) for call in host_result.model_calls]
    return CaseRunObservation(
        final_response=host_result.final_text,
        tool_calls=tool_calls,
        forbidden_calls=forbidden_names,
        handoffs=[_mapping(payload) for payload in host_result.handoffs],
        events=[_mapping(event) for event in host_result.events],
        visible_tool_sets=[
            [str(name) for name in call.get("tools", [])]
            for call in model_calls
        ],
        token_usage=TokenUsage.model_validate(dict(host_result.token_usage)),
        model_calls=len(model_calls),
        duration_ms=duration_ms,
        metadata={
            "session_id": host_result.session_id,
            "model_calls": model_calls,
        },
    )


async def _execute(request: dict[str, Any]) -> CaseResult:
    case = EvaluationCase.model_validate(request["case"])
    attempt = int(request.get("attempt", 1))
    output_dir = Path(request["output_dir"])
    workspace_root = output_dir / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    config = replace(
        AgentScopeRuntimeConfig.from_env(workspace_root=workspace_root),
        user_id=f"eval-{case.id}-{attempt}",
    )
    messages = [turn.content for turn in case.conversation]
    started = time.monotonic()
    web_session_id = f"eval-{case.id}-{attempt}"
    host = EvaluationHost(
        config=config,
        workspace_root=workspace_root,
        runtime_setup=(
            case.runtime_setup.model_dump(mode="json")
            if case.runtime_setup is not None
            else None
        ),
        entrypoint=case.entrypoint,
    )
    try:
        host_result = await host.run(messages, web_session_id=web_session_id)
    except Exception as exc:
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        observation = _to_observation(
            host.snapshot(
                session_id=(
                    f"{web_session_id}__"
                    f"{config.main_router_agent_id if case.entrypoint == 'router' else config.navigation_agent_id}"
                ),
            ),
            duration_ms=duration_ms,
        )
        return CaseResult(
            case_id=case.id,
            suite=case.suite,
            repeat_index=attempt,
            status=EvaluationStatus.ERROR,
            observation=observation,
            error_type=type(exc).__name__,
            error_message=str(host.recorder.redact(str(exc))),
            metrics={
                "duration_ms": observation.duration_ms,
                "model_calls": observation.model_calls,
                "tool_calls": len(observation.tool_calls),
                "input_tokens": observation.token_usage.input_tokens,
                "output_tokens": observation.token_usage.output_tokens,
                "total_tokens": observation.token_usage.total_tokens,
            },
        )
    observation = _to_observation(
        host_result,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
    )
    return grade_case(case, observation, repeat_index=attempt)


def _redacted_error(error: BaseException, request: dict[str, Any]) -> str:
    output_dir = request.get("output_dir")
    recorder = TraceRecorder.for_workspace(output_dir) if output_dir else TraceRecorder()
    return str(recorder.redact(str(error)))


def _write_result(path: Path, result: CaseResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    recorder = TraceRecorder.for_workspace(path.parent)
    sanitized = recorder.redact(result.model_dump(mode="json"))
    persisted = CaseResult.model_validate(sanitized)
    path.write_text(persisted.model_dump_json(indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request: dict[str, Any] = {}
    case: EvaluationCase | None = None
    attempt = 1
    try:
        raw = json.loads(args.request_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("worker request must contain one JSON object")
        request = raw
        # The artifact path is intentionally derived from the request location so
        # no machine-specific absolute path is persisted in worker-request.json.
        request["output_dir"] = str(args.request_file.parent)
        case = EvaluationCase.model_validate(request["case"])
        attempt = int(request.get("attempt", 1))
        result = asyncio.run(_execute(request))
        _write_result(args.result_file, result)
        return 0 if result.status.value in {"PASS", "FAIL"} else 2
    except Exception as exc:
        if case is None:
            print(f"Invalid evaluation worker request: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        result = CaseResult(
            case_id=case.id,
            suite=case.suite,
            repeat_index=attempt,
            status=EvaluationStatus.ERROR,
            error_type=type(exc).__name__,
            error_message=_redacted_error(exc, request),
        )
        _write_result(args.result_file, result)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

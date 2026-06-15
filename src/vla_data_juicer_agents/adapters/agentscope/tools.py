from __future__ import annotations

import inspect
from typing import Any, Callable

from agentscope.tool import FunctionTool

from vla_data_juicer_agents.core.tool import ToolContext, ToolSpec


def _plain_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def build_agentscope_tool(
    spec: ToolSpec,
    *,
    ctx_factory: Callable[[], ToolContext],
    runtime_invoke: Callable[[str, dict[str, Any], Callable[[], Any]], Any] | None = None,
) -> FunctionTool:
    async def _tool(**kwargs: Any) -> dict[str, Any]:
        args = spec.input_model.model_validate(kwargs)

        async def _call() -> dict[str, Any]:
            payload = spec.executor(ctx_factory(), args)
            if inspect.isawaitable(payload):
                payload = await payload
            return _plain_payload(payload)

        if runtime_invoke is None:
            return await _call()

        invoked = runtime_invoke(spec.name, kwargs, _call)
        if inspect.isawaitable(invoked):
            return await invoked
        return invoked

    _tool.__name__ = spec.name
    _tool.__doc__ = spec.description
    _tool.__signature__ = _signature_from_model(spec.input_model)
    return FunctionTool(
        _tool,
        name=spec.name,
        description=spec.description,
        is_read_only=spec.effects == "read",
    )


def _signature_from_model(model: type) -> inspect.Signature:
    parameters = []
    for name, field in model.model_fields.items():
        default = inspect.Parameter.empty if field.is_required() else field.default
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=field.annotation,
            )
        )
    return inspect.Signature(parameters=parameters)

from __future__ import annotations

from collections import OrderedDict

from vla_data_juicer_agents.core.tool.contracts import ToolSpec


_REGISTRY: "OrderedDict[str, ToolSpec]" = OrderedDict()
_DEFAULTS_LOADED = False


def register_tool_spec(spec: ToolSpec) -> ToolSpec:
    if spec.name in _REGISTRY:
        raise ValueError(f"tool already registered: {spec.name}")
    _REGISTRY[spec.name] = spec
    return spec


def _ensure_default_tools() -> None:
    global _DEFAULTS_LOADED
    if _DEFAULTS_LOADED:
        return
    _DEFAULTS_LOADED = True
    from vla_data_juicer_agents.tools.vla.run_workflow import VLA_CONTINUE_WORKFLOW, VLA_RUN_WORKFLOW

    register_tool_spec(VLA_RUN_WORKFLOW)
    register_tool_spec(VLA_CONTINUE_WORKFLOW)


def list_tool_specs() -> list[ToolSpec]:
    _ensure_default_tools()
    return list(_REGISTRY.values())


def get_tool_spec(name: str) -> ToolSpec:
    _ensure_default_tools()
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown tool: {name}") from exc

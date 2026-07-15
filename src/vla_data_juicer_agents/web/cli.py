from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vla-data-agent-web",
        description="Run the DataPilot web server.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host interface for the web server.")
    parser.add_argument("--port", type=int, default=8765, help="Port for the web server.")
    parser.add_argument(
        "--working-dir",
        default="./.djx",
        help="Working directory exposed to the web app through VLA_DATA_AGENT_WEB_WORKING_DIR.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model id exposed to the web app through VLA_DATA_AGENT_WEB_MODEL.",
    )
    parser.add_argument(
        "--frontend-dist",
        default=None,
        help="Optional built frontend dist directory served by the web app.",
    )
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload.")
    return parser


def _agentscope_runtime_from_env(working_dir: str):
    enabled = os.environ.get("VLA_AGENT_ENABLE_AGENTSCOPE")
    if enabled is not None and enabled.strip().lower() in {"0", "false"}:
        return None

    from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
    from vla_data_juicer_agents.runtime.agentscope_runtime import create_agentscope_runtime

    config = AgentScopeRuntimeConfig.from_env(workspace_root=working_dir)
    return create_agentscope_runtime(config)


def create_cli_app():
    from vla_data_juicer_agents.web.app import create_app

    working_dir = os.environ.get("VLA_DATA_AGENT_WEB_WORKING_DIR", "./.djx")
    runtime = _agentscope_runtime_from_env(working_dir)
    return create_app(agentscope_runtime=runtime)


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = build_parser()
    args = parser.parse_args(argv)
    os.environ["VLA_DATA_AGENT_WEB_WORKING_DIR"] = args.working_dir
    if args.model is None:
        os.environ.pop("VLA_DATA_AGENT_WEB_MODEL", None)
    else:
        os.environ["VLA_DATA_AGENT_WEB_MODEL"] = args.model
    if args.frontend_dist is None:
        os.environ.pop("VLA_DATA_AGENT_WEB_FRONTEND_DIST", None)
    else:
        os.environ["VLA_DATA_AGENT_WEB_FRONTEND_DIST"] = args.frontend_dist

    try:
        _agentscope_runtime_from_env(args.working_dir)
    except RuntimeError as exc:
        print(f"AgentScope runtime configuration failed: {exc}", file=sys.stderr)
        return 2

    uvicorn.run(
        "vla_data_juicer_agents.web.cli:create_cli_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=None,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

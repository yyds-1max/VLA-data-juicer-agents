from __future__ import annotations

import argparse
import os


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
    uvicorn.run(
        "vla_data_juicer_agents.web.app:create_app",
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

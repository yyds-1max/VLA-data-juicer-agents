from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vla-data-agent-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--working-dir", default="./.djx")
    parser.add_argument("--model", default=None)
    parser.add_argument("--reload", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = build_parser()
    args = parser.parse_args(argv)
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

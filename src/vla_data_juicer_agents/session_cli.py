from __future__ import annotations

import argparse
import sys

from vla_data_juicer_agents.tui.app import run_tui_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vla-data-agent",
        description="LLM-backed natural-language session agent for VLA data processing workflows.",
    )
    parser.add_argument("--message", default=None, help="Run a single natural-language request and exit.")
    parser.add_argument("--model", default=None, help="Qwen model id; defaults to VLA_AGENT_MODEL or qwen3.5-plus.")
    parser.add_argument("--working-dir", default="./.djx", help="Session working directory for artifacts.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.message is not None and not str(args.message).strip():
        print("message must not be empty", file=sys.stderr)
        return 2
    return run_tui_session(args)


if __name__ == "__main__":
    raise SystemExit(main())

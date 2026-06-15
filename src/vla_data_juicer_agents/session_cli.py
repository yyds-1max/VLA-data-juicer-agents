from __future__ import annotations

import argparse
import sys

from vla_data_juicer_agents.capabilities.session.orchestrator import VLASessionAgent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vla-data-agent",
        description="LLM-backed natural-language session agent for VLA data processing workflows.",
    )
    parser.add_argument("--message", default=None, help="Run a single natural-language request and exit.")
    parser.add_argument("--model", default=None, help="Qwen model id; defaults to VLA_AGENT_MODEL or qwen3.5-plus.")
    parser.add_argument("--working-dir", default="./.djx", help="Session working directory for artifacts.")
    return parser


def _build_agent(args: argparse.Namespace) -> VLASessionAgent:
    return VLASessionAgent(
        use_llm_router=True,
        working_dir=args.working_dir,
        model=args.model,
    )


def _run_one_shot(args: argparse.Namespace) -> int:
    message = str(args.message or "").strip()
    if not message:
        print("message must not be empty", file=sys.stderr)
        return 2
    try:
        agent = _build_agent(args)
        reply = agent.handle_message(message)
    except Exception as exc:
        print(f"Failed to run vla-data-agent session: {exc}", file=sys.stderr)
        return 2
    print(reply.text)
    return 0


def _run_interactive(args: argparse.Namespace) -> int:
    try:
        agent = _build_agent(args)
    except Exception as exc:
        print(f"Failed to start vla-data-agent session: {exc}", file=sys.stderr)
        return 2

    print("VLA data agent started. Describe your task in natural language. Type `help` or `exit`.")
    while True:
        try:
            message = input("you> ")
        except EOFError:
            print("\nSession ended.")
            return 0
        try:
            reply = agent.handle_message(message)
        except Exception as exc:
            print(f"agent> session failed: {exc}")
            return 2
        print(f"agent> {reply.text}")
        if reply.stop:
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.message is not None:
        return _run_one_shot(args)
    return _run_interactive(args)


if __name__ == "__main__":
    raise SystemExit(main())


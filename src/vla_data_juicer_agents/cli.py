from __future__ import annotations

import argparse
import asyncio
import json
import sys

from vla_data_juicer_agents.core.cancellation import TurnCancelled
from vla_data_juicer_agents.navigation.agent_tools import resolve_navigation_agent_tools
from vla_data_juicer_agents.navigation.agents import create_executor_agent
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.navigation.workflow import (
    direct_execution_terminal_state,
    prepare_direct_navigation_entry,
    run_direct_plan_until_submitted,
    run_executor_agent,
)


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="VLA navigation workflow agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--date", required=True)
        sub.add_argument("--segments", nargs="+", default=None)
        sub.add_argument("--scene-mode", choices=("in", "out"), default=None)
        sub.add_argument("--dry-run", action="store_true")
        sub.add_argument("--model", default=None)
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    request = NavigationRequest(
        date=args.date,
        segments=args.segments,
        scene_mode=args.scene_mode,
        dry_run=args.dry_run,
    )
    settings = NavigationSettings()
    run_store = WorkflowRunStore(settings.runs_root)
    run_dir = run_store.create_run(request.date)
    run_store.write_json(run_dir, "request.json", request.model_dump(mode="json"))
    session_id = f"direct__{run_dir.name}"
    services = None
    task = None
    plan = None
    try:
        services, task = prepare_direct_navigation_entry(
            run_dir=run_dir,
            request=request,
            settings=settings,
            agentscope_session_id=session_id,
            user_request="CLI navigation workflow",
        )
        plan = await run_direct_plan_until_submitted(
            services=services,
            task=task,
            request=request,
            agentscope_session_id=session_id,
            model=args.model,
            run_store=run_store,
            run_dir=run_dir,
        )
        run_store.write_json(run_dir, "plan.json", plan.model_dump(mode="json"))
        if args.command == "plan":
            run_store.write_json(run_dir, "final_report.json", {"status": "planned", "ok": True})
            print(plan.model_dump_json(indent=2))
            return 0
        execution_tools = resolve_navigation_agent_tools(
            services=services,
            agentscope_session_id=session_id,
            cancellation=None,
        )
        executor = create_executor_agent(model=args.model, tools=execution_tools, dry_run=args.dry_run)
        overview = services.plan_store.get_execution_overview(plan.plan_id).model_dump(mode="json")
        final_output = await run_executor_agent(
            executor,
            plan,
            execution_overview=overview,
            run_store=run_store,
            run_dir=run_dir,
        )
        terminal = direct_execution_terminal_state(
            services=services,
            task_id=task.task_id,
            plan_id=plan.plan_id,
        )
        terminal["final_output"] = final_output
        run_store.write_json(
            run_dir,
            "final_report.json",
            terminal,
        )
        print(final_output)
        return 0 if terminal["ok"] else 2
    except TurnCancelled as exc:
        run_store.write_json(
            run_dir,
            "final_report.json",
            {"status": "failed", "ok": False, "error_type": type(exc).__name__, "message": str(exc)},
        )
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        if services is not None and task is not None and plan is not None:
            terminal = direct_execution_terminal_state(
                services=services,
                task_id=task.task_id,
                plan_id=plan.plan_id,
            )
            if terminal["ok"] or terminal["status"] in {
                "waiting_user", "failed", "needs_replan"
            }:
                terminal["error_type"] = type(exc).__name__
                terminal["message"] = str(exc)
                run_store.write_json(run_dir, "final_report.json", terminal)
                print(str(exc), file=sys.stderr)
                return 0 if terminal["ok"] else 2
        run_store.write_json(
            run_dir,
            "final_report.json",
            {"status": "failed", "ok": False, "error_type": type(exc).__name__, "message": str(exc)},
        )
        print(str(exc), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())

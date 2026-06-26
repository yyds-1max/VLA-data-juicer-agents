from __future__ import annotations

import argparse
import asyncio
import sys

from vla_data_juicer_agents.navigation.agents import create_executor_agent, create_plan_agent
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.inspection import infer_navigation_processing_profile
from vla_data_juicer_agents.navigation.models import NavigationDataProfile, NavigationRequest
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore
from vla_data_juicer_agents.navigation.workflow import (
    build_deterministic_plan_template,
    run_executor_agent,
    run_plan_agent,
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
        sub.add_argument("--model", default=None, help="Qwen model id; defaults to VLA_AGENT_MODEL or qwen3.5-plus.")
        sub.add_argument(
            "--no-llm",
            action="store_true",
            help="Only build the deterministic plan template for local dry-run debugging.",
        )

    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.scene_mode is None:
        print("Missing required --scene-mode {in,out}; please provide in or out.", file=sys.stderr)
        return 2

    request = NavigationRequest(
        date=args.date,
        segments=args.segments,
        scene_mode=args.scene_mode,
        dry_run=args.dry_run,
    )

    if args.no_llm:
        if args.command != "plan":
            print("--no-llm only supports the plan command.", file=sys.stderr)
            return 2
    settings = NavigationSettings()
    run_store = WorkflowRunStore(settings.runs_root)
    run_dir = run_store.create_run(request.date)
    run_store.write_json(run_dir, "request.json", request.model_dump(mode="json"))

    if args.no_llm:
        processing_profile = infer_navigation_processing_profile(
            request.date,
            request.segments,
            settings=settings,
        )
        if processing_profile.blocking_issues or processing_profile.topic_params.blocking_issues:
            print(processing_profile.model_dump_json(indent=2), file=sys.stderr)
            run_store.write_json(
                run_dir,
                "final_report.json",
                {
                    "status": "failed",
                    "ok": False,
                    "processing_profile": processing_profile.model_dump(mode="json"),
                },
            )
            return 2
        data_profile = NavigationDataProfile(
            date=request.date,
            segments=request.segments,
            scene_mode=request.scene_mode,
            processing_profile=processing_profile,
            platform_hint=processing_profile.platform_hint,
            sensor_bindings=processing_profile.sensor_bindings,
            localization_policy=processing_profile.localization_policy,
            topic_params=processing_profile.topic_params,
            gridmap_source=processing_profile.gridmap_policy.source,
            stage_variants=processing_profile.stage_variants,
            blocking_issues=list(processing_profile.blocking_issues),
            warnings=list(processing_profile.warnings),
            evidence=dict(processing_profile.evidence),
        )
        plan = build_deterministic_plan_template(
            request.date,
            processing_profile.id,
            request.segments,
            scene_mode=request.scene_mode,
            data_profile=data_profile,
        )
    else:
        plan_agent = create_plan_agent(model=args.model, request=request)
        plan = await run_plan_agent(plan_agent, request, run_store=run_store, run_dir=run_dir)
    run_store.write_json(run_dir, "plan.json", plan.model_dump(mode="json"))

    if args.command == "plan":
        run_store.write_json(run_dir, "final_report.json", {"status": "planned", "ok": True})
        print(plan.model_dump_json(indent=2))
        return 0

    executor_agent = create_executor_agent(model=args.model, dry_run=args.dry_run)
    final_output = await run_executor_agent(executor_agent, plan, run_store=run_store, run_dir=run_dir)
    run_store.write_json(
        run_dir,
        "final_report.json",
        {"status": "completed", "ok": True, "final_output": final_output},
    )
    print(final_output)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable, Sequence
import uuid


DEFAULT_SUITE = "router-smoke"
DEFAULT_TIMEOUT_SECONDS = 180.0
WORKER_MODULE = "vla_data_juicer_agents.evaluation.worker"


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vla-agent-eval",
        description="Validate and run local VLA agent evaluations.",
    )
    parser.add_argument(
        "--cases-root",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate",
        help="Validate evaluation cases without calling a model.",
    )
    validate.add_argument("--suite", default=DEFAULT_SUITE)

    run = subparsers.add_parser("run", help="Run an evaluation suite.")
    run.add_argument("--suite", default=DEFAULT_SUITE)
    run.add_argument("--case", dest="case_id", default=None)
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--output-dir", type=Path, default=None)
    run.add_argument("--write-baseline", action="store_true")

    compare = subparsers.add_parser(
        "compare",
        help="Compare a candidate run with a compact baseline.",
    )
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output-dir", type=Path, default=None)

    promote = subparsers.add_parser(
        "promote",
        help="Promote an audited aggregate report without calling a model.",
    )
    promote.add_argument("--input", dest="input_path", type=Path, required=True)
    promote.add_argument("--suite", default=DEFAULT_SUITE)
    return parser


def _case_id(case: Any) -> str:
    value = getattr(case, "id", None)
    if not isinstance(value, str) or not value:
        raise ValueError("evaluation case is missing a non-empty id")
    return value


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _status_value(result: Any) -> str:
    status = getattr(result, "status", None)
    if status is None and isinstance(result, dict):
        status = result.get("status")
    value = getattr(status, "value", status)
    return str(value or "ERROR").upper()


def _case_timeout(case: Any) -> float:
    limits = getattr(case, "limits", None)
    value = getattr(limits, "timeout_seconds", None)
    if value is None and isinstance(limits, dict):
        value = limits.get("timeout_seconds")
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    timeout = float(value)
    if timeout <= 0:
        raise ValueError(f"case {_case_id(case)!r} has a non-positive timeout")
    return timeout


def _load_cases(cases_root: Path, suite: str) -> list[Any]:
    from .cases import load_suite

    cases = list(load_suite(cases_root, suite))
    if not cases:
        raise ValueError(f"suite {suite!r} contains no cases")
    return cases


def _result_from_payload(payload: dict[str, Any]) -> Any:
    from .models import CaseResult

    return CaseResult.model_validate(payload)


def _synthetic_result(
    *,
    case: Any,
    attempt: int,
    status: str,
    reason: str,
    duration_seconds: float | None = None,
) -> Any:
    """Build an infrastructure result while keeping the wire contract centralized."""

    from .models import CaseResult

    if status == "TIMEOUT":
        result = CaseResult.timeout(case, repeat_index=attempt, message=reason)
    else:
        result = CaseResult.error(case, repeat_index=attempt, error=reason)
    if duration_seconds is not None:
        result.metrics["duration_ms"] = round(duration_seconds * 1000)
    return result


def _run_worker(
    *,
    case: Any,
    attempt: int,
    attempt_dir: Path,
    timeout_seconds: float,
) -> Any:
    attempt_dir.mkdir(parents=True, exist_ok=True)
    request_path = attempt_dir / "worker-request.json"
    result_path = attempt_dir / "worker-result.json"
    request_path.write_text(
        json.dumps(
            {
                "case": _model_dump(case),
                "attempt": attempt,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-m",
        WORKER_MODULE,
        "--request-file",
        str(request_path),
        "--result-file",
        str(result_path),
    ]
    started = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        duration = (datetime.now(timezone.utc) - started).total_seconds()
        return _synthetic_result(
            case=case,
            attempt=attempt,
            status="TIMEOUT",
            reason=f"worker exceeded {timeout_seconds:g}s timeout",
            duration_seconds=duration,
        )

    if result_path.is_file():
        try:
            return _result_from_payload(json.loads(result_path.read_text(encoding="utf-8")))
        except Exception as exc:
            reason = f"invalid worker result: {type(exc).__name__}: {exc}"
    else:
        stderr = completed.stderr.strip()
        reason = f"worker exited {completed.returncode} without a result"
        if stderr:
            reason = f"{reason}: {stderr[-1000:]}"
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    return _synthetic_result(
        case=case,
        attempt=attempt,
        status="ERROR",
        reason=reason,
        duration_seconds=duration,
    )


def _git_commit(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _run_metadata(*, repo_root: Path, suite: str, repeat: int, run_id: str) -> dict[str, Any]:
    from .contract import EVALUATION_CONTRACT_VERSION

    try:
        agentscope_version = version("agentscope")
    except PackageNotFoundError:
        agentscope_version = None
    return {
        "schema_version": 1,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "run_id": run_id,
        "suite": suite,
        "repeat": repeat,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(repo_root),
        "model": os.environ.get("VLA_AGENT_ROUTER_MODEL") or os.environ.get("VLA_AGENT_MODEL"),
        "model_parameters": {"parallel_tool_calls": False},
        "agentscope_version": agentscope_version,
    }


def _finalize_metadata(
    metadata: dict[str, Any],
    *,
    cases: Sequence[Any],
    results: Sequence[Any],
) -> None:
    from .cases import cases_sha256
    from vla_data_juicer_agents.runtime.agentscope_prompts import main_router_prompt

    metadata["cases_sha256"] = cases_sha256(cases)
    metadata["prompt_sha256"] = hashlib.sha256(
        main_router_prompt().encode("utf-8"),
    ).hexdigest()
    schema_hashes: set[str] = set()
    for result in results:
        observation = getattr(result, "observation", None)
        if observation is None:
            continue
        for model_call in observation.metadata.get("model_calls", []):
            digest = model_call.get("schema_hash")
            if isinstance(digest, str) and digest:
                schema_hashes.add(digest)
    metadata["tool_schema_sha256"] = sorted(schema_hashes)
    metadata["completed_at"] = datetime.now(timezone.utc).isoformat()


def _write_reports(
    *,
    results: Sequence[Any],
    metadata: dict[str, Any],
    output_dir: Path,
    baseline_dir: Path | None,
    suite: str,
) -> None:
    from .reporting import write_aggregate_report, write_baseline_reports

    write_aggregate_report(results, output_dir / "aggregate.json", run_metadata=metadata)
    if baseline_dir is not None:
        if any(_status_value(result) == "ERROR" for result in results):
            raise ValueError(
                "baseline was not written because the run contains ERROR results",
            )
        baseline_dir.mkdir(parents=True, exist_ok=True)
        write_baseline_reports(
            results,
            baseline_dir / f"{suite}.json",
            baseline_dir / f"{suite}.md",
            run_metadata=metadata,
        )


def _exit_code(results: Iterable[Any]) -> int:
    statuses = {_status_value(result) for result in results}
    if "ERROR" in statuses:
        return 2
    if statuses.intersection({"FAIL", "FAILED", "TIMEOUT"}):
        return 1
    return 0


def _select_cases(cases: list[Any], case_id: str | None) -> list[Any]:
    if case_id is None:
        return cases
    selected = [case for case in cases if _case_id(case) == case_id]
    if not selected:
        raise ValueError(f"case {case_id!r} is not part of the selected suite")
    return selected


def _print_summary(results: Sequence[Any], output_dir: Path) -> None:
    from .stability import summarize_results

    for result in results:
        case_id = getattr(result, "case_id", "unknown")
        attempt = getattr(result, "repeat_index", "?")
        print(f"{_status_value(result):7} {case_id} (attempt {attempt})")
    print("Case stability:")
    for summary in summarize_results(results):
        passed = summary.status_counts["PASS"]
        print(
            f"{summary.stability_status.value:13} {summary.case_id} "
            f"({passed}/{summary.attempts} PASS, rate={summary.pass_rate:.3f})",
        )
    print(f"Evaluation artifacts: {output_dir}")


def _compare_command(args: argparse.Namespace) -> int:
    from .comparison import (
        compare_report_files,
        comparison_exit_code,
        write_comparison_reports,
    )

    comparison = compare_report_files(args.baseline, args.candidate)
    output_dir = args.output_dir or args.candidate.resolve().parent
    json_path, markdown_path = write_comparison_reports(comparison, output_dir)
    for case in comparison["cases"]:
        print(
            f"{case['verdict']:9} {case['case_id']} "
            f"({case['baseline']['pass_rate']:.3f} -> "
            f"{case['candidate']['pass_rate']:.3f})",
        )
    print(f"Comparison JSON: {json_path}")
    print(f"Comparison Markdown: {markdown_path}")
    return comparison_exit_code(comparison)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = _repository_root()
    cases_root = args.cases_root or repo_root / "evals" / "cases"
    try:
        if args.command == "promote":
            from .promotion import promote_baseline

            promoted = promote_baseline(
                args.input_path,
                suite=args.suite,
                repo_root=repo_root,
                cases_root=cases_root,
            )
            print(
                f"Promoted {promoted.attempt_count} attempt(s) across "
                f"{promoted.case_count} case(s).",
            )
            print(f"Baseline JSON: {promoted.json_path}")
            print(f"Baseline Markdown: {promoted.markdown_path}")
            return 0

        if args.command == "compare":
            return _compare_command(args)

        cases = _load_cases(cases_root, args.suite)
        if args.command == "validate":
            print(f"Validated {len(cases)} case(s) in suite {args.suite!r}.")
            return 0

        if args.repeat <= 0:
            raise ValueError("--repeat must be a positive integer")
        if args.case_id is not None and args.write_baseline:
            raise ValueError(
                "--write-baseline requires a complete suite; remove --case",
            )
        cases = _select_cases(cases, args.case_id)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        output_dir = args.output_dir or repo_root / ".artifacts" / "evaluation" / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        metadata = _run_metadata(
            repo_root=repo_root,
            suite=args.suite,
            repeat=args.repeat,
            run_id=run_id,
        )
        results = []
        for case in cases:
            for attempt in range(1, args.repeat + 1):
                results.append(
                    _run_worker(
                        case=case,
                        attempt=attempt,
                        attempt_dir=output_dir / _case_id(case) / f"attempt-{attempt}",
                        timeout_seconds=_case_timeout(case),
                    ),
                )
        _finalize_metadata(metadata, cases=cases, results=results)
        _write_reports(
            results=results,
            metadata=metadata,
            output_dir=output_dir,
            baseline_dir=repo_root / "evals" / "baselines" if args.write_baseline else None,
            suite=args.suite,
        )
        _print_summary(results, output_dir)
        return _exit_code(results)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Evaluation configuration failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Evaluation infrastructure failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

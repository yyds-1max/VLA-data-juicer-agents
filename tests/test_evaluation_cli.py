from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.evaluation import cli
from vla_data_juicer_agents.evaluation.models import (
    CaseResult,
    EvaluationCase,
    EvaluationStatus,
)


def _case(*, timeout_seconds: int = 180) -> EvaluationCase:
    return EvaluationCase.model_validate(
        {
            "schema_version": 1,
            "id": "router_test",
            "suite": "router-smoke",
            "entrypoint": "router",
            "tags": ["test"],
            "conversation": [{"role": "user", "content": "你能做什么？"}],
            "limits": {
                "max_model_calls": 2,
                "max_tool_calls": 0,
                "timeout_seconds": timeout_seconds,
            },
            "expectations": {
                "tools": {
                    "allowed_calls": [],
                    "required_counts": {},
                    "handoff_count": 0,
                },
                "response": {"language": "Chinese"},
            },
        },
    )


def _result(status: EvaluationStatus, *, repeat_index: int = 1) -> CaseResult:
    return CaseResult(
        case_id="router_test",
        suite="router-smoke",
        repeat_index=repeat_index,
        status=status,
    )


def _comparison_report(status: str) -> dict:
    counts = {name: int(name == status) for name in ("PASS", "FAIL", "TIMEOUT", "ERROR")}
    return {
        "schema_version": 2,
        "run_metadata": {
            "suite": "router-smoke",
            "cases_sha256": "cases",
            "model": "qwen-test",
            "model_parameters": {"parallel_tool_calls": False},
            "agentscope_version": "2.0.1",
        },
        "case_summaries": [
            {
                "case_id": "router_test",
                "suite": "router-smoke",
                "attempts": 1,
                "stability_status": "ERROR" if status == "ERROR" else "SINGLE_SAMPLE",
                "status_counts": counts,
                "pass_rate": float(status == "PASS"),
                "failure_signatures": {},
            },
        ],
    }


def test_validate_only_loads_cases_and_never_runs_worker(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_load_cases", lambda root, suite: [_case()])
    monkeypatch.setattr(
        cli,
        "_run_worker",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("worker must not run")),
    )

    assert cli.main(["validate", "--suite", "router-smoke"]) == 0
    assert "Validated 1 case" in capsys.readouterr().out


def test_run_rejects_non_positive_repeat_without_running_worker(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_load_cases", lambda root, suite: [_case()])
    monkeypatch.setattr(
        cli,
        "_run_worker",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("worker must not run")),
    )

    assert cli.main(["run", "--suite", "router-smoke", "--repeat", "0"]) == 2
    assert "positive integer" in capsys.readouterr().err


def test_run_selects_case_repeats_and_returns_one_for_failures(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[int] = []

    def fake_worker(**kwargs):
        calls.append(kwargs["attempt"])
        status = EvaluationStatus.PASS if kwargs["attempt"] == 1 else EvaluationStatus.FAIL
        return _result(status, repeat_index=kwargs["attempt"])

    monkeypatch.setattr(cli, "_load_cases", lambda root, suite: [_case()])
    monkeypatch.setattr(cli, "_run_worker", fake_worker)
    monkeypatch.setattr(cli, "_write_reports", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_run_metadata", lambda **kwargs: {})

    code = cli.main(
        [
            "run",
            "--suite",
            "router-smoke",
            "--case",
            "router_test",
            "--repeat",
            "2",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert code == 1
    assert calls == [1, 2]


def test_run_returns_two_for_infrastructure_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "_load_cases", lambda root, suite: [_case()])
    monkeypatch.setattr(
        cli,
        "_run_worker",
        lambda **kwargs: _result(EvaluationStatus.ERROR),
    )
    monkeypatch.setattr(cli, "_write_reports", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_run_metadata", lambda **kwargs: {})

    assert cli.main(["run", "--output-dir", str(tmp_path)]) == 2


def test_print_summary_includes_case_stability(capsys, tmp_path: Path) -> None:
    cli._print_summary(
        [
            _result(EvaluationStatus.PASS, repeat_index=1),
            _result(EvaluationStatus.FAIL, repeat_index=2),
        ],
        tmp_path,
    )

    output = capsys.readouterr().out
    assert "Case stability:" in output
    assert "FLAKY" in output
    assert "1/2 PASS" in output


def test_partial_case_cannot_overwrite_full_baseline(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "_load_cases", lambda root, suite: [_case()])
    monkeypatch.setattr(
        cli,
        "_run_worker",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("worker must not run")),
    )

    code = cli.main(
        [
            "run",
            "--case",
            "router_test",
            "--write-baseline",
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert code == 2


def test_error_run_writes_aggregate_but_preserves_existing_baseline(tmp_path: Path) -> None:
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    json_path = baseline_dir / "router-smoke.json"
    markdown_path = baseline_dir / "router-smoke.md"
    json_path.write_text("existing-json\n", encoding="utf-8")
    markdown_path.write_text("existing-markdown\n", encoding="utf-8")

    with pytest.raises(ValueError, match="contains ERROR"):
        cli._write_reports(
            results=[_result(EvaluationStatus.ERROR)],
            metadata={"schema_version": 1},
            output_dir=tmp_path / "run",
            baseline_dir=baseline_dir,
            suite="router-smoke",
        )

    assert (tmp_path / "run" / "aggregate.json").is_file()
    assert json_path.read_text(encoding="utf-8") == "existing-json\n"
    assert markdown_path.read_text(encoding="utf-8") == "existing-markdown\n"


def test_compare_command_writes_reports_and_returns_regression_exit_code(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    candidate = candidate_dir / "aggregate.json"
    baseline.write_text(json.dumps(_comparison_report("PASS")), encoding="utf-8")
    candidate.write_text(json.dumps(_comparison_report("FAIL")), encoding="utf-8")

    code = cli.main(
        [
            "compare",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
        ],
    )

    assert code == 1
    assert (candidate_dir / "comparison.json").is_file()
    assert (candidate_dir / "comparison.md").is_file()


def test_compare_command_returns_two_for_candidate_error(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "aggregate.json"
    baseline.write_text(json.dumps(_comparison_report("FAIL")), encoding="utf-8")
    candidate.write_text(json.dumps(_comparison_report("ERROR")), encoding="utf-8")

    assert cli.main(
        [
            "compare",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
        ],
    ) == 2


def test_promote_command_delegates_without_loading_or_running_cases(
    monkeypatch,
    tmp_path: Path,
) -> None:
    aggregate = tmp_path / "aggregate.json"
    aggregate.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "_load_cases",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cases loaded in CLI")),
    )
    promoted = SimpleNamespace(
        attempt_count=9,
        case_count=3,
        json_path=tmp_path / "router-smoke.json",
        markdown_path=tmp_path / "router-smoke.md",
    )
    monkeypatch.setattr(
        "vla_data_juicer_agents.evaluation.promotion.promote_baseline",
        lambda *args, **kwargs: promoted,
    )

    assert cli.main(
        ["promote", "--input", str(aggregate), "--suite", "router-smoke"],
    ) == 0


def test_worker_subprocess_uses_json_files_and_not_secret_arguments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    seen_command: list[str] = []

    def fake_run(command, **kwargs):
        seen_command.extend(command)
        result_path = Path(command[command.index("--result-file") + 1])
        result_path.write_text(
            _result(EvaluationStatus.PASS).model_dump_json(),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-secret-that-must-not-leak")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli._run_worker(
        case=_case(),
        attempt=1,
        attempt_dir=tmp_path / "attempt-1",
        timeout_seconds=180,
    )

    assert result.status is EvaluationStatus.PASS
    assert "sk-test-secret-that-must-not-leak" not in " ".join(seen_command)
    request_path = Path(seen_command[seen_command.index("--request-file") + 1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert "DASHSCOPE_API_KEY" not in request
    assert "output_dir" not in request
    assert "sk-test-secret-that-must-not-leak" not in request_path.read_text(encoding="utf-8")


def test_worker_timeout_becomes_timeout_result(monkeypatch, tmp_path: Path) -> None:
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli._run_worker(
        case=_case(timeout_seconds=7),
        attempt=2,
        attempt_dir=tmp_path / "attempt-2",
        timeout_seconds=7,
    )

    assert result.status is EvaluationStatus.TIMEOUT
    assert result.repeat_index == 2
    assert "7s timeout" in (result.error_message or "")


def test_worker_maps_host_trace_to_observation_without_model_call(monkeypatch) -> None:
    from vla_data_juicer_agents.evaluation.worker import _to_observation

    host_result = SimpleNamespace(
        session_id="session-1",
        events=({"type": "MODEL_CALL_END", "input_tokens": 3, "output_tokens": 2},),
        model_calls=(
            {
                "model_name": "qwen-test",
                "tools": ["start_navigation_data_task", "execute_bash_command"],
                "schema_hash": "abc",
            },
        ),
        tool_calls=(
            {
                "id": "call-1",
                "name": "start_navigation_data_task",
                "input": '{"date":"20260101"}',
            },
        ),
        forbidden_calls=(),
        handoffs=({"date": "20260101"},),
        final_text="已转交。",
        token_usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )

    observation = _to_observation(host_result, duration_ms=12)

    assert observation.final_response == "已转交。"
    assert observation.tool_calls[0].arguments == {"date": "20260101"}
    assert observation.visible_tool_sets == [
        ["start_navigation_data_task", "execute_bash_command"],
    ]
    assert observation.token_usage.total_tokens == 5
    assert observation.model_calls == 1
    assert observation.duration_ms == 12


def test_worker_preserves_partial_trace_when_provider_fails(monkeypatch, tmp_path: Path) -> None:
    import asyncio

    from vla_data_juicer_agents.evaluation import worker
    from vla_data_juicer_agents.evaluation.host import HostRunResult

    class FailingHost:
        recorder = SimpleNamespace(redact=lambda value: value)

        def __init__(self, **_kwargs):
            pass

        async def run(self, _message, *, web_session_id):
            raise ConnectionError("provider unavailable")

        def snapshot(self, *, session_id):
            return HostRunResult(
                session_id=session_id,
                events=(),
                model_calls=(
                    {
                        "model_name": "qwen-test",
                        "tools": ["start_navigation_data_task"],
                        "schema_hash": "abc",
                    },
                ),
                tool_calls=(),
                forbidden_calls=(),
                handoffs=(),
                final_text="",
                token_usage={
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
            )

    monkeypatch.setattr(worker, "EvaluationHost", FailingHost)
    request = {
        "case": _case().model_dump(mode="json"),
        "attempt": 1,
        "output_dir": str(tmp_path),
    }

    result = asyncio.run(worker._execute(request))

    assert result.status is EvaluationStatus.ERROR
    assert result.error_type == "ConnectionError"
    assert result.observation is not None
    assert result.observation.model_calls == 1
    assert result.observation.metadata["model_calls"][0]["schema_hash"] == "abc"

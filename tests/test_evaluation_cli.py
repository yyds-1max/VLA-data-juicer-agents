from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

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

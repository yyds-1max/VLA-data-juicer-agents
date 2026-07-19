from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vla_data_juicer_agents.evaluation import promotion
from vla_data_juicer_agents.evaluation.models import CaseResult, EvaluationStatus


def _case_payload(case_id: str) -> dict:
    return {
        "schema_version": 1,
        "id": case_id,
        "suite": "router-smoke",
        "entrypoint": "router",
        "tags": ["test"],
        "conversation": [{"role": "user", "content": "测试"}],
        "limits": {"max_model_calls": 2, "max_tool_calls": 0, "timeout_seconds": 10},
        "expectations": {
            "tools": {"allowed_calls": [], "required_counts": {}, "handoff_count": 0},
            "response": {"language": "Chinese"},
        },
    }


def _setup_suite(tmp_path: Path, ids: tuple[str, ...] = ("case_a", "case_b")) -> Path:
    suite_dir = tmp_path / "cases" / "router-smoke"
    suite_dir.mkdir(parents=True)
    for case_id in ids:
        (suite_dir / f"{case_id}.yaml").write_text(
            json.dumps(_case_payload(case_id), ensure_ascii=False),
            encoding="utf-8",
        )
    return tmp_path / "cases"


def _write_aggregate(
    tmp_path: Path,
    cases_root: Path,
    *,
    commit: str = "head123",
    repeat: int = 2,
    statuses: dict[tuple[str, int], EvaluationStatus] | None = None,
    included_ids: tuple[str, ...] = ("case_a", "case_b"),
    cases_hash: str | None = None,
    schema_version: int = 2,
) -> Path:
    cases = promotion.load_suite(cases_root, "router-smoke")
    results = []
    for case_id in included_ids:
        for attempt in range(1, repeat + 1):
            status = (statuses or {}).get((case_id, attempt), EvaluationStatus.PASS)
            results.append(
                CaseResult(
                    case_id=case_id,
                    suite="router-smoke",
                    repeat_index=attempt,
                    status=status,
                    error_type="ProviderError" if status is EvaluationStatus.ERROR else None,
                ).model_dump(mode="json"),
            )
    report = {
        "schema_version": schema_version,
        "run_metadata": {
            "suite": "router-smoke",
            "evaluation_contract_version": 2,
            "repeat": repeat,
            "git_commit": commit,
            "cases_sha256": cases_hash or promotion.cases_sha256(cases),
            "model": "qwen-test",
            "model_parameters": {"parallel_tool_calls": False},
            "agentscope_version": "1.0",
        },
        "summary": {},
        "results": results,
    }
    path = tmp_path / "aggregate.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


@pytest.fixture
def clean_git(monkeypatch):
    monkeypatch.setattr(promotion, "_git_head", lambda root: "head123")
    monkeypatch.setattr(promotion, "_worktree_changes", lambda root: "")


def test_promotes_complete_run_atomically_without_model(
    monkeypatch,
    tmp_path: Path,
    clean_git,
) -> None:
    cases_root = _setup_suite(tmp_path)
    statuses = {
        ("case_a", 2): EvaluationStatus.FAIL,
        ("case_b", 2): EvaluationStatus.TIMEOUT,
    }
    aggregate = _write_aggregate(tmp_path, cases_root, statuses=statuses)
    baseline_dir = tmp_path / "baselines"
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(promotion.os, "replace", recording_replace)
    monkeypatch.setattr(
        "vla_data_juicer_agents.evaluation.host.EvaluationHost",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model host must not run")),
    )

    promoted = promotion.promote_baseline(
        aggregate,
        suite="router-smoke",
        repo_root=tmp_path,
        cases_root=cases_root,
        baseline_dir=baseline_dir,
    )

    assert promoted.case_count == 2
    assert promoted.attempt_count == 4
    assert promoted.json_path.is_file()
    assert promoted.markdown_path.is_file()
    assert len(replacements) == 2
    assert all(source.name.startswith(".router-smoke.") for source, _ in replacements)
    assert not list(baseline_dir.glob("*.tmp"))
    report = json.loads(promoted.json_path.read_text(encoding="utf-8"))
    assert [item["status"] for item in report["results"]] == [
        "PASS",
        "FAIL",
        "PASS",
        "TIMEOUT",
    ]
    assert "conversation" not in promoted.json_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("stale", "HEAD"),
        ("hash", "cases_sha256"),
        ("contract", "evaluation_contract_version"),
        ("partial", "complete suite"),
        ("error", "ERROR"),
    ],
)
def test_rejects_stale_hash_partial_and_error_runs(
    tmp_path: Path,
    clean_git,
    mutation: str,
    message: str,
) -> None:
    cases_root = _setup_suite(tmp_path)
    kwargs = {}
    if mutation == "stale":
        kwargs["commit"] = "old123"
    elif mutation == "hash":
        kwargs["cases_hash"] = "stale-cases"
    elif mutation == "contract":
        aggregate = _write_aggregate(tmp_path, cases_root)
        raw = json.loads(aggregate.read_text(encoding="utf-8"))
        raw["run_metadata"]["evaluation_contract_version"] = 1
        aggregate.write_text(json.dumps(raw), encoding="utf-8")
    elif mutation == "partial":
        kwargs["included_ids"] = ("case_a",)
    elif mutation == "error":
        kwargs["statuses"] = {("case_a", 1): EvaluationStatus.ERROR}
    if mutation != "contract":
        aggregate = _write_aggregate(tmp_path, cases_root, **kwargs)

    with pytest.raises(promotion.PromotionError, match=message):
        promotion.promote_baseline(
            aggregate,
            suite="router-smoke",
            repo_root=tmp_path,
            cases_root=cases_root,
            baseline_dir=tmp_path / "baselines",
        )


def test_rejects_dirty_worktree(monkeypatch, tmp_path: Path, clean_git) -> None:
    cases_root = _setup_suite(tmp_path)
    aggregate = _write_aggregate(tmp_path, cases_root)
    monkeypatch.setattr(promotion, "_worktree_changes", lambda root: " M tracked.py")

    with pytest.raises(promotion.PromotionError, match="worktree must be clean"):
        promotion.promote_baseline(
            aggregate,
            suite="router-smoke",
            repo_root=tmp_path,
            cases_root=cases_root,
        )


def test_rejects_non_contiguous_or_duplicate_attempts(tmp_path: Path, clean_git) -> None:
    cases_root = _setup_suite(tmp_path)
    aggregate = _write_aggregate(tmp_path, cases_root)
    raw = json.loads(aggregate.read_text(encoding="utf-8"))
    raw["results"][1]["repeat_index"] = 1
    aggregate.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(promotion.PromotionError, match="attempts must be exactly"):
        promotion.promote_baseline(
            aggregate,
            suite="router-smoke",
            repo_root=tmp_path,
            cases_root=cases_root,
        )


def test_accepts_v1_aggregate(tmp_path: Path, clean_git) -> None:
    cases_root = _setup_suite(tmp_path)
    aggregate = _write_aggregate(tmp_path, cases_root, schema_version=1)

    result = promotion.promote_baseline(
        aggregate,
        suite="router-smoke",
        repo_root=tmp_path,
        cases_root=cases_root,
        baseline_dir=tmp_path / "baselines",
    )

    assert result.json_path.exists()


def test_report_build_failure_preserves_existing_baseline(
    monkeypatch,
    tmp_path: Path,
    clean_git,
) -> None:
    cases_root = _setup_suite(tmp_path)
    aggregate = _write_aggregate(tmp_path, cases_root)
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    old_json = baseline_dir / "router-smoke.json"
    old_markdown = baseline_dir / "router-smoke.md"
    old_json.write_text("old-json", encoding="utf-8")
    old_markdown.write_text("old-markdown", encoding="utf-8")

    def fail_after_first_temporary_file(results, json_path, markdown_path, **kwargs):
        Path(json_path).write_text("incomplete", encoding="utf-8")
        raise RuntimeError("report rendering failed")

    monkeypatch.setattr(promotion, "write_baseline_reports", fail_after_first_temporary_file)

    with pytest.raises(RuntimeError, match="report rendering failed"):
        promotion.promote_baseline(
            aggregate,
            suite="router-smoke",
            repo_root=tmp_path,
            cases_root=cases_root,
            baseline_dir=baseline_dir,
        )

    assert old_json.read_text(encoding="utf-8") == "old-json"
    assert old_markdown.read_text(encoding="utf-8") == "old-markdown"
    assert not list(baseline_dir.glob("*.tmp"))


def test_second_replace_failure_restores_existing_baseline_pair(
    monkeypatch,
    tmp_path: Path,
    clean_git,
) -> None:
    cases_root = _setup_suite(tmp_path)
    aggregate = _write_aggregate(tmp_path, cases_root)
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    old_json = baseline_dir / "router-smoke.json"
    old_markdown = baseline_dir / "router-smoke.md"
    old_json.write_text("old-json", encoding="utf-8")
    old_markdown.write_text("old-markdown", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def fail_candidate_markdown_once(source, destination):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("markdown replacement failed")
        real_replace(source, destination)

    monkeypatch.setattr(promotion.os, "replace", fail_candidate_markdown_once)

    with pytest.raises(OSError, match="markdown replacement failed"):
        promotion.promote_baseline(
            aggregate,
            suite="router-smoke",
            repo_root=tmp_path,
            cases_root=cases_root,
            baseline_dir=baseline_dir,
        )

    assert old_json.read_text(encoding="utf-8") == "old-json"
    assert old_markdown.read_text(encoding="utf-8") == "old-markdown"
    assert not list(baseline_dir.glob("*.tmp"))
    assert not list(baseline_dir.glob("*.bak"))

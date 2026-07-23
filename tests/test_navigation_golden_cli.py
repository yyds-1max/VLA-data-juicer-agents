from __future__ import annotations

import json
from pathlib import Path

from vla_data_juicer_agents.navigation.golden import cli


def _write_cases(path: Path) -> None:
    path.write_text(
        """
schema_version: 1
runtime_id: navigation_odom_v1
cases:
  - id: odom_golden
""".lstrip(),
        encoding="utf-8",
    )


def test_validate_capture_and_compare_cli(tmp_path: Path, capsys) -> None:
    cases = tmp_path / "cases.yaml"
    _write_cases(cases)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "value.json").write_text('{"value":1}', encoding="utf-8")
    (candidate / "value.json").write_text('{"value":1}', encoding="utf-8")

    assert cli.main(["validate", "--cases", str(cases)]) == 0
    snapshot_path = tmp_path / "reports" / "snapshot.json"
    assert cli.main(
        [
            "capture",
            "--cases",
            str(cases),
            "--case",
            "odom_golden",
            "--root",
            str(baseline),
            "--role",
            "legacy",
            "--output",
            str(snapshot_path),
        ],
    ) == 0
    snapshot = snapshot_path.read_text(encoding="utf-8")
    assert str(baseline) not in snapshot
    assert json.loads(snapshot)["case_id"] == "odom_golden"

    output_dir = tmp_path / "comparison"
    assert cli.main(
        [
            "compare",
            "--cases",
            str(cases),
            "--case",
            "odom_golden",
            "--baseline-root",
            str(baseline),
            "--candidate-root",
            str(candidate),
            "--output-dir",
            str(output_dir),
        ],
    ) == 0
    assert json.loads((output_dir / "comparison.json").read_text())["verdict"] == "EQUIVALENT"
    output = capsys.readouterr().out
    assert "EQUIVALENT" in output
    assert str(snapshot_path) not in output


def test_compare_returns_one_for_business_difference(tmp_path: Path) -> None:
    cases = tmp_path / "cases.yaml"
    _write_cases(cases)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "value.json").write_text('{"value":1}', encoding="utf-8")
    (candidate / "value.json").write_text('{"value":2}', encoding="utf-8")

    assert cli.main(
        [
            "compare",
            "--cases",
            str(cases),
            "--case",
            "odom_golden",
            "--baseline-root",
            str(baseline),
            "--candidate-root",
            str(candidate),
            "--output-dir",
            str(tmp_path / "comparison"),
        ],
    ) == 1


def test_cli_refuses_to_write_inside_input_root(tmp_path: Path, capsys) -> None:
    cases = tmp_path / "cases.yaml"
    _write_cases(cases)
    root = tmp_path / "input"
    root.mkdir()

    assert cli.main(
        [
            "capture",
            "--cases",
            str(cases),
            "--case",
            "odom_golden",
            "--root",
            str(root),
            "--role",
            "legacy",
            "--output",
            str(root / "snapshot.json"),
        ],
    ) == 2
    assert "refusing to write" in capsys.readouterr().err


def test_cli_redacts_absolute_paths_and_secrets_from_errors(tmp_path: Path, capsys) -> None:
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        """
schema_version: 1
runtime_id: navigation_odom_v1
cases:
  - id: odom_golden
    artifact_scope: /Users/private/sk-abcdefghijklmnop
""".lstrip(),
        encoding="utf-8",
    )

    assert cli.main(["validate", "--cases", str(cases)]) == 2
    error = capsys.readouterr().err
    assert "/Users/private" not in error
    assert "sk-abcdefghijklmnop" not in error
    assert "[PATH]" in error or "[REDACTED]" in error


def test_cli_redacts_generic_absolute_paths_and_bearer_tokens(
    tmp_path: Path,
    capsys,
) -> None:
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        """
schema_version: 1
runtime_id: navigation_odom_v1
cases:
  - id: odom_golden
    artifact_scope: "/mnt/company/Bearer eyJhbGciOiJub25l"
""".lstrip(),
        encoding="utf-8",
    )

    assert cli.main(["validate", "--cases", str(cases)]) == 2
    error = capsys.readouterr().err
    assert "/mnt/company" not in error
    assert "eyJhbGciOiJub25l" not in error
    assert "[PATH]" in error or "[REDACTED]" in error

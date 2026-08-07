from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG_HELPER = ROOT / "scripts" / "run_web_config.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_web_config_under_test",
        CONFIG_HELPER,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_path(home: Path) -> Path:
    config_dir = home / ".config" / "vla-data-juicer-agents"
    config_dir.mkdir(parents=True, mode=0o700)
    (home / ".config").chmod(0o700)
    config_dir.chmod(0o700)
    return config_dir / "run-web.json"


def _write_config(
    home: Path,
    value: object,
    *,
    mode: int = 0o600,
) -> Path:
    path = _config_path(home)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)
    return path


def test_fixed_config_is_optional_and_has_no_path_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    home = tmp_path / "home"
    home.mkdir()
    alternate = tmp_path / "alternate.json"
    alternate.write_text(
        json.dumps({"WORKING_DIR": "/must-not-load"}),
        encoding="utf-8",
    )
    alternate.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VLA_RUN_WEB_CONFIG", str(alternate))

    assert helper.fixed_config_path() == (
        home / ".config" / "vla-data-juicer-agents" / "run-web.json"
    )
    assert helper.load_fixed_config() == {}


def test_fixed_config_loads_allowlisted_string_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    home = tmp_path / "home"
    home.mkdir()
    _write_config(
        home,
        {
            "WORKING_DIR": "/srv/datapilot/state",
            "VLA_FRONTEND_NODE_BIN_DIR": "/srv/node/bin",
            "VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS": "21600",
            "VLA_TRAINING_DEV_ADMIN": "0",
            "VLA_TRAINING_SIMULATION_ENABLED": "1",
        },
    )
    monkeypatch.setenv("HOME", str(home))

    assert helper.load_fixed_config() == {
        "WORKING_DIR": "/srv/datapilot/state",
        "VLA_FRONTEND_NODE_BIN_DIR": "/srv/node/bin",
        "VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS": "21600",
        "VLA_TRAINING_DEV_ADMIN": "0",
        "VLA_TRAINING_SIMULATION_ENABLED": "1",
    }


def test_explicit_caller_environment_takes_precedence() -> None:
    helper = _load_helper()

    environment = helper.deployment_environment(
        {
            "WORKING_DIR": "/configured",
            "VLA_FRONTEND_NODE_BIN_DIR": "/configured/node",
        },
        {
            "WORKING_DIR": "/explicit",
            "DASHSCOPE_API_KEY": "inherited-secret",
        },
    )

    assert environment["WORKING_DIR"] == "/explicit"
    assert environment["VLA_FRONTEND_NODE_BIN_DIR"] == "/configured/node"
    assert environment["DASHSCOPE_API_KEY"] == "inherited-secret"
    assert environment["VLA_RUN_WEB_CONFIG_LOADED"] == "1"


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            '{"WORKING_DIR":"/one","WORKING_DIR":"/two"}',
            "duplicate key",
        ),
        (
            '{"DASHSCOPE_API_KEY":"must-not-be-stored-here"}',
            "unsupported key",
        ),
        (
            '{"WEB_CMD":"/tmp/fake-web"}',
            "unsupported key",
        ),
        (
            '["not-an-object"]',
            "must be a JSON object",
        ),
        (
            '{"WORKING_DIR":42}',
            "must be a non-empty string",
        ),
        (
            '{"WORKING_DIR":""}',
            "must be a non-empty string",
        ),
        (
            '{"WORKING_DIR":"bad\\u0000value"}',
            "must be a non-empty string",
        ),
    ),
)
def test_fixed_config_rejects_invalid_json_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    message: str,
) -> None:
    helper = _load_helper()
    home = tmp_path / "home"
    home.mkdir()
    path = _config_path(home)
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(helper.DeploymentConfigError, match=message):
        helper.load_fixed_config()


def test_fixed_config_rejects_oversized_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    home = tmp_path / "home"
    home.mkdir()
    path = _config_path(home)
    path.write_bytes(b" " * (helper.MAX_CONFIG_BYTES + 1))
    path.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(helper.DeploymentConfigError):
        helper.load_fixed_config()


def test_fixed_config_rejects_symlink_and_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    home = tmp_path / "home"
    home.mkdir()
    path = _config_path(home)
    target = tmp_path / "target.json"
    target.write_text('{"WORKING_DIR":"/configured"}', encoding="utf-8")
    target.chmod(0o600)
    path.symlink_to(target)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(helper.DeploymentConfigError):
        helper.load_fixed_config()

    path.unlink()
    os.link(target, path)
    with pytest.raises(helper.DeploymentConfigError, match="unsafe"):
        helper.load_fixed_config()


@pytest.mark.parametrize("mode", (0o400, 0o640, 0o644, 0o660))
def test_fixed_config_requires_exact_private_file_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    helper = _load_helper()
    home = tmp_path / "home"
    home.mkdir()
    _write_config(home, {"WORKING_DIR": "/configured"}, mode=mode)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(helper.DeploymentConfigError, match="unsafe"):
        helper.load_fixed_config()


def test_fixed_config_rejects_unsafe_application_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    home = tmp_path / "home"
    home.mkdir()
    path = _write_config(home, {"WORKING_DIR": "/configured"})
    path.parent.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(helper.DeploymentConfigError, match="directory is unsafe"):
        helper.load_fixed_config()


def test_fixed_config_rejects_foreign_owner_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    current_euid = os.geteuid()
    home = tmp_path / "home"
    home.mkdir()
    _write_config(home, {"WORKING_DIR": "/configured"})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(helper.os, "geteuid", lambda: current_euid + 1)

    with pytest.raises(helper.DeploymentConfigError, match="directory is unsafe"):
        helper.load_fixed_config()


def test_fixed_config_rejects_non_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    home = tmp_path / "home"
    home.mkdir()
    path = _config_path(home)
    path.mkdir(mode=0o600)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(helper.DeploymentConfigError, match="unsafe"):
        helper.load_fixed_config()

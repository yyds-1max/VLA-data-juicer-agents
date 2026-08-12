from __future__ import annotations

import pytest

from vla_data_juicer_agents.training.auth import (
    TRAINING_CREATE_RUNS,
    TRAINING_MANAGE_MODELS,
    TRAINING_MANAGE_NODES,
    TRAINING_STOP_RUNS,
    TRAINING_VIEW,
    TrainingSettings,
)


def test_training_auth_defaults_to_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLA_TRAINING_DEV_ADMIN", raising=False)
    monkeypatch.delenv("VLA_TRAINING_SIMULATION_ENABLED", raising=False)
    monkeypatch.delenv("VLA_TRAINING_CENTER_BASE_URL", raising=False)

    settings = TrainingSettings.from_env()
    principal = settings.principal()

    assert settings.simulation_enabled is True
    assert principal.subject == "anonymous-read-only"
    assert principal.permissions == frozenset({TRAINING_VIEW})


def test_training_dev_admin_is_explicit_and_simulation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLA_TRAINING_DEV_ADMIN", "1")
    monkeypatch.setenv("VLA_TRAINING_SIMULATION_ENABLED", "1")

    principal = TrainingSettings.from_env().principal()

    assert principal.permissions == frozenset(
        {
            TRAINING_VIEW,
            TRAINING_MANAGE_NODES,
            TRAINING_MANAGE_MODELS,
            TRAINING_CREATE_RUNS,
            TRAINING_STOP_RUNS,
        }
    )

    monkeypatch.setenv("VLA_TRAINING_SIMULATION_ENABLED", "0")
    with pytest.raises(ValueError, match="requires training simulation"):
        TrainingSettings.from_env()


def test_training_auth_rejects_ambiguous_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLA_TRAINING_DEV_ADMIN", "sometimes")

    with pytest.raises(ValueError, match="must be a boolean"):
        TrainingSettings.from_env()


def test_training_center_url_must_be_an_https_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLA_TRAINING_CENTER_BASE_URL", "http://center.example/api")

    with pytest.raises(ValueError, match="valid HTTPS origin"):
        TrainingSettings.from_env()

    monkeypatch.setenv(
        "VLA_TRAINING_CENTER_BASE_URL", "https://center.example:8443"
    )
    assert TrainingSettings.from_env().center_base_url == (
        "https://center.example:8443"
    )

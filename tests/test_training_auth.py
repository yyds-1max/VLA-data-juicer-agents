from __future__ import annotations

import pytest

from vla_data_juicer_agents.training.auth import (
    TRAINING_CREATE_RUNS,
    TRAINING_MANAGE_MODELS,
    TRAINING_STOP_RUNS,
    TRAINING_VIEW,
    TrainingSettings,
)


def test_training_auth_defaults_to_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLA_TRAINING_DEV_ADMIN", raising=False)
    monkeypatch.delenv("VLA_TRAINING_SIMULATION_ENABLED", raising=False)

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

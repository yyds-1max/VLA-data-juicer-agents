from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Final
from urllib.parse import urlsplit


TRAINING_VIEW: Final = "training:view"
TRAINING_MANAGE_NODES: Final = "training:manage_nodes"
TRAINING_MANAGE_MODELS: Final = "training:manage_models"
TRAINING_CREATE_RUNS: Final = "training:create_runs"
TRAINING_STOP_RUNS: Final = "training:stop_runs"

_ADMIN_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {
        TRAINING_VIEW,
        TRAINING_MANAGE_NODES,
        TRAINING_MANAGE_MODELS,
        TRAINING_CREATE_RUNS,
        TRAINING_STOP_RUNS,
    }
)
_READ_ONLY_PERMISSIONS: Final[frozenset[str]] = frozenset({TRAINING_VIEW})


@dataclass(frozen=True)
class TrainingPrincipal:
    """Authenticated identity used by the training application boundary."""

    subject: str
    authentication_mode: str
    permissions: frozenset[str]

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def public_projection(self) -> dict[str, object]:
        return {
            "authentication_mode": self.authentication_mode,
            "permissions": sorted(self.permissions),
        }


@dataclass(frozen=True)
class TrainingSettings:
    """Security-sensitive training switches sourced from deployment settings."""

    simulation_enabled: bool = True
    real_execution_enabled: bool = False
    development_admin: bool = False
    center_base_url: str | None = None
    center_ca_certificate_path: str | None = None

    def __post_init__(self) -> None:
        if self.development_admin and not self.simulation_enabled:
            raise ValueError(
                "VLA_TRAINING_DEV_ADMIN requires training simulation to be enabled"
            )
        if self.center_base_url is not None:
            try:
                parsed = urlsplit(self.center_base_url)
                port = parsed.port
            except ValueError as exc:
                raise ValueError(
                    "VLA_TRAINING_CENTER_BASE_URL must be a valid HTTPS origin"
                ) from exc
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
                or (port is not None and not 1 <= port <= 65535)
            ):
                raise ValueError(
                    "VLA_TRAINING_CENTER_BASE_URL must be a valid HTTPS origin"
                )
        if self.center_ca_certificate_path is not None:
            if self.center_base_url is None:
                raise ValueError(
                    "VLA_TRAINING_CENTER_CA_CERT_PATH requires VLA_TRAINING_CENTER_BASE_URL"
                )
            if (
                len(self.center_ca_certificate_path) > 4096
                or any(
                    character in self.center_ca_certificate_path
                    for character in ("\x00", "\r", "\n")
                )
            ):
                raise ValueError(
                    "VLA_TRAINING_CENTER_CA_CERT_PATH must be a valid file path"
                )

    @classmethod
    def from_env(cls) -> TrainingSettings:
        return cls(
            simulation_enabled=_env_bool("VLA_TRAINING_SIMULATION_ENABLED", True),
            real_execution_enabled=_env_bool(
                "VLA_TRAINING_REAL_EXECUTION_ENABLED", False
            ),
            development_admin=_env_bool("VLA_TRAINING_DEV_ADMIN", False),
            center_base_url=(
                os.environ.get("VLA_TRAINING_CENTER_BASE_URL", "").strip() or None
            ),
            center_ca_certificate_path=(
                os.environ.get("VLA_TRAINING_CENTER_CA_CERT_PATH", "").strip()
                or None
            ),
        )

    def principal(self) -> TrainingPrincipal:
        if self.development_admin:
            return TrainingPrincipal(
                subject="development-admin",
                authentication_mode="development_admin",
                permissions=_ADMIN_PERMISSIONS,
            )
        return TrainingPrincipal(
            subject="anonymous-read-only",
            authentication_mode="read_only",
            permissions=_READ_ONLY_PERMISSIONS,
        )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")

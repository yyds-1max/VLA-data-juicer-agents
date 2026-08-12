#!/usr/bin/env python3
"""Load the fixed DataPilot Web deployment configuration as inert JSON.

The configuration is deliberately not a shell fragment.  This helper validates
the fixed per-user path and then execs the requested command with allowlisted
values added to its environment.  Explicit values already present in the
caller's environment take precedence.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


CONFIG_RELATIVE_PATH = Path(".config/vla-data-juicer-agents/run-web.json")
MAX_CONFIG_BYTES = 64 * 1024
LOADED_ENV = "VLA_RUN_WEB_CONFIG_LOADED"

ALLOWED_KEYS = frozenset(
    {
        "AGENT_DATA_ENV_SETUP",
        "AGENT_DATA_PYTHON",
        "FRONTEND_DIST",
        "HOST",
        "LOG_DIR",
        "LOG_FILE",
        "PID_FILE",
        "PORT",
        "SKIP_FRONTEND_BUILD",
        "STATE_DIR",
        "VLA_ANNOTATION_MINIMUM_FREE_BYTES",
        "VLA_ANNOTATION_RUNTIME_TIMEOUT_SECONDS",
        "VLA_ANNOTATION_WORK_ROOT",
        "VLA_BWRAP",
        "VLA_DATA_AGENT_WEB_WORKING_DIR",
        "VLA_DPKG_QUERY",
        "VLA_FRONTEND_NODE_BIN_DIR",
        "VLA_LEGACY_CLIP_DATA_ROOT",
        "VLA_NAVIGATION_ODOM_V1_MANIFEST",
        "VLA_NAVIGATION_ODOM_V1_SOURCE",
        "VLA_NAVIGATION_WRITER_LOCK_PATH",
        "VLA_RUNTIME_DEPENDENCY_SUMMARY",
        "VLA_TRACKING_BINARY_DATA_ROOT",
        "VLA_TRACKING_LEGACY_DATA_ROOT",
        "VLA_TRAINING_DB_PATH",
        "VLA_TRAINING_CENTER_BASE_URL",
        "VLA_TRAINING_CENTER_CA_CERT_PATH",
        "VLA_TRAINING_DEV_ADMIN",
        "VLA_TRAINING_FAKE_TICK_SECONDS",
        "VLA_TRAINING_SIMULATION_ENABLED",
        "VLA_VLADATASETS_ROOT",
        "VLA_XVFB",
        "VLA_XVFB_DEB",
        "VLA_XVFB_RUN",
        "WORKING_DIR",
    }
)


class DeploymentConfigError(RuntimeError):
    """The fixed deployment configuration is unavailable or unsafe."""


class _DuplicateKeyError(ValueError):
    pass


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def fixed_config_path() -> Path:
    """Return the one supported configuration path.

    There is intentionally no CLI flag or application-specific environment
    variable for selecting another file.
    """

    return Path.home() / CONFIG_RELATIVE_PATH


def _validate_owner_directory(path: Path, *, exact_mode: int | None) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DeploymentConfigError(
            "deployment configuration directory is unavailable"
        ) from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path.absolute()
        or metadata.st_uid != os.geteuid()
        or mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise DeploymentConfigError(
            "deployment configuration directory is unsafe"
        )


def _read_config_file(path: Path) -> bytes:
    _validate_owner_directory(path.parent.parent, exact_mode=None)
    _validate_owner_directory(path.parent, exact_mode=0o700)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        before_path = path.lstat()
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before_path.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before_path.st_dev != before.st_dev
            or before_path.st_ino != before.st_ino
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size > MAX_CONFIG_BYTES
        ):
            raise DeploymentConfigError(
                "deployment configuration file is unsafe"
            )
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            after_path = path.lstat()
        except OSError as exc:
            raise DeploymentConfigError(
                "deployment configuration changed while reading"
            ) from exc
        if (
            len(payload) > MAX_CONFIG_BYTES
            or len(payload) != before.st_size
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or after_path.st_dev != after.st_dev
            or after_path.st_ino != after.st_ino
            or stat.S_ISLNK(after_path.st_mode)
        ):
            raise DeploymentConfigError(
                "deployment configuration changed while reading"
            )
        return payload
    except DeploymentConfigError:
        raise
    except OSError as exc:
        raise DeploymentConfigError(
            "deployment configuration file is unavailable"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_fixed_config() -> dict[str, str]:
    """Load and validate the fixed configuration, or return empty if absent."""

    path = fixed_config_path()
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise DeploymentConfigError(
            "deployment configuration file is unavailable"
        ) from exc
    payload = _read_config_file(path)
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_without_duplicate_keys,
        )
    except _DuplicateKeyError as exc:
        raise DeploymentConfigError(
            f"deployment configuration contains duplicate key: {exc}"
        ) from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentConfigError(
            "deployment configuration is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise DeploymentConfigError(
            "deployment configuration must be a JSON object"
        )
    unknown = sorted(set(value) - ALLOWED_KEYS)
    if unknown:
        raise DeploymentConfigError(
            "deployment configuration contains unsupported key(s): "
            + ", ".join(unknown)
        )
    result: dict[str, str] = {}
    for key, raw_value in value.items():
        if not isinstance(raw_value, str) or not raw_value or "\x00" in raw_value:
            raise DeploymentConfigError(
                f"deployment configuration value for {key} must be a non-empty string"
            )
        result[key] = raw_value
    return result


def deployment_environment(
    config: dict[str, str],
    inherited: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge config below explicit caller values and mark it as loaded."""

    environment = dict(os.environ if inherited is None else inherited)
    for key, value in config.items():
        environment.setdefault(key, value)
    environment[LOADED_ENV] = "1"
    return environment


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        print("run_web config error: a command is required", file=sys.stderr)
        return 2
    try:
        config = load_fixed_config()
        environment = deployment_environment(config)
        os.execvpe(arguments[0], arguments, environment)
    except DeploymentConfigError as exc:
        print(f"run_web config error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("run_web config error: command could not be executed", file=sys.stderr)
        return 127
    return 127


if __name__ == "__main__":
    raise SystemExit(main())

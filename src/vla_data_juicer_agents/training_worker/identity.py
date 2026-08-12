from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from typing import Any
from uuid import uuid4


IDENTITY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """Persistent node identity.

    ``credential`` is local authentication material.  It must never be added
    to logs, health payloads, or resource payloads.
    """

    worker_id: str
    credential: str
    created_at: str
    schema_version: int = IDENTITY_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "worker_id": self.worker_id,
            "created_at": self.created_at,
        }


def store_worker_token(state_dir: Path, worker_token: str) -> Path:
    """Atomically persist a center-issued bearer credential with mode 0600."""

    if not worker_token.startswith("worker_") or len(worker_token) < 40:
        raise ValueError("worker token has an invalid format")
    state_dir = Path(state_dir).expanduser()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_path = state_dir / "worker-token"
    if token_path.is_symlink():
        raise RuntimeError("worker token path must not be a symbolic link")
    temporary_path = state_dir / f".worker-token.{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(temporary_path, flags, 0o600)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            stream.write(worker_token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, token_path)
        token_path.chmod(0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return token_path


def load_worker_token(state_dir: Path) -> str | None:
    token_path = Path(state_dir).expanduser() / "worker-token"
    if not token_path.exists():
        return None
    if token_path.is_symlink():
        raise RuntimeError("worker token path must not be a symbolic link")
    try:
        token_path.chmod(0o600)
        worker_token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("worker token is unreadable") from exc
    if not worker_token.startswith("worker_") or len(worker_token) < 40:
        raise RuntimeError("worker token has an invalid format")
    return worker_token


def load_or_create_identity(state_dir: Path) -> WorkerIdentity:
    """Load a stable identity or atomically create one with restrictive modes."""

    state_dir = Path(state_dir).expanduser()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except OSError:
        # Some mounted filesystems do not implement POSIX permissions.  The
        # identity file is still opened with 0600 below.
        pass

    identity_path = state_dir / "identity.json"
    if identity_path.is_symlink():
        raise RuntimeError("worker identity path must not be a symbolic link")

    if identity_path.exists():
        return _load_identity(identity_path)

    identity = WorkerIdentity(
        worker_id=f"worker-{uuid4().hex}",
        credential=secrets.token_urlsafe(32),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    payload = json.dumps(_identity_payload(identity), sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        file_descriptor = os.open(identity_path, flags, 0o600)
    except FileExistsError:
        # Another worker process won the race.  It is safe to use the identity
        # it created after validating its contents.
        return _load_identity(identity_path)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        identity_path.chmod(0o600)
    except OSError:
        pass
    return identity


def _load_identity(path: Path) -> WorkerIdentity:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("worker identity is unreadable or invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("worker identity must be a JSON object")
    schema_version = payload.get("schema_version")
    worker_id = payload.get("worker_id")
    credential = payload.get("credential")
    created_at = payload.get("created_at")
    if schema_version != IDENTITY_SCHEMA_VERSION:
        raise RuntimeError("worker identity schema version is unsupported")
    if not all(isinstance(value, str) and value for value in (worker_id, credential, created_at)):
        raise RuntimeError("worker identity is missing required fields")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return WorkerIdentity(
        worker_id=worker_id,
        credential=credential,
        created_at=created_at,
        schema_version=schema_version,
    )


def _identity_payload(identity: WorkerIdentity) -> dict[str, Any]:
    return {
        **identity.public_payload(),
        "credential": identity.credential,
    }

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

from .errors import (
    TrainingConflictError,
    TrainingForbiddenError,
    TrainingNotFoundError,
)
from .migrations import apply_training_migrations
from .models import RunStatus, TERMINAL_RUN_STATUSES, TrainingNodeStatus


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_ref(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class TrainingStore:
    """SQLite repository for simulation training state.

    Every compound transition uses ``BEGIN IMMEDIATE`` so GPU/port allocation,
    state changes and their public event are committed atomically.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connection() as connection:
            apply_training_migrations(connection, applied_at=now_iso())

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _effective_node_status(
        row: sqlite3.Row, *, offline_after_seconds: float
    ) -> str:
        status = str(row["status"])
        if status not in {
            TrainingNodeStatus.ONLINE.value,
            TrainingNodeStatus.DEGRADED.value,
        }:
            return status
        heartbeat_at = row["last_heartbeat_at"]
        if heartbeat_at is None:
            return TrainingNodeStatus.OFFLINE.value
        try:
            age = datetime.now(UTC) - datetime.fromisoformat(str(heartbeat_at))
        except ValueError:
            return TrainingNodeStatus.OFFLINE.value
        if age.total_seconds() > offline_after_seconds:
            return TrainingNodeStatus.OFFLINE.value
        return status

    @classmethod
    def _safe_node(
        cls, row: sqlite3.Row, *, offline_after_seconds: float = 45.0
    ) -> dict[str, Any]:
        capabilities = (
            json.loads(row["capabilities_json"])
            if row["capabilities_json"] is not None
            else None
        )
        return {
            "node_ref": row["node_ref"],
            "name": row["name"],
            "description": row["description"],
            "address": row["address"],
            "ssh_port": row["ssh_port"],
            "ssh_username": row["ssh_username"] or None,
            "host_key_algorithm": row["host_key_algorithm"],
            "host_public_key": row["host_public_key"],
            "host_key_fingerprint": row["host_key_fingerprint"],
            "deployment_status": row["deployment_status"],
            "deployment_message": row["deployment_message"],
            "deployment_started_at": row["deployment_started_at"],
            "deployment_finished_at": row["deployment_finished_at"],
            "installed_worker_version": row["installed_worker_version"],
            "status": cls._effective_node_status(
                row, offline_after_seconds=offline_after_seconds
            ),
            "state_revision": row["state_revision"],
            "heartbeat_revision": row["heartbeat_revision"],
            "enrolled_at": row["enrolled_at"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "last_seen_at": row["last_heartbeat_at"],
            "worker_instance_id": row["worker_instance_id"],
            "worker_version": row["worker_version"],
            "protocol_version": row["protocol_version"],
            "health_message": row["health_message"],
            "capabilities": capabilities,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_node(self, data: dict[str, Any], actor: str) -> dict[str, Any]:
        timestamp = now_iso()
        node_ref = new_ref("node")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO training_nodes(
                node_ref,name,description,address,ssh_port,ssh_username,status,
                state_revision,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'pending_enrollment',1,?,?)""",
                (
                    node_ref,
                    data["name"],
                    data.get("description") or "",
                    data["address"],
                    data.get("ssh_port", 22),
                    data.get("ssh_username") or "",
                    timestamp,
                    timestamp,
                ),
            )
            self._audit(db, actor, "node.created", node_ref, {}, timestamp)
        return self.get_node(node_ref)

    def list_nodes(self, *, offline_after_seconds: float = 45.0) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM training_nodes ORDER BY id DESC").fetchall()
        return [
            self._safe_node(row, offline_after_seconds=offline_after_seconds)
            for row in rows
        ]

    def get_node(
        self, node_ref: str, *, offline_after_seconds: float = 45.0
    ) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
        if row is None:
            raise TrainingNotFoundError(
                "training_node_not_found", "Training node was not found."
            )
        return self._safe_node(row, offline_after_seconds=offline_after_seconds)

    def update_node(
        self,
        node_ref: str,
        expected_revision: int,
        data: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError(
                    "training_node_not_found", "Training node was not found."
                )
            if int(row["state_revision"]) != expected_revision:
                raise TrainingConflictError(
                    "training_node_revision_conflict",
                    "The training node changed.",
                    current=self._safe_node(row),
                )
            merged = {
                "name": row["name"],
                "description": row["description"],
                "address": row["address"],
                "ssh_port": row["ssh_port"],
                "ssh_username": row["ssh_username"],
                "status": row["status"],
            }
            merged.update(
                {key: value for key, value in data.items() if value is not None}
            )
            desired_state = merged.pop("desired_state", None)
            if desired_state is not None:
                merged["status"] = desired_state
            worker_token_sha256 = row["worker_token_sha256"]
            if merged["status"] == TrainingNodeStatus.DISABLED.value:
                worker_token_sha256 = None
                db.execute(
                    """UPDATE training_node_enrollment_tokens
                    SET consumed_at=? WHERE node_id=? AND consumed_at IS NULL""",
                    (timestamp, row["id"]),
                )
            db.execute(
                """UPDATE training_nodes SET
                name=?,description=?,address=?,ssh_port=?,ssh_username=?,status=?,
                worker_token_sha256=?,state_revision=state_revision+1,updated_at=?
                WHERE id=?""",
                (
                    merged["name"],
                    merged["description"],
                    merged["address"],
                    merged["ssh_port"],
                    merged["ssh_username"],
                    merged["status"],
                    worker_token_sha256,
                    timestamp,
                    row["id"],
                ),
            )
            self._audit(
                db,
                actor,
                "node.updated",
                node_ref,
                {"status": merged["status"]},
                timestamp,
            )
        return self.get_node(node_ref)

    def delete_node(
        self, node_ref: str, expected_revision: int, actor: str
    ) -> None:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError(
                    "training_node_not_found", "Training node was not found."
                )
            if int(row["state_revision"]) != expected_revision:
                raise TrainingConflictError(
                    "training_node_revision_conflict",
                    "The training node changed.",
                    current=self._safe_node(row),
                )
            snapshot_exists = db.execute(
                """SELECT 1 FROM training_node_resource_snapshots
                WHERE node_id=? LIMIT 1""",
                (row["id"],),
            ).fetchone()
            if row["enrolled_at"] is not None or snapshot_exists is not None:
                raise TrainingConflictError(
                    "training_node_has_history",
                    "An enrolled node cannot be deleted; disable it instead.",
                    current=self._safe_node(row),
                )
            db.execute(
                """UPDATE model_verification_requests SET
                status='failed',
                result_json=?,worker_instance_id=NULL,lease_expires_at=NULL,
                finished_at=?,updated_at=?
                WHERE node_id=? AND status IN ('queued','running')""",
                (
                    canonical_json(
                        {
                            "checks": [
                                {
                                    "code": "training_node_deleted",
                                    "label": "训练节点",
                                    "status": "failed",
                                    "detail": "训练节点已删除，配置验证未完成。",
                                }
                            ]
                        }
                    ),
                    timestamp,
                    timestamp,
                    row["id"],
                ),
            )
            self._audit(db, actor, "node.deleted", node_ref, {}, timestamp)
            db.execute("DELETE FROM training_nodes WHERE id=?", (row["id"],))

    def create_enrollment_token(
        self,
        node_ref: str,
        expected_revision: int,
        expires_in_seconds: int,
        actor: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
        ).isoformat(timespec="milliseconds")
        token = f"enroll_{secrets.token_urlsafe(32)}"
        token_ref = new_ref("enrollment")
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError(
                    "training_node_not_found", "Training node was not found."
                )
            if int(row["state_revision"]) != expected_revision:
                raise TrainingConflictError(
                    "training_node_revision_conflict",
                    "The training node changed.",
                    current=self._safe_node(row),
                )
            if row["status"] == TrainingNodeStatus.DISABLED.value:
                raise TrainingConflictError(
                    "training_node_disabled",
                    "A disabled training node cannot be enrolled.",
                    current=self._safe_node(row),
                )
            db.execute(
                """UPDATE training_node_enrollment_tokens SET consumed_at=?
                WHERE node_id=? AND consumed_at IS NULL""",
                (timestamp, row["id"]),
            )
            db.execute(
                """INSERT INTO training_node_enrollment_tokens(
                token_ref,node_id,token_sha256,expires_at,created_by,created_at)
                VALUES(?,?,?,?,?,?)""",
                (
                    token_ref,
                    row["id"],
                    self._token_digest(token),
                    expires_at,
                    actor,
                    timestamp,
                ),
            )
            db.execute(
                """UPDATE training_nodes SET state_revision=state_revision+1,
                updated_at=? WHERE id=?""",
                (timestamp, row["id"]),
            )
            self._audit(
                db,
                actor,
                "node.enrollment_token_created",
                node_ref,
                {"expires_at": expires_at},
                timestamp,
            )
        return {
            "enrollment_token": token,
            "expires_at": expires_at,
            "node": self.get_node(node_ref),
        }

    def begin_node_deployment(
        self,
        node_ref: str,
        expected_revision: int,
        *,
        host_key_algorithm: str,
        host_public_key: str,
        host_key_fingerprint: str,
        actor: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError(
                    "training_node_not_found", "Training node was not found."
                )
            if int(row["state_revision"]) != expected_revision:
                raise TrainingConflictError(
                    "training_node_revision_conflict",
                    "The training node changed.",
                    current=self._safe_node(row),
                )
            if row["status"] == TrainingNodeStatus.DISABLED.value:
                raise TrainingConflictError(
                    "training_node_disabled",
                    "A disabled training node cannot deploy a Worker.",
                    current=self._safe_node(row),
                )
            if row["deployment_status"] == "deploying":
                raise TrainingConflictError(
                    "training_node_deployment_in_progress",
                    "A Worker deployment is already in progress.",
                    current=self._safe_node(row),
                )
            db.execute(
                """UPDATE training_nodes SET
                host_key_algorithm=?,host_public_key=?,host_key_fingerprint=?,
                deployment_status='deploying',deployment_message=NULL,
                deployment_started_at=?,deployment_finished_at=NULL,
                state_revision=state_revision+1,updated_at=? WHERE id=?""",
                (
                    host_key_algorithm,
                    host_public_key,
                    host_key_fingerprint,
                    timestamp,
                    timestamp,
                    row["id"],
                ),
            )
            self._audit(
                db,
                actor,
                "node.deployment_started",
                node_ref,
                {"host_key_fingerprint": host_key_fingerprint},
                timestamp,
            )
        return self.get_node(node_ref)

    def finish_node_deployment(
        self,
        node_ref: str,
        *,
        succeeded: bool,
        message: str,
        worker_version: str | None,
        actor: str,
        ssh_username: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        safe_message = message[:1000]
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError(
                    "training_node_not_found", "Training node was not found."
                )
            db.execute(
                """UPDATE training_nodes SET deployment_status=?,deployment_message=?,
                deployment_finished_at=?,installed_worker_version=COALESCE(?,installed_worker_version),
                ssh_username=CASE WHEN ? THEN ? ELSE ssh_username END,
                state_revision=state_revision+1,updated_at=? WHERE id=?""",
                (
                    "succeeded" if succeeded else "failed",
                    safe_message,
                    timestamp,
                    worker_version,
                    1 if succeeded and ssh_username is not None else 0,
                    ssh_username,
                    timestamp,
                    row["id"],
                ),
            )
            self._audit(
                db,
                actor,
                "node.deployment_succeeded" if succeeded else "node.deployment_failed",
                node_ref,
                {"message": safe_message, "worker_version": worker_version},
                timestamp,
            )
        return self.get_node(node_ref)

    def begin_node_worker_removal(
        self,
        node_ref: str,
        expected_revision: int,
        *,
        actor: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError(
                    "training_node_not_found", "Training node was not found."
                )
            if int(row["state_revision"]) != expected_revision:
                raise TrainingConflictError(
                    "training_node_revision_conflict",
                    "The training node changed.",
                    current=self._safe_node(row),
                )
            if row["deployment_status"] == "deploying":
                raise TrainingConflictError(
                    "training_node_deployment_in_progress",
                    "A Worker operation is already in progress.",
                    current=self._safe_node(row),
                )
            if not any(
                row[field] is not None
                for field in (
                    "installed_worker_version",
                    "worker_instance_id",
                    "enrolled_at",
                )
            ):
                raise TrainingConflictError(
                    "training_node_worker_not_installed",
                    "This training node has no installed Worker.",
                    current=self._safe_node(row),
                )
            db.execute(
                """UPDATE training_node_enrollment_tokens SET consumed_at=?
                WHERE node_id=? AND consumed_at IS NULL""",
                (timestamp, row["id"]),
            )
            db.execute(
                """UPDATE training_nodes SET
                status='disabled',worker_token_sha256=NULL,
                deployment_status='deploying',
                deployment_message='Training Worker removal is in progress.',
                deployment_started_at=?,deployment_finished_at=NULL,
                state_revision=state_revision+1,updated_at=? WHERE id=?""",
                (timestamp, timestamp, row["id"]),
            )
            self._audit(
                db,
                actor,
                "node.worker_removal_started",
                node_ref,
                {},
                timestamp,
            )
        return self.get_node(node_ref)

    def finish_node_worker_removal(
        self,
        node_ref: str,
        *,
        succeeded: bool,
        message: str,
        actor: str,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        safe_message = message[:1000]
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError(
                    "training_node_not_found", "Training node was not found."
                )
            if succeeded:
                db.execute(
                    """UPDATE training_nodes SET
                    status='pending_enrollment',deployment_status='not_started',
                    deployment_message=?,deployment_finished_at=?,
                    installed_worker_version=NULL,enrolled_at=NULL,
                    last_heartbeat_at=NULL,worker_instance_id=NULL,
                    worker_version=NULL,protocol_version=NULL,
                    worker_token_sha256=NULL,health_message=NULL,
                    capabilities_json=NULL,
                    state_revision=state_revision+1,
                    updated_at=? WHERE id=?""",
                    (
                        safe_message,
                        timestamp,
                        timestamp,
                        row["id"],
                    ),
                )
                db.execute(
                    "DELETE FROM training_node_resource_snapshots WHERE node_id=?",
                    (row["id"],),
                )
            else:
                db.execute(
                    """UPDATE training_nodes SET
                    status='repair_required',deployment_status='failed',
                    deployment_message=?,deployment_finished_at=?,
                    worker_token_sha256=NULL,state_revision=state_revision+1,
                    updated_at=? WHERE id=?""",
                    (safe_message, timestamp, timestamp, row["id"]),
                )
            self._audit(
                db,
                actor,
                (
                    "node.worker_removed"
                    if succeeded
                    else "node.worker_removal_failed"
                ),
                node_ref,
                {"message": safe_message},
                timestamp,
            )
        return self.get_node(node_ref)

    def invalidate_node_enrollment_tokens(self, node_ref: str) -> None:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute(
                "SELECT id FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if row is None:
                return
            db.execute(
                """UPDATE training_node_enrollment_tokens SET consumed_at=?
                WHERE node_id=? AND consumed_at IS NULL""",
                (timestamp, row["id"]),
            )

    def enroll_node(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = now_iso()
        worker_token = f"worker_{secrets.token_urlsafe(32)}"
        digest = self._token_digest(data["enrollment_token"])
        with self.transaction() as db:
            token_row = db.execute(
                """SELECT t.*,n.node_ref,n.status AS node_status
                FROM training_node_enrollment_tokens t
                JOIN training_nodes n ON n.id=t.node_id
                WHERE t.token_sha256=?""",
                (digest,),
            ).fetchone()
            if (
                token_row is None
                or token_row["consumed_at"] is not None
                or str(token_row["expires_at"]) <= timestamp
            ):
                raise TrainingForbiddenError(
                    "invalid_enrollment_token",
                    "The enrollment token is invalid, expired, or already used.",
                )
            if token_row["node_status"] == TrainingNodeStatus.DISABLED.value:
                raise TrainingForbiddenError(
                    "training_node_disabled", "The training node is disabled."
                )
            db.execute(
                """UPDATE training_node_enrollment_tokens
                SET consumed_at=? WHERE id=? AND consumed_at IS NULL""",
                (timestamp, token_row["id"]),
            )
            db.execute(
                """UPDATE training_nodes SET
                status='online',heartbeat_revision=heartbeat_revision+1,
                enrolled_at=COALESCE(enrolled_at,?),last_heartbeat_at=?,
                worker_instance_id=?,worker_version=?,protocol_version=?,
                worker_token_sha256=?,health_message=NULL,capabilities_json=?,
                updated_at=? WHERE id=?""",
                (
                    timestamp,
                    timestamp,
                    data["worker_instance_id"],
                    data["worker_version"],
                    data["protocol_version"],
                    self._token_digest(worker_token),
                    canonical_json(data["capabilities"]),
                    timestamp,
                    token_row["node_id"],
                ),
            )
            self._audit(
                db,
                f"worker:{data['worker_instance_id']}",
                "node.enrolled",
                token_row["node_ref"],
                {
                    "worker_version": data["worker_version"],
                    "protocol_version": data["protocol_version"],
                },
                timestamp,
            )
        return {
            "node": self.get_node(token_row["node_ref"]),
            "worker_token": worker_token,
        }

    def record_node_heartbeat(
        self, node_ref: str, worker_token: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        timestamp = now_iso()
        digest = self._token_digest(worker_token)
        status = {
            "healthy": TrainingNodeStatus.ONLINE.value,
            "degraded": TrainingNodeStatus.DEGRADED.value,
            "repair_required": TrainingNodeStatus.REPAIR_REQUIRED.value,
        }[data["health"]]
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if (
                row is None
                or row["worker_token_sha256"] is None
                or not secrets.compare_digest(str(row["worker_token_sha256"]), digest)
                or row["worker_instance_id"] != data["worker_instance_id"]
            ):
                raise TrainingForbiddenError(
                    "worker_authentication_failed",
                    "The Training Worker credential is invalid.",
                )
            if row["status"] == TrainingNodeStatus.DISABLED.value:
                raise TrainingForbiddenError(
                    "training_node_disabled", "The training node is disabled."
                )
            capabilities_json = row["capabilities_json"]
            if data.get("capabilities") is not None:
                capabilities_json = canonical_json(data["capabilities"])
            db.execute(
                """UPDATE training_nodes SET
                status=?,last_heartbeat_at=?,
                worker_version=?,protocol_version=?,health_message=?,
                capabilities_json=?,heartbeat_revision=heartbeat_revision+1
                WHERE id=?""",
                (
                    status,
                    timestamp,
                    data["worker_version"],
                    data["protocol_version"],
                    data.get("health_message"),
                    capabilities_json,
                    row["id"],
                ),
            )
            db.execute(
                """INSERT INTO training_node_resource_snapshots(
                node_id,captured_at,resources_json) VALUES(?,?,?)""",
                (row["id"], timestamp, canonical_json(data["resources"])),
            )
            db.execute(
                """DELETE FROM training_node_resource_snapshots
                WHERE node_id=? AND id NOT IN (
                  SELECT id FROM training_node_resource_snapshots
                  WHERE node_id=? ORDER BY id DESC LIMIT 1000
                )""",
                (row["id"], row["id"]),
            )
        return self.get_node(node_ref)

    def claim_model_verification(
        self, node_ref: str, worker_instance_id: str
    ) -> dict[str, Any] | None:
        """Claim one bounded, read-only model verification command for a Worker."""
        timestamp = now_iso()
        lease_expires_at = (
            datetime.now(UTC) + timedelta(seconds=90)
        ).isoformat(timespec="milliseconds")
        with self.transaction() as db:
            node = db.execute(
                "SELECT id,worker_instance_id,status FROM training_nodes WHERE node_ref=?",
                (node_ref,),
            ).fetchone()
            if (
                node is None
                or node["worker_instance_id"] != worker_instance_id
                or node["status"] not in {
                    TrainingNodeStatus.ONLINE.value,
                    TrainingNodeStatus.DEGRADED.value,
                }
            ):
                return None
            db.execute(
                """UPDATE model_verification_requests
                SET status='queued',worker_instance_id=NULL,lease_expires_at=NULL,
                    started_at=NULL,updated_at=?
                WHERE node_id=? AND status='running' AND lease_expires_at<?""",
                (timestamp, node["id"], timestamp),
            )
            command = db.execute(
                """SELECT id,verification_ref,request_json
                FROM model_verification_requests
                WHERE node_id=? AND status='queued' ORDER BY id LIMIT 1""",
                (node["id"],),
            ).fetchone()
            if command is None:
                return None
            changed = db.execute(
                """UPDATE model_verification_requests
                SET status='running',worker_instance_id=?,lease_expires_at=?,
                    started_at=?,updated_at=?
                WHERE id=? AND status='queued'""",
                (
                    worker_instance_id,
                    lease_expires_at,
                    timestamp,
                    timestamp,
                    command["id"],
                ),
            ).rowcount
            if changed != 1:
                return None
            return {
                "command_ref": command["verification_ref"],
                "kind": "verify_model_configuration",
                "payload": json.loads(command["request_json"]),
            }

    def request_model_verification(
        self, family_ref: str, expected_revision: int, actor: str
    ) -> dict[str, Any]:
        timestamp = now_iso()
        verification_ref = new_ref("verify")
        with self.transaction() as db:
            model = db.execute(
                """SELECT model.* FROM registered_models AS model
                JOIN model_families AS family ON family.current_model_id=model.id
                WHERE family.family_ref=?""",
                (family_ref,),
            ).fetchone()
            if model is None:
                raise TrainingNotFoundError(
                    "model_not_found", "Training model was not found."
                )
            if int(model["current_revision"]) != expected_revision:
                raise TrainingConflictError(
                    "model_configuration_edit_conflict",
                    "The model configuration was edited by another request.",
                    current={"edit_revision": model["current_revision"]},
                )
            if model["status"] == "disabled":
                raise TrainingConflictError(
                    "model_family_disabled",
                    "Disabled model families cannot be verified.",
                )
            existing = db.execute(
                """SELECT verification_ref FROM model_verification_requests
                WHERE model_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1""",
                (model["id"],),
            ).fetchone()
            if existing is not None:
                raise TrainingConflictError(
                    "model_verification_in_progress",
                    "A verification request is already in progress for this model family configuration.",
                    current={"verification_ref": existing["verification_ref"]},
                )
            revision = db.execute(
                """SELECT * FROM model_revisions
                WHERE model_id=? AND revision_number=?""",
                (model["id"], expected_revision),
            ).fetchone()
            template = json.loads(revision["launch_template_json"])
            node = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?",
                (template["server_ref"],),
            ).fetchone()
            if node is None:
                raise TrainingConflictError(
                    "model_verification_requires_training_node",
                    "Model verification requires a registered Training Worker node.",
                )
            if self._effective_node_status(node, offline_after_seconds=45.0) != TrainingNodeStatus.ONLINE.value:
                raise TrainingConflictError(
                    "model_verification_node_unavailable",
                    "The selected Training Worker node must be online before verification.",
                )
            request = {
                "family_ref": family_ref,
                "working_directory": template["working_directory"],
                "executable": template["executable"],
                "entrypoint": template["entrypoint"],
                "output_root": template["output_root"],
                "runtime_environment": template.get(
                    "runtime_environment", {"kind": "system"}
                ),
            }
            db.execute(
                """INSERT INTO model_verification_requests(
                verification_ref,model_id,model_revision_id,node_id,node_ref_snapshot,status,
                request_json,created_at,updated_at)
                VALUES(?,?,?,?,?,'queued',?,?,?)""",
                (
                    verification_ref,
                    model["id"],
                    revision["id"],
                    node["id"],
                    node["node_ref"],
                    canonical_json(request),
                    timestamp,
                    timestamp,
                ),
            )
            self._audit(
                db,
                actor,
                "model.verification_requested",
                family_ref,
                {"verification_ref": verification_ref, "node_ref": node["node_ref"]},
                timestamp,
            )
        return self.get_model(family_ref, include_private=True)

    def finish_model_verification(
        self,
        node_ref: str,
        worker_token: str,
        command_ref: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = now_iso()
        digest = self._token_digest(worker_token)
        with self.transaction() as db:
            node = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
            if (
                node is None
                or node["worker_token_sha256"] is None
                or not secrets.compare_digest(str(node["worker_token_sha256"]), digest)
                or node["worker_instance_id"] != data["worker_instance_id"]
            ):
                raise TrainingForbiddenError(
                    "worker_authentication_failed",
                    "The Training Worker credential is invalid.",
                )
            command = db.execute(
                """SELECT verification.*,model.current_revision,model.status AS model_status
                FROM model_verification_requests AS verification
                JOIN registered_models AS model ON model.id=verification.model_id
                WHERE verification.verification_ref=? AND verification.node_id=?""",
                (command_ref, node["id"]),
            ).fetchone()
            if command is None:
                raise TrainingNotFoundError(
                    "model_verification_not_found",
                    "Model verification command was not found.",
                )
            if command["status"] != "running" or command["worker_instance_id"] != data["worker_instance_id"]:
                raise TrainingConflictError(
                    "model_verification_not_claimed",
                    "The model verification command is not claimed by this Worker.",
                )
            final_status = data["status"]
            result = {"checks": data["checks"]}
            db.execute(
                """UPDATE model_verification_requests SET
                status=?,result_json=?,lease_expires_at=NULL,finished_at=?,updated_at=?
                WHERE id=?""",
                (
                    final_status,
                    canonical_json(result),
                    timestamp,
                    timestamp,
                    command["id"],
                ),
            )
            current_revision_id = db.execute(
                """SELECT id FROM model_revisions
                WHERE model_id=? AND revision_number=?""",
                (command["model_id"], command["current_revision"]),
            ).fetchone()
            configuration_is_current = (
                current_revision_id is not None
                and int(current_revision_id["id"]) == int(command["model_revision_id"])
            )
            if configuration_is_current and command["model_status"] != "disabled":
                db.execute(
                    "UPDATE registered_models SET status=?,updated_at=? WHERE id=?",
                    (
                        "verified" if final_status == "succeeded" else "draft",
                        timestamp,
                        command["model_id"],
                    ),
                )
            self._audit(
                db,
                f"worker:{data['worker_instance_id']}",
                f"model.verification_{final_status}",
                command_ref,
                {"check_count": len(data["checks"])},
                timestamp,
            )
        return {
            "command_ref": command_ref,
            "status": final_status,
            "accepted_at": timestamp,
        }

    def get_node_resources(self, node_ref: str) -> dict[str, Any]:
        node = self.get_node(node_ref)
        with self.connection() as db:
            row = db.execute(
                """SELECT s.captured_at,s.resources_json
                FROM training_node_resource_snapshots s
                JOIN training_nodes n ON n.id=s.node_id
                WHERE n.node_ref=? ORDER BY s.id DESC LIMIT 1""",
                (node_ref,),
            ).fetchone()
        if row is None:
            return {
                "node_ref": node_ref,
                "captured_at": None,
                "stale": True,
                "resources": None,
            }
        captured_at = datetime.fromisoformat(str(row["captured_at"]))
        age_seconds = (datetime.now(UTC) - captured_at).total_seconds()
        return {
            "node_ref": node_ref,
            "captured_at": row["captured_at"],
            "stale": node["status"] not in {"online", "degraded"}
            or age_seconds > 90,
            "resources": json.loads(row["resources_json"]),
        }

    def create_model(self, data: dict[str, Any], actor: str) -> dict[str, Any]:
        timestamp = now_iso()
        family_ref, model_ref, revision_ref = (
            new_ref("family"), new_ref("model"), new_ref("mrev")
        )
        with self.transaction() as db:
            family_cursor = db.execute(
                """INSERT INTO model_families(family_ref,name,created_at,updated_at)
                VALUES(?,?,?,?)""",
                (family_ref, data["family_name"], timestamp, timestamp),
            )
            cursor = db.execute(
                """INSERT INTO registered_models(
                model_ref,name,description,status,current_revision,created_at,updated_at,
                family_id,version_number,version_description)
                VALUES(?,?,?,'draft',1,?,?,?,?,?)""",
                (
                    model_ref,
                    data["family_name"],
                    data.get("description") or "",
                    timestamp,
                    timestamp,
                    int(family_cursor.lastrowid),
                    1,
                    None,
                ),
            )
            model_id = int(cursor.lastrowid)
            self._insert_revision(db, model_id, revision_ref, 1, data, timestamp)
            db.execute(
                "UPDATE model_families SET current_model_id=? WHERE id=?",
                (model_id, int(family_cursor.lastrowid)),
            )
            self._audit(
                db, actor, "model.created", family_ref,
                {}, timestamp,
            )
        return self.get_model(family_ref, include_private=True)

    def update_model(self, family_ref: str, expected_revision: int, data: dict[str, Any], actor: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute(
                """SELECT model.* FROM registered_models AS model
                JOIN model_families AS family ON family.current_model_id=model.id
                WHERE family.family_ref=?""",
                (family_ref,),
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError("model_not_found", "Training model was not found.")
            if row["status"] == "disabled":
                raise TrainingConflictError("model_not_editable", "Disabled models cannot be edited.")
            if row["current_revision"] != expected_revision:
                raise TrainingConflictError("model_configuration_edit_conflict", "The model configuration was edited by another request.", current={"edit_revision": row["current_revision"]})
            revision = expected_revision + 1
            revision_ref = new_ref("mrev")
            db.execute(
                """UPDATE registered_models SET description=?,version_description=NULL,
                status='draft',current_revision=?,updated_at=? WHERE id=?""",
                (
                    data.get("description") or row["description"] or "",
                    revision,
                    timestamp,
                    row["id"],
                ),
            )
            self._insert_revision(db, row["id"], revision_ref, revision, data, timestamp)
            db.execute(
                "UPDATE model_families SET updated_at=? WHERE family_ref=?",
                (timestamp, family_ref),
            )
            self._audit(db, actor, "model.updated", family_ref, {"edit_revision": revision}, timestamp)
        return self.get_model(family_ref, include_private=True)

    def _insert_revision(self, db: sqlite3.Connection, model_id: int, revision_ref: str, revision: int, data: dict[str, Any], timestamp: str) -> None:
        template = data["launch_template"]
        db.execute(
            """INSERT INTO model_revisions(revision_ref,model_id,revision_number,
            working_directory,entrypoint,fixed_argv_json,output_template,
            parameter_definitions_json,launch_template_json,created_at,
            data_access_mode)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (revision_ref, model_id, revision, template["working_directory"], template["entrypoint"], canonical_json(template.get("fixed_argv", [])), f"{template['output_root'].rstrip('/')}/{{run_ref}}", canonical_json(data["parameter_definitions"]), canonical_json(template), timestamp, data.get("data_access_mode", "self_managed")),
        )

    def list_models(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(self._MODEL_SELECT + " ORDER BY family.id DESC").fetchall()
        return [self._safe_model(row) for row in rows]

    def get_model(self, family_ref: str, *, revision: int | None = None, include_private: bool = False) -> dict[str, Any]:
        with self.connection() as db:
            model = db.execute(
                self._MODEL_SELECT + " WHERE family.family_ref=?", (family_ref,)
            ).fetchone()
            if model is None:
                raise TrainingNotFoundError("model_not_found", "Training model was not found.")
            number = revision or int(model["current_revision"])
            rev = db.execute("SELECT * FROM model_revisions WHERE model_id=? AND revision_number=?", (model["id"], number)).fetchone()
        result = self._safe_model(model)
        result["parameter_definitions"] = json.loads(rev["parameter_definitions_json"])
        result["data_access_mode"] = rev["data_access_mode"]
        result["launch_template"] = json.loads(rev["launch_template_json"]) if include_private else {
            "domain": json.loads(rev["launch_template_json"])["domain"],
            "server_ref": json.loads(rev["launch_template_json"])["server_ref"],
        }
        if include_private:
            result["output_template"] = rev["output_template"]
        return result

    def get_model_record(self, family_ref: str, revision: int | None = None) -> dict[str, Any]:
        with self.connection() as db:
            model = db.execute(
                self._MODEL_SELECT + " WHERE family.family_ref=?",
                (family_ref,),
            ).fetchone()
            if model is None:
                raise TrainingNotFoundError(
                    "model_not_found", "Training model was not found."
                )
            number = revision or int(model["current_revision"])
            rev = db.execute(
                """SELECT * FROM model_revisions
                WHERE model_id=? AND revision_number=?""",
                (model["id"], number),
            ).fetchone()
            if rev is None:
                raise TrainingNotFoundError(
                    "model_revision_not_found",
                    "Training model configuration was not found.",
                )
        # ``number`` is captured from the same model row that selected the
        # configuration.  A concurrent edit may create a newer revision after
        # these reads, but it cannot turn this result into an old-definition /
        # new-revision hybrid; create_run will reject the now-stale revision.
        result = self._safe_model(model)
        result["parameter_definitions"] = json.loads(
            rev["parameter_definitions_json"]
        )
        result["launch_template"] = json.loads(rev["launch_template_json"])
        result["data_access_mode"] = rev["data_access_mode"]
        result["output_template"] = rev["output_template"]
        result["internal_revision"] = number
        result["revision_ref"] = rev["revision_ref"]
        result["model_ref"] = model["model_ref"]
        return result

    def find_create_run_by_idempotency(
        self, idempotency_key: str, request_payload: Any
    ) -> dict[str, Any] | None:
        """Return an earlier run before mutable model/resource preparation.

        The caller must pass the original public request payload, not a
        prepared object containing the model's current internal revision.  A
        second check remains in ``create_run`` to close the lookup/create race.
        """

        digest = payload_hash(request_payload)
        with self.connection() as db:
            idem = db.execute(
                """SELECT payload_sha256,response_ref FROM training_idempotency
                WHERE scope='create_run' AND idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if idem is None:
                return None
            if idem["payload_sha256"] != digest:
                raise TrainingConflictError(
                    "idempotency_conflict",
                    "Idempotency-Key was already used for another request.",
                )
            return self._get_run_in(db, idem["response_ref"])

    _MODEL_SELECT = """SELECT model.*,family.family_ref,family.name AS family_name,
      (SELECT COUNT(*) FROM model_versions AS version
       WHERE version.family_id=family.id) AS trained_version_count,
      verification.verification_ref AS verification_ref,
      verification.status AS verification_status,
      verification.result_json AS verification_result_json,
      verification.created_at AS verification_created_at,
      verification.finished_at AS verification_finished_at
      FROM registered_models AS model
      JOIN model_families AS family ON family.current_model_id=model.id
      LEFT JOIN model_verification_requests AS verification ON verification.id=(
        SELECT latest_verification.id
        FROM model_verification_requests AS latest_verification
        JOIN model_revisions AS latest_revision
          ON latest_revision.id=latest_verification.model_revision_id
        WHERE latest_verification.model_id=model.id
          AND latest_revision.revision_number=model.current_revision
        ORDER BY latest_verification.id DESC LIMIT 1
      )"""

    @staticmethod
    def _safe_model(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "family_ref": row["family_ref"],
            "family_name": row["family_name"],
            "status": row["status"],
            "edit_revision": int(row["current_revision"]),
            "trained_version_count": int(row["trained_version_count"]),
            "configuration_editable": row["status"] != "disabled",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if row["verification_ref"] is not None:
            verification: dict[str, Any] = {
                "verification_ref": row["verification_ref"],
                "status": row["verification_status"],
                "requested_at": row["verification_created_at"],
                "finished_at": row["verification_finished_at"],
            }
            if row["verification_result_json"] is not None:
                verification.update(json.loads(row["verification_result_json"]))
            result["verification"] = verification
        return result

    def find_available_port(self, server_ref: str, start: int = 29500, end: int = 29600, db: sqlite3.Connection | None = None) -> int:
        def find(connection: sqlite3.Connection) -> int:
            leased = {int(row[0]) for row in connection.execute("SELECT master_port FROM port_leases WHERE server_ref=?", (server_ref,))}
            for port in range(start, end + 1):
                if port not in leased:
                    return port
            raise TrainingConflictError("master_port_exhausted", "No simulation master port is available.")
        if db is not None:
            return find(db)
        with self.connection() as connection:
            return find(connection)

    def create_run(
        self,
        *,
        data: dict[str, Any],
        run_spec_builder: Any,
        idempotency_key: str,
        actor: str,
        idempotency_payload: Any | None = None,
    ) -> dict[str, Any]:
        digest = payload_hash(
            data if idempotency_payload is None else idempotency_payload
        )
        timestamp = now_iso()
        with self.transaction() as db:
            idem = db.execute("SELECT * FROM training_idempotency WHERE scope='create_run' AND idempotency_key=?", (idempotency_key,)).fetchone()
            if idem is not None:
                if idem["payload_sha256"] != digest:
                    raise TrainingConflictError("idempotency_conflict", "Idempotency-Key was already used for another request.")
                existing_ref = idem["response_ref"]
                return self._get_run_in(db, existing_ref)
            placeholders = ",".join("?" for _ in data["gpu_uuids"])
            conflicts = db.execute(f"SELECT gpu_uuid FROM gpu_leases WHERE gpu_uuid IN ({placeholders})", tuple(data["gpu_uuids"])).fetchall()
            if conflicts:
                raise TrainingConflictError("gpu_lease_conflict", "One or more GPUs are already leased.", current={"gpu_uuids": [row[0] for row in conflicts]})
            model = db.execute(
                """SELECT model.id,model.model_ref,model.current_revision,
                family.id AS family_id,family.family_ref,family.name AS family_name
                FROM registered_models AS model
                JOIN model_families AS family ON family.id=model.family_id
                WHERE model.model_ref=?""",
                (data["model_ref"],),
            ).fetchone()
            revision = db.execute(
                "SELECT id,model_id,revision_number FROM model_revisions WHERE revision_ref=?",
                (data["revision_ref"],),
            ).fetchone()
            if (
                model is None
                or revision is None
                or int(revision["model_id"]) != int(model["id"])
                or int(revision["revision_number"]) != int(model["current_revision"])
            ):
                raise TrainingConflictError(
                    "model_configuration_changed",
                    "The selected model configuration changed before the run was created.",
                )
            if data.get("dataset_splits") is not None:
                self._validate_dataset_splits_in(
                    db, data["server_ref"], data["dataset_splits"]
                )
            port = (
                self.find_available_port(data["server_ref"], db=db)
                if data.get("requires_master_port", True)
                else None
            )
            run_ref = new_ref("run")
            version_number = int(
                db.execute(
                    """SELECT COALESCE(MAX(version_number),0)+1
                    FROM model_versions WHERE family_id=?""",
                    (model["family_id"],),
                ).fetchone()[0]
            )
            version_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
            version_meta = {
                "version_ref": new_ref("version"),
                "version_number": version_number,
                "version_date": version_date,
                "version_label": f"v{version_number}-{version_date}",
                "snapshot_ref": (
                    new_ref("dataset_snapshot")
                    if data.get("dataset_splits") is not None
                    else None
                ),
            }
            spec = run_spec_builder(run_ref, port, version_meta)
            stage_specs = list(spec.get("stages") or [])
            if not 1 <= len(stage_specs) <= 10:
                raise TrainingConflictError(
                    "invalid_training_stage_count",
                    "A training run must contain between 1 and 10 stages.",
                )
            seed = int(hashlib.sha256(run_ref.encode()).hexdigest()[:8], 16)
            total_steps = sum(
                max(1, int(stage.get("total_steps", 20))) for stage in stage_specs
            )
            first_stage = stage_specs[0]
            parent_spec = {
                "model_ref": model["model_ref"],
                "family_ref": model["family_ref"],
                "family_name": model["family_name"],
                "version_ref": version_meta["version_ref"],
                "version_number": version_number,
                "version_date": version_date,
                "version_label": version_meta["version_label"],
            }
            cursor = db.execute(
                """INSERT INTO training_runs(run_ref,model_id,model_revision_id,mode,server_ref,gpu_uuids_json,parameters_json,run_spec_json,command_preview,status,state_revision,seed,total_steps,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'queued',1,?,?,?,?)""",
                (
                    run_ref,
                    model["id"],
                    revision["id"],
                    "simulation",
                    data["server_ref"],
                    canonical_json(data["gpu_uuids"]),
                    canonical_json(first_stage.get("parameters", {})),
                    canonical_json(parent_spec),
                    first_stage.get("command_preview", ""),
                    seed,
                    total_steps,
                    timestamp,
                    timestamp,
                ),
            )
            run_id = int(cursor.lastrowid)
            db.execute(
                """INSERT INTO model_versions(
                version_ref,family_id,run_id,version_number,version_date,
                version_label,created_at,description) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    version_meta["version_ref"],
                    model["family_id"],
                    run_id,
                    version_number,
                    version_date,
                    version_meta["version_label"],
                    timestamp,
                    data.get("version_description") or None,
                ),
            )
            if version_meta["snapshot_ref"] is not None:
                manifest = spec.get("dataset_manifest")
                if not isinstance(manifest, dict):
                    raise TrainingConflictError(
                        "dataset_manifest_missing",
                        "Managed training data requires a dataset manifest.",
                    )
                manifest_json = canonical_json(manifest)
                db.execute(
                    """INSERT INTO dataset_snapshots(
                    snapshot_ref,run_id,manifest_json,manifest_sha256,created_at)
                    VALUES(?,?,?,?,?)""",
                    (
                        version_meta["snapshot_ref"],
                        run_id,
                        manifest_json,
                        hashlib.sha256(manifest_json.encode()).hexdigest(),
                        timestamp,
                    ),
                )
            for index, stage in enumerate(stage_specs, start=1):
                private_spec = stage.get("private_spec") or {}
                stage_ref = stage.get("stage_ref") or new_ref("stage")
                output_directory = stage.get("output_directory") or private_spec.get(
                    "output_directory", ""
                )
                stage_input_source = stage.get("stage_input_source", "manual")
                db.execute(
                    """INSERT INTO training_stages(
                    stage_ref,run_id,stage_number,stage_name,stage_input_source,
                    parameters_json,run_spec_json,command_preview,output_directory,
                    status,total_steps,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
                    (
                        stage_ref,
                        run_id,
                        index,
                        stage.get("stage_name") or self._stage_name(index),
                        stage_input_source,
                        canonical_json(stage.get("parameters", {})),
                        canonical_json(private_spec),
                        stage.get("command_preview", ""),
                        output_directory,
                        max(1, int(stage.get("total_steps", 20))),
                        timestamp,
                        timestamp,
                    ),
                )
            db.executemany("INSERT INTO gpu_leases(gpu_uuid,run_id,acquired_at) VALUES(?,?,?)", [(gpu, run_id, timestamp) for gpu in data["gpu_uuids"]])
            if port is not None:
                db.execute("INSERT INTO port_leases(server_ref,master_port,run_id,acquired_at) VALUES(?,?,?,?)", (data["server_ref"], port, run_id, timestamp))
            db.execute("INSERT INTO training_idempotency(scope,idempotency_key,payload_sha256,response_ref,created_at) VALUES('create_run',?,?,?,?)", (idempotency_key, digest, run_ref, timestamp))
            self._log(db, run_id, "info", "Simulation workflow queued.", timestamp)
            self._event(db, "run.updated", run_ref, {"stage_count": len(stage_specs)}, timestamp)
            self._audit(
                db,
                actor,
                "run.created",
                run_ref,
                {
                    "mode": "simulation",
                    "stage_count": len(stage_specs),
                    "version_label": version_meta["version_label"],
                },
                timestamp,
            )
            return self._get_run_in(db, run_ref)

    def _validate_dataset_splits_in(
        self,
        db: sqlite3.Connection,
        node_ref: str,
        splits: dict[str, list[dict[str, Any]]],
    ) -> None:
        entries = [*(splits.get("train") or []), *(splits.get("test") or [])]
        if not entries:
            raise TrainingConflictError(
                "managed_dataset_training_set_required",
                "Managed training data requires at least one ready training date.",
            )
        refs = [str(item["replica_ref"]) for item in entries]
        placeholders = ",".join("?" for _ in refs)
        rows = db.execute(
            self._REPLICA_SELECT + f" WHERE replica.replica_ref IN ({placeholders})",
            tuple(refs),
        ).fetchall()
        by_ref = {str(row["replica_ref"]): row for row in rows}
        if set(by_ref) != set(refs):
            raise TrainingConflictError(
                "dataset_replica_changed",
                "A selected dataset date changed before the run was created.",
            )
        for entry in entries:
            row = by_ref[str(entry["replica_ref"])]
            if (
                row["status"] != "ready"
                or row["node_ref_snapshot"] != node_ref
                or row["release_ref"] != entry["release_ref"]
                or row["dataset_date"] != entry["dataset_date"]
                or row["local_root"] != entry["local_root"]
                or row["inventory_sha256"] != entry["inventory_sha256"]
            ):
                raise TrainingConflictError(
                    "dataset_replica_changed",
                    "A selected dataset date changed before the run was created.",
                )

    @staticmethod
    def _stage_name(number: int) -> str:
        names = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
        return f"第{names[number - 1]}阶段"

    def list_runs(self, *, status: str | None, after: str | None, limit: int) -> dict[str, Any]:
        clauses, values = [], []
        if status:
            clauses.append("run.status=?"); values.append(status)
        if after:
            try: cursor_id = int(after)
            except ValueError: cursor_id = 0
            clauses.append("run.id<?"); values.append(cursor_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as db:
            rows = db.execute(
                f"""{self._RUN_SELECT} {where} ORDER BY run.id DESC LIMIT ?""",
                (*values, limit + 1),
            ).fetchall()
            items = [
                self._safe_run(row, self._load_stages(db, int(row["id"])))
                for row in rows[:limit]
            ]
        return {"items": items, "next_after": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    def get_run(self, run_ref: str) -> dict[str, Any]:
        with self.connection() as db:
            return self._get_run_in(db, run_ref)

    def _get_run_in(self, db: sqlite3.Connection, run_ref: str) -> dict[str, Any]:
        row = db.execute(
            self._RUN_SELECT + " WHERE run.run_ref=?", (run_ref,)
        ).fetchone()
        if row is None:
            raise TrainingNotFoundError("run_not_found", "Training run was not found.")
        return self._safe_run(row, self._load_stages(db, int(row["id"])))

    _RUN_SELECT = """SELECT run.*,family.family_ref,family.name AS family_name,
      model.model_ref,version.version_ref,version.version_number,
      version.version_date,version.version_label,version.description AS version_description,
      snapshot.snapshot_ref,snapshot.manifest_json AS dataset_manifest_json,
      version_artifact.path AS version_model_path
      FROM training_runs AS run
      JOIN registered_models AS model ON model.id=run.model_id
      JOIN model_families AS family ON family.id=model.family_id
      JOIN model_versions AS version ON version.run_id=run.id
      LEFT JOIN dataset_snapshots AS snapshot ON snapshot.run_id=run.id
      LEFT JOIN training_artifacts AS version_artifact ON version_artifact.id=(
        SELECT latest_artifact.id FROM training_artifacts AS latest_artifact
        WHERE latest_artifact.version_id=version.id
          AND latest_artifact.kind='version_model'
        ORDER BY latest_artifact.id DESC LIMIT 1
      )"""

    @classmethod
    def _safe_run(
        cls, row: sqlite3.Row, stages: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        stage_items = stages or []
        current_stage = next(
            (
                stage
                for stage in stage_items
                if stage["status"] in {"preparing", "running"}
            ),
            None,
        )
        if current_stage is None and stage_items:
            current_stage = next(
                (
                    stage
                    for stage in stage_items
                    if stage["status"] in {"failed", "lost"}
                ),
                None,
            )
        if current_stage is None and stage_items:
            current_stage = next(
                (stage for stage in stage_items if stage["status"] == "pending"),
                None,
            )
        if current_stage is None and stage_items and row["status"] == "cancelled":
            current_stage = next(
                (stage for stage in stage_items if stage["status"] == "cancelled"),
                stage_items[-1],
            )
        if current_stage is None and stage_items:
            current_stage = stage_items[-1]
        first_stage = stage_items[0] if stage_items else None
        version_number = int(row["version_number"])
        return {
            "run_ref": row["run_ref"],
            "status": row["status"],
            "state_revision": row["state_revision"],
            "mode": row["mode"],
            "server_ref": row["server_ref"],
            "gpu_uuids": json.loads(row["gpu_uuids_json"]),
            "model_ref": row["model_ref"],
            "family_ref": row["family_ref"],
            "family_name": row["family_name"],
            "version_ref": row["version_ref"],
            "version_number": version_number,
            "version_date": row["version_date"],
            "version_label": row["version_label"],
            "version_description": row["version_description"],
            "dataset_snapshot": (
                {
                    "snapshot_ref": row["snapshot_ref"],
                    "manifest": json.loads(row["dataset_manifest_json"]),
                }
                if row["snapshot_ref"] is not None
                else None
            ),
            # Kept temporarily for older clients while the public API migrates.
            "model_version_number": version_number,
            "model_display_name": f"{row['family_name']} {row['version_label']}",
            "stage_count": len(stage_items),
            "current_stage_number": (
                current_stage["stage_number"] if current_stage is not None else None
            ),
            "stages": stage_items,
            "parameters": first_stage["parameters"] if first_stage else {},
            "run_spec": first_stage["run_spec"] if first_stage else None,
            "command_preview": first_stage["command_preview"] if first_stage else "",
            "current_step": row["current_step"],
            "total_steps": row["total_steps"],
            "progress": row["current_step"] / row["total_steps"] if row["total_steps"] else 0,
            "version_model": (
                {"kind": "version_model", "output_directory": row["version_model_path"]}
                if row["version_model_path"] is not None
                else None
            ),
            "failure": {"code": row["failure_code"], "message": row["failure_message"]} if row["failure_code"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    @classmethod
    def _load_stages(
        cls, db: sqlite3.Connection, run_id: int
    ) -> list[dict[str, Any]]:
        rows = db.execute(
            """SELECT stage.*,artifact.path AS artifact_path
            FROM training_stages AS stage
            LEFT JOIN training_artifacts AS artifact ON artifact.id=(
              SELECT latest.id FROM training_artifacts AS latest
              WHERE latest.stage_id=stage.id AND latest.kind='stage_output'
              ORDER BY latest.id DESC LIMIT 1
            )
            WHERE stage.run_id=? ORDER BY stage.stage_number""",
            (run_id,),
        ).fetchall()
        return [cls._safe_stage(row) for row in rows]

    @staticmethod
    def _safe_stage(row: sqlite3.Row) -> dict[str, Any]:
        spec = json.loads(row["run_spec_json"])
        parameters = json.loads(row["parameters_json"])
        sensitive = set(spec.get("sensitive_parameters", []))
        return {
            "stage_ref": row["stage_ref"],
            "stage_number": int(row["stage_number"]),
            "stage_name": row["stage_name"],
            "stage_input_source": row["stage_input_source"],
            "status": row["status"],
            "parameters": {
                key: "********" if key in sensitive else value
                for key, value in parameters.items()
            },
            "run_spec": spec.get("public_spec"),
            "command_preview": spec.get(
                "safe_command_preview", row["command_preview"]
            ),
            "output_directory": row["output_directory"],
            "artifact": (
                {"kind": "stage_output", "output_directory": row["artifact_path"]}
                if row["artifact_path"] is not None
                else None
            ),
            "current_step": int(row["current_step"]),
            "total_steps": int(row["total_steps"]),
            "progress": (
                int(row["current_step"]) / int(row["total_steps"])
                if row["total_steps"]
                else 0
            ),
            "failure": (
                {"code": row["failure_code"], "message": row["failure_message"]}
                if row["failure_code"]
                else None
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def stop_run(self, run_ref: str, expected_revision: int, idempotency_key: str, actor: str) -> dict[str, Any]:
        timestamp = now_iso()
        digest = payload_hash(
            {"run_ref": run_ref, "expected_revision": expected_revision}
        )
        with self.transaction() as db:
            idem = db.execute(
                """SELECT payload_sha256,response_ref FROM training_idempotency
                WHERE scope='stop_run' AND idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if idem is not None:
                if idem["payload_sha256"] != digest or idem["response_ref"] != run_ref:
                    raise TrainingConflictError(
                        "idempotency_conflict",
                        "Idempotency-Key was already used for another request.",
                    )
                return self._get_run_in(db, idem["response_ref"])
            row = db.execute(
                self._RUN_SELECT + " WHERE run.run_ref=?", (run_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError("run_not_found", "Training run was not found.")
            if row["state_revision"] != expected_revision:
                raise TrainingConflictError("run_revision_conflict", "The run state changed.", current=self._safe_run(row, self._load_stages(db, int(row["id"]))))
            if RunStatus(row["status"]) in TERMINAL_RUN_STATUSES:
                raise TrainingConflictError("run_already_finished", "The run is already finished.", current=self._safe_run(row, self._load_stages(db, int(row["id"]))))
            if row["status"] == "queued":
                result = self._finish_in(db, row, "cancelled", timestamp)
            else:
                db.execute("UPDATE training_runs SET status='stop_requested',state_revision=state_revision+1,updated_at=? WHERE id=?", (timestamp, row["id"]))
                self._event(db, "run.updated", run_ref, {}, timestamp)
                result = self._get_run_in(db, run_ref)
            db.execute("INSERT INTO training_idempotency(scope,idempotency_key,payload_sha256,response_ref,created_at) VALUES('stop_run',?,?,?,?)", (idempotency_key, digest, run_ref, timestamp))
            self._audit(db, actor, "run.stop_requested", run_ref, {}, timestamp)
            return result

    def claim_next_run(self, worker_id: str, lease_seconds: float = 10) -> dict[str, Any] | None:
        timestamp = now_iso(); expiry = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(timespec="milliseconds")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM training_runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None: return None
            db.execute("UPDATE training_runs SET status='preparing',state_revision=state_revision+1,owner_id=?,owner_epoch=owner_epoch+1,lease_expires_at=?,heartbeat_at=?,updated_at=? WHERE id=?", (worker_id, expiry, timestamp, timestamp, row["id"]))
            stage = db.execute(
                """SELECT * FROM training_stages WHERE run_id=? AND status='pending'
                ORDER BY stage_number LIMIT 1""",
                (row["id"],),
            ).fetchone()
            if stage is None:
                return self._finish_in(
                    db,
                    row,
                    "failed",
                    timestamp,
                    "training_stages_missing",
                    "The training workflow has no pending stage.",
                )
            db.execute(
                """UPDATE training_stages SET status='preparing',updated_at=?
                WHERE id=?""",
                (timestamp, stage["id"]),
            )
            self._event(
                db,
                "run.updated",
                row["run_ref"],
                {"stage_ref": stage["stage_ref"], "stage_number": stage["stage_number"]},
                timestamp,
            )
            return self._get_run_in(db, row["run_ref"])

    def transition_running(self, run_ref: str, worker_id: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM training_runs WHERE run_ref=? AND owner_id=?", (run_ref, worker_id)).fetchone()
            if row is None: raise TrainingConflictError("worker_lease_lost", "Worker no longer owns this run.")
            if row["status"] == "stop_requested": return self._finish_in(db, row, "cancelled", timestamp)
            if row["status"] not in {"preparing", "running"}: raise TrainingConflictError("invalid_run_transition", "Run cannot enter running state.")
            stage = db.execute(
                """SELECT * FROM training_stages WHERE run_id=? AND status='preparing'
                ORDER BY stage_number LIMIT 1""",
                (row["id"],),
            ).fetchone()
            if stage is None:
                raise TrainingConflictError(
                    "invalid_stage_transition", "No stage is ready to start."
                )
            db.execute(
                """UPDATE training_runs SET status='running',
                state_revision=state_revision+1,started_at=COALESCE(started_at,?),
                updated_at=? WHERE id=?""",
                (timestamp, timestamp, row["id"]),
            )
            db.execute(
                """UPDATE training_stages SET status='running',started_at=?,
                updated_at=? WHERE id=?""",
                (timestamp, timestamp, stage["id"]),
            )
            log_seq = self._log(
                db,
                row["id"],
                "info",
                f"{stage['stage_name']} simulation started.",
                timestamp,
                stage_id=int(stage["id"]),
            )
            payload = {
                "stage_ref": stage["stage_ref"],
                "stage_number": stage["stage_number"],
            }
            self._event(db, "run.updated", run_ref, payload, timestamp)
            self._event(db, "run.log.appended", run_ref, {**payload, "item_seq": log_seq}, timestamp)
            return self._get_run_in(db, run_ref)

    def append_step(self, run_ref: str, worker_id: str, metric: dict[str, Any], message: str, stage_ref: str | None = None) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM training_runs WHERE run_ref=? AND owner_id=?", (run_ref, worker_id)).fetchone()
            if row is None: raise TrainingConflictError("worker_lease_lost", "Worker no longer owns this run.")
            if row["status"] == "stop_requested": return self._finish_in(db, row, "cancelled", timestamp)
            if row["status"] != "running": return self._get_run_in(db, run_ref)
            if stage_ref is None:
                stage = db.execute(
                    """SELECT * FROM training_stages WHERE run_id=? AND status='running'
                    ORDER BY stage_number LIMIT 1""",
                    (row["id"],),
                ).fetchone()
            else:
                stage = db.execute(
                    """SELECT * FROM training_stages
                    WHERE run_id=? AND stage_ref=? AND status='running'""",
                    (row["id"], stage_ref),
                ).fetchone()
            if stage is None:
                raise TrainingConflictError(
                    "worker_stage_lease_lost", "Worker no longer owns this stage."
                )
            seq = int(db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM metric_samples WHERE run_id=?", (row["id"],)).fetchone()[0])
            db.execute("""INSERT INTO metric_samples(run_id,seq,step,total_steps,epoch,loss,learning_rate,grad_norm,elapsed_seconds,gpu_json,created_at,stage_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (row["id"], seq, metric["step"], metric["total_steps"], metric["epoch"], metric["loss"], metric["learning_rate"], metric["grad_norm"], metric["elapsed_seconds"], canonical_json(metric["gpus"]), timestamp, stage["id"]))
            completed_steps = int(
                db.execute(
                    """SELECT COALESCE(SUM(total_steps),0) FROM training_stages
                    WHERE run_id=? AND status='succeeded'""",
                    (row["id"],),
                ).fetchone()[0]
            )
            overall_step = min(int(row["total_steps"]), completed_steps + int(metric["step"]))
            db.execute(
                """UPDATE training_stages SET current_step=?,updated_at=? WHERE id=?""",
                (metric["step"], timestamp, stage["id"]),
            )
            db.execute("UPDATE training_runs SET current_step=?,heartbeat_at=?,lease_expires_at=?,updated_at=? WHERE id=?", (overall_step, timestamp, (datetime.now(UTC)+timedelta(seconds=10)).isoformat(timespec="milliseconds"), timestamp, row["id"]))
            log_seq = self._log(db, row["id"], "info", message, timestamp, stage_id=int(stage["id"]))
            event_payload = {
                "stage_ref": stage["stage_ref"],
                "stage_number": stage["stage_number"],
            }
            self._event(db, "run.metric.appended", run_ref, {**event_payload, "item_seq": seq}, timestamp)
            self._event(db, "run.log.appended", run_ref, {**event_payload, "item_seq": log_seq}, timestamp)
            return self._get_run_in(db, run_ref)

    def finish_stage(
        self,
        run_ref: str,
        worker_id: str,
        stage_ref: str | None = None,
        status: str = "succeeded",
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM training_runs WHERE run_ref=? AND owner_id=?",
                (run_ref, worker_id),
            ).fetchone()
            if row is None:
                raise TrainingConflictError(
                    "worker_lease_lost", "Worker no longer owns this run."
                )
            if row["status"] == "stop_requested":
                return self._finish_in(db, row, "cancelled", timestamp)
            if stage_ref is None:
                stage = db.execute(
                    """SELECT * FROM training_stages
                    WHERE run_id=? AND status IN ('running','preparing')
                    ORDER BY stage_number LIMIT 1""",
                    (row["id"],),
                ).fetchone()
            else:
                stage = db.execute(
                    """SELECT * FROM training_stages WHERE run_id=? AND stage_ref=?
                    AND status IN ('running','preparing')""",
                    (row["id"], stage_ref),
                ).fetchone()
            if stage is None:
                raise TrainingConflictError(
                    "invalid_stage_transition", "The stage is no longer active."
                )
            if status != "succeeded":
                db.execute(
                    """UPDATE training_stages SET status=?,failure_code=?,
                    failure_message=?,finished_at=?,updated_at=? WHERE id=?""",
                    (
                        status,
                        failure_code,
                        failure_message,
                        timestamp,
                        timestamp,
                        stage["id"],
                    ),
                )
                return self._finish_in(
                    db, row, status, timestamp, failure_code, failure_message
                )

            db.execute(
                """UPDATE training_stages SET status='succeeded',
                current_step=total_steps,finished_at=?,updated_at=? WHERE id=?""",
                (timestamp, timestamp, stage["id"]),
            )
            version = db.execute(
                "SELECT id FROM model_versions WHERE run_id=?", (row["id"],)
            ).fetchone()
            db.execute(
                """INSERT INTO training_artifacts(
                artifact_ref,version_id,stage_id,kind,path,simulated,created_at)
                VALUES(?,?,?,'stage_output',?,1,?)""",
                (
                    new_ref("artifact"),
                    version["id"],
                    stage["id"],
                    stage["output_directory"],
                    timestamp,
                ),
            )
            completed_steps = int(
                db.execute(
                    """SELECT COALESCE(SUM(total_steps),0) FROM training_stages
                    WHERE run_id=? AND status='succeeded'""",
                    (row["id"],),
                ).fetchone()[0]
            )
            next_stage = db.execute(
                """SELECT * FROM training_stages WHERE run_id=? AND status='pending'
                ORDER BY stage_number LIMIT 1""",
                (row["id"],),
            ).fetchone()
            log_seq = self._log(
                db,
                row["id"],
                "info",
                f"{stage['stage_name']} simulation succeeded.",
                timestamp,
                stage_id=int(stage["id"]),
            )
            finished_payload = {
                "stage_ref": stage["stage_ref"],
                "stage_number": stage["stage_number"],
            }
            self._event(db, "run.log.appended", run_ref, {**finished_payload, "item_seq": log_seq}, timestamp)
            if next_stage is not None:
                db.execute(
                    """UPDATE training_stages SET status='preparing',updated_at=?
                    WHERE id=?""",
                    (timestamp, next_stage["id"]),
                )
                db.execute(
                    """UPDATE training_runs SET current_step=?,
                    state_revision=state_revision+1,heartbeat_at=?,
                    lease_expires_at=?,updated_at=? WHERE id=?""",
                    (
                        completed_steps,
                        timestamp,
                        (datetime.now(UTC) + timedelta(seconds=10)).isoformat(
                            timespec="milliseconds"
                        ),
                        timestamp,
                        row["id"],
                    ),
                )
                self._event(
                    db,
                    "run.updated",
                    run_ref,
                    {
                        "stage_ref": next_stage["stage_ref"],
                        "stage_number": next_stage["stage_number"],
                    },
                    timestamp,
                )
                return self._get_run_in(db, run_ref)

            db.execute(
                """INSERT INTO training_artifacts(
                artifact_ref,version_id,stage_id,kind,path,simulated,created_at)
                VALUES(?,?,?,'version_model',?,1,?)""",
                (
                    new_ref("artifact"),
                    version["id"],
                    stage["id"],
                    stage["output_directory"],
                    timestamp,
                ),
            )
            return self._finish_in(db, row, "succeeded", timestamp)

    def finish_run(self, run_ref: str, worker_id: str, status: str = "succeeded", failure_code: str | None = None, failure_message: str | None = None) -> dict[str, Any]:
        return self.finish_stage(
            run_ref,
            worker_id,
            None,
            status,
            failure_code,
            failure_message,
        )

    def _finish_in(self, db: sqlite3.Connection, row: sqlite3.Row, status: str, timestamp: str, failure_code: str | None = None, failure_message: str | None = None) -> dict[str, Any]:
        if status == "cancelled":
            db.execute(
                """UPDATE training_stages SET status='cancelled',finished_at=?,
                updated_at=? WHERE run_id=?
                AND status IN ('pending','preparing','running')""",
                (timestamp, timestamp, row["id"]),
            )
        elif status in {"failed", "lost"}:
            active_status = "lost" if status == "lost" else "failed"
            db.execute(
                """UPDATE training_stages SET status=?,failure_code=COALESCE(failure_code,?),
                failure_message=COALESCE(failure_message,?),finished_at=?,updated_at=?
                WHERE run_id=? AND status IN ('preparing','running')""",
                (
                    active_status,
                    failure_code,
                    failure_message,
                    timestamp,
                    timestamp,
                    row["id"],
                ),
            )
            db.execute(
                """UPDATE training_stages SET status='skipped',finished_at=?,updated_at=?
                WHERE run_id=? AND status='pending'""",
                (timestamp, timestamp, row["id"]),
            )
        db.execute("""UPDATE training_runs SET status=?,
            current_step=CASE WHEN ?='succeeded' THEN total_steps ELSE current_step END,
            state_revision=state_revision+1,owner_id=NULL,lease_expires_at=NULL,
            finished_at=?,updated_at=?,failure_code=?,failure_message=? WHERE id=?""",
            (status, status, timestamp, timestamp, failure_code, failure_message, row["id"]),
        )
        db.execute("DELETE FROM gpu_leases WHERE run_id=?", (row["id"],)); db.execute("DELETE FROM port_leases WHERE run_id=?", (row["id"],))
        if status == "succeeded":
            stage = db.execute(
                """SELECT * FROM training_stages WHERE run_id=?
                AND status='succeeded' ORDER BY stage_number DESC LIMIT 1""",
                (row["id"],),
            ).fetchone()
        else:
            stage = db.execute(
                """SELECT * FROM training_stages WHERE run_id=? AND status=?
                ORDER BY stage_number LIMIT 1""",
                (row["id"], status),
            ).fetchone()
        stage_payload = (
            {
                "stage_ref": stage["stage_ref"],
                "stage_number": stage["stage_number"],
            }
            if stage is not None
            else {}
        )
        log_seq = self._log(
            db,
            row["id"],
            "info" if status in {"succeeded", "cancelled"} else "error",
            f"Simulation run {status}.",
            timestamp,
            stage_id=int(stage["id"]) if stage is not None else None,
        )
        self._event(
            db,
            "run.log.appended",
            row["run_ref"],
            {**stage_payload, "item_seq": log_seq},
            timestamp,
        )
        self._event(db, "run.updated", row["run_ref"], stage_payload, timestamp)
        return self._get_run_in(db, row["run_ref"])

    def recover_stale_runs(self) -> int:
        timestamp = now_iso()
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM training_runs WHERE status IN ('preparing','running','stop_requested') AND lease_expires_at IS NOT NULL AND lease_expires_at<?", (timestamp,)).fetchall()
            for row in rows:
                self._finish_in(db, row, "lost", timestamp, "worker_lease_expired", "The simulation worker lease expired.")
            return len(rows)

    def list_logs(self, run_ref: str, after_seq: int, limit: int, stage_ref: str | None = None) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT id FROM training_runs WHERE run_ref=?", (run_ref,)).fetchone()
            if row is None: raise TrainingNotFoundError("run_not_found", "Training run was not found.")
            values: list[Any] = [row["id"], after_seq]
            stage_clause = ""
            if stage_ref is not None:
                stage_clause = " AND stage.stage_ref=?"
                values.append(stage_ref)
            values.append(limit + 1)
            items = [dict(item) for item in db.execute(f"""SELECT log.seq,log.level,
                log.message,log.created_at,stage.stage_ref,stage.stage_number
                FROM run_logs AS log
                LEFT JOIN training_stages AS stage ON stage.id=log.stage_id
                WHERE log.run_id=? AND log.seq>?{stage_clause}
                ORDER BY log.seq LIMIT ?""", tuple(values)).fetchall()]
        return {"items": items[:limit], "next_after": items[limit-1]["seq"] if len(items) > limit else None}

    def list_metrics(self, run_ref: str, after_seq: int, limit: int, stage_ref: str | None = None) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT id FROM training_runs WHERE run_ref=?", (run_ref,)).fetchone()
            if row is None: raise TrainingNotFoundError("run_not_found", "Training run was not found.")
            values: list[Any] = [row["id"], after_seq]
            stage_clause = ""
            if stage_ref is not None:
                stage_clause = " AND stage.stage_ref=?"
                values.append(stage_ref)
            values.append(limit + 1)
            rows = db.execute(f"""SELECT metric.*,stage.stage_ref,stage.stage_number
                FROM metric_samples AS metric
                LEFT JOIN training_stages AS stage ON stage.id=metric.stage_id
                WHERE metric.run_id=? AND metric.seq>?{stage_clause}
                ORDER BY metric.seq LIMIT ?""", tuple(values)).fetchall()
        items = [{**{key: item[key] for key in ("seq","step","total_steps","epoch","loss","learning_rate","grad_norm","elapsed_seconds","created_at","stage_ref","stage_number")}, "gpus": json.loads(item["gpu_json"])} for item in rows]
        return {"items": items[:limit], "next_after": items[limit-1]["seq"] if len(items) > limit else None}

    def list_events(self, after_seq: int, limit: int) -> dict[str, Any]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM training_events WHERE seq>? ORDER BY seq LIMIT ?", (after_seq, limit + 1)).fetchall()
        items = [{"event_id": row["seq"], "type": row["event_type"], "run_ref": row["run_ref"], **json.loads(row["payload_json"])} for row in rows]
        return {"items": items[:limit], "next_after": items[limit-1]["event_id"] if len(items) > limit else None}

    def list_audit_events(self, target_ref: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT action,created_at FROM audit_events WHERE target_ref=? ORDER BY seq LIMIT ?",
                (target_ref, limit),
            ).fetchall()
        return [
            {
                "created_at": row["created_at"],
                "action": row["action"],
                "summary": row["action"].replace(".", " "),
            }
            for row in rows
        ]

    def authenticate_worker(
        self, node_ref: str, worker_token: str, worker_instance_id: str | None = None
    ) -> dict[str, Any]:
        digest = self._token_digest(worker_token)
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)
            ).fetchone()
        if (
            row is None
            or row["worker_token_sha256"] is None
            or not secrets.compare_digest(str(row["worker_token_sha256"]), digest)
            or (
                worker_instance_id is not None
                and row["worker_instance_id"] != worker_instance_id
            )
        ):
            raise TrainingForbiddenError(
                "worker_authentication_failed",
                "The Training Worker credential is invalid.",
            )
        return self._safe_node(row)

    def ensure_source_manifest_placeholder(self, release: dict[str, Any]) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM dataset_source_manifests WHERE release_ref=?",
                (release["release_ref"],),
            ).fetchone()
            if existing is not None:
                return self._safe_source_manifest(existing)
            manifest_ref = new_ref("dataset_manifest")
            cursor = db.execute(
                """INSERT INTO dataset_source_manifests(
                manifest_ref,release_ref,domain,dataset_date,status,created_at,updated_at)
                VALUES(?,?,?,?,'preparing',?,?)""",
                (
                    manifest_ref,
                    release["release_ref"],
                    release.get("domain", "navigation"),
                    release["dataset_date"],
                    timestamp,
                    timestamp,
                ),
            )
            row = db.execute("SELECT * FROM dataset_source_manifests WHERE id=?", (cursor.lastrowid,)).fetchone()
            assert row is not None
            return self._safe_source_manifest(row)

    def complete_source_manifest(self, inventory: dict[str, Any]) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM dataset_source_manifests WHERE release_ref=?",
                (inventory["release_ref"],),
            ).fetchone()
            if existing is None:
                raise TrainingNotFoundError("dataset_source_manifest_not_found", "The dataset source manifest was not found.")
            manifest_ref = str(existing["manifest_ref"])
            files: list[dict[str, Any]] = []
            for index, item in enumerate(inventory["files"], start=1):
                files.append(
                    {
                        **item,
                        "file_ref": "dataset_file_"
                        + hashlib.sha256(
                            f"{manifest_ref}\0{index}\0{item['relative_path']}".encode()
                        ).hexdigest(),
                    }
                )
            db.execute(
                "DELETE FROM dataset_source_files WHERE manifest_id=?",
                (existing["id"],),
            )
            db.executemany(
                """INSERT INTO dataset_source_files(
                manifest_id,file_ref,ordinal,relative_path,size_bytes,sha256)
                VALUES(?,?,?,?,?,?)""",
                (
                    (
                        existing["id"],
                        item["file_ref"],
                        ordinal,
                        item["relative_path"],
                        int(item["size_bytes"]),
                        item["sha256"],
                    )
                    for ordinal, item in enumerate(files)
                ),
            )
            db.execute(
                """UPDATE dataset_source_manifests SET status='ready',source_root=?,
                inventory_json=?,inventory_sha256=?,file_count=?,total_bytes=?,
                error_code=NULL,error_message=NULL,preparation_lease_expires_at=NULL,
                updated_at=? WHERE id=?""",
                (
                    inventory["source_root"],
                    None,
                    inventory["inventory_sha256"],
                    len(files),
                    inventory["total_bytes"],
                    timestamp,
                    existing["id"],
                ),
            )
            row = db.execute(
                "SELECT * FROM dataset_source_manifests WHERE id=?",
                (existing["id"],),
            ).fetchone()
            assert row is not None
            waiting = db.execute(
                """SELECT transfer.* FROM dataset_transfers AS transfer
                WHERE transfer.source_manifest_id=? AND transfer.status='preparing'
                ORDER BY transfer.id""", (existing["id"],)
            ).fetchall()
            for transfer in waiting:
                db.execute("UPDATE dataset_transfers SET status='queued',updated_at=? WHERE transfer_ref=?", (timestamp, transfer["transfer_ref"]))
                node = db.execute("SELECT * FROM training_nodes WHERE id=?", (transfer["node_id"],)).fetchone()
                if node is None:
                    continue
                self._insert_node_command(db, node, "transfer_dataset", {
                    "transfer_ref": transfer["transfer_ref"],
                    "release_ref": inventory["release_ref"],
                    "dataset_date": inventory["dataset_date"],
                    "destination_parent": transfer["target_parent_directory"],
                }, timestamp)
            return self._safe_source_manifest(row)

    def claim_source_manifest_preparation(self) -> dict[str, Any] | None:
        timestamp = now_iso()
        lease_expires_at = (
            datetime.now(UTC) + timedelta(minutes=30)
        ).isoformat(timespec="milliseconds")
        with self.transaction() as db:
            row = db.execute(
                """SELECT * FROM dataset_source_manifests
                WHERE status='preparing'
                   OR (status='building' AND preparation_lease_expires_at<?)
                ORDER BY id LIMIT 1""",
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE dataset_source_manifests SET status='building',error_code=NULL,error_message=NULL,preparation_lease_expires_at=?,updated_at=? WHERE id=?", (lease_expires_at, timestamp, row["id"]))
            return {"manifest_ref": row["manifest_ref"], "release_ref": row["release_ref"], "dataset_date": row["dataset_date"]}

    def fail_source_manifest_preparation(self, manifest_ref: str, code: str, message: str) -> None:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute("SELECT id FROM dataset_source_manifests WHERE manifest_ref=?", (manifest_ref,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE dataset_source_manifests SET status='failed',error_code=?,error_message=?,preparation_lease_expires_at=NULL,updated_at=? WHERE id=?", (code, message, timestamp, row["id"]))
            db.execute("UPDATE dataset_transfers SET status='failed',error_code=?,error_message=?,finished_at=?,updated_at=? WHERE source_manifest_id=? AND status='preparing'", (code, message, timestamp, timestamp, row["id"]))

    @staticmethod
    def _safe_source_manifest(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "manifest_ref": row["manifest_ref"],
            "release_ref": row["release_ref"],
            "dataset_date": row["dataset_date"],
            "status": row["status"],
            "inventory_sha256": row["inventory_sha256"],
            "file_count": int(row["file_count"]),
            "total_bytes": int(row["total_bytes"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }

    def get_source_manifest_by_release(self, release_ref: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM dataset_source_manifests WHERE release_ref=?",
                (release_ref,),
            ).fetchone()
        return self._safe_source_manifest(row) if row is not None else None

    def source_manifest_page(
        self, release_ref: str, *, cursor: int, limit: int
    ) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM dataset_source_manifests WHERE release_ref=?",
                (release_ref,),
            ).fetchone()
            page_rows = (
                db.execute(
                    """SELECT file_ref,relative_path,size_bytes,sha256
                    FROM dataset_source_files WHERE manifest_id=?
                    ORDER BY ordinal LIMIT ? OFFSET ?""",
                    (row["id"], limit, cursor),
                ).fetchall()
                if row is not None and row["status"] == "ready"
                else []
            )
        if row is None:
            raise TrainingNotFoundError(
                "dataset_source_manifest_not_found",
                "The dataset source manifest was not found.",
            )
        if row["status"] != "ready":
            raise TrainingConflictError("dataset_source_manifest_not_ready", "The dataset source manifest is not ready.")
        page = [
            {
                "file_ref": item["file_ref"],
                "relative_path": item["relative_path"],
                "size_bytes": int(item["size_bytes"]),
                "sha256": item["sha256"],
            }
            for item in page_rows
        ]
        next_cursor = (
            cursor + len(page)
            if cursor + len(page) < int(row["file_count"])
            else None
        )
        return {
            **self._safe_source_manifest(row),
            "files": page,
            "next_cursor": next_cursor,
        }

    def get_source_file(self, file_ref: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                """SELECT file.file_ref,file.relative_path,file.size_bytes,file.sha256,
                source.release_ref,source.source_root
                FROM dataset_source_files AS file
                JOIN dataset_source_manifests AS source ON source.id=file.manifest_id
                WHERE file.file_ref=? AND source.status='ready'""",
                (file_ref,),
            ).fetchone()
        if row is None:
            raise TrainingNotFoundError(
                "dataset_source_file_not_found",
                "The released dataset file was not found.",
            )
        root = Path(str(row["source_root"]))
        candidate = root / row["relative_path"]
        try:
            if candidate.is_symlink():
                raise OSError("symbolic link")
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise TrainingNotFoundError(
                "dataset_source_file_not_found",
                "The released dataset file is no longer available.",
            ) from exc
        if root not in resolved.parents or not resolved.is_file():
            raise TrainingForbiddenError(
                "dataset_source_file_unsafe",
                "The released dataset file is not safe to serve.",
            )
        return {
            "file_ref": row["file_ref"],
            "relative_path": row["relative_path"],
            "size_bytes": int(row["size_bytes"]),
            "sha256": row["sha256"],
            "path": resolved,
            "release_ref": row["release_ref"],
        }

    def authorize_source_download(self, node_ref: str, release_ref: str) -> None:
        with self.connection() as db:
            row = db.execute(
                """SELECT 1 FROM dataset_transfers AS transfer
                JOIN dataset_source_manifests AS source
                  ON source.id=transfer.source_manifest_id
                WHERE transfer.node_ref_snapshot=? AND source.release_ref=?
                  AND transfer.status IN (
                    'preparing','queued','running','pause_requested','cancel_requested'
                  ) LIMIT 1""",
                (node_ref, release_ref),
            ).fetchone()
        if row is None:
            raise TrainingForbiddenError(
                "dataset_source_not_assigned_to_node",
                "This released dataset was not assigned to the authenticated node.",
            )

    def create_directory_listing(
        self, node_ref: str, path: str, actor: str
    ) -> dict[str, Any]:
        timestamp = now_iso()
        command_ref = new_ref("listing")
        with self.transaction() as db:
            node = self._require_online_node_in(db, node_ref)
            db.execute(
                """INSERT INTO training_node_commands(
                command_ref,node_id,node_ref_snapshot,kind,status,request_json,
                created_at,updated_at) VALUES(?,?,?,'list_directories','queued',?,?,?)""",
                (
                    command_ref,
                    node["id"],
                    node_ref,
                    canonical_json({"path": path}),
                    timestamp,
                    timestamp,
                ),
            )
            self._audit(db, actor, "dataset.directory_listing_requested", command_ref, {}, timestamp)
        return self.get_directory_listing(command_ref)

    def get_directory_listing(self, listing_ref: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                """SELECT * FROM training_node_commands
                WHERE command_ref=? AND kind='list_directories'""",
                (listing_ref,),
            ).fetchone()
        if row is None:
            raise TrainingNotFoundError(
                "directory_listing_not_found", "The directory listing was not found."
            )
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        listing = result.get("listing") or {
            key: result[key]
            for key in ("path", "parent_path", "writable", "free_bytes", "directories")
            if key in result
        }
        error = result.get("error") or {}
        return {
            "listing_ref": row["command_ref"],
            "node_ref": row["node_ref_snapshot"],
            "status": row["status"],
            **listing,
            "error_code": error.get("code"),
            "error_message": error.get("message"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_dataset_transfers(
        self,
        *,
        node_ref: str,
        source_manifests: list[dict[str, Any]],
        target_parent_directory: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        actor: str,
    ) -> list[dict[str, Any]]:
        digest = payload_hash(request_payload)
        timestamp = now_iso()
        with self.transaction() as db:
            replay = db.execute(
                """SELECT payload_sha256,response_ref FROM training_idempotency
                WHERE scope='dataset_transfer_batch' AND idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["payload_sha256"] != digest:
                    raise TrainingConflictError(
                        "idempotency_conflict",
                        "Idempotency-Key was already used for another request.",
                    )
                return [
                    self._get_dataset_transfer_in(db, ref)
                    for ref in json.loads(replay["response_ref"])
                ]
            node = self._require_online_node_in(db, node_ref)
            active = db.execute(
                """SELECT transfer_ref FROM dataset_transfers
                WHERE node_id=? AND status IN ('preparing','queued','running','pause_requested','cancel_requested')
                LIMIT 1""",
                (node["id"],),
            ).fetchone()
            if active is not None:
                raise TrainingConflictError(
                    "dataset_transfer_node_busy",
                    "This training node already has an active dataset transfer.",
                    current={"transfer_ref": active["transfer_ref"]},
                )
            transfer_refs: list[str] = []
            for manifest in source_manifests:
                source = db.execute(
                    "SELECT * FROM dataset_source_manifests WHERE manifest_ref=?",
                    (manifest["manifest_ref"],),
                ).fetchone()
                assert source is not None
                if source["status"] == "failed":
                    db.execute(
                        """UPDATE dataset_source_manifests SET status='preparing',
                        error_code=NULL,error_message=NULL,
                        preparation_lease_expires_at=NULL,updated_at=? WHERE id=?""",
                        (timestamp, source["id"]),
                    )
                    source = db.execute(
                        "SELECT * FROM dataset_source_manifests WHERE id=?",
                        (source["id"],),
                    ).fetchone()
                    assert source is not None
                replica = db.execute(
                    """SELECT replica_ref FROM dataset_replicas
                    WHERE node_id=? AND source_manifest_id=?""",
                    (node["id"], source["id"]),
                ).fetchone()
                if replica is not None:
                    raise TrainingConflictError(
                        "dataset_replica_already_exists",
                        "This released date already exists on the training node.",
                        current={"replica_ref": replica["replica_ref"]},
                    )
                previous = db.execute(
                    """SELECT transfer_ref,status FROM dataset_transfers
                    WHERE node_id=? AND source_manifest_id=?
                      AND status IN ('failed','paused')
                    ORDER BY id DESC LIMIT 1""",
                    (node["id"], source["id"]),
                ).fetchone()
                if previous is not None:
                    raise TrainingConflictError(
                        "dataset_transfer_retry_required",
                        "This released date already has a paused or failed transfer. Continue that transfer instead.",
                        current={
                            "transfer_ref": previous["transfer_ref"],
                            "status": previous["status"],
                        },
                    )
                transfer_ref = new_ref("transfer")
                suffix = str(source["release_ref"]).rsplit("_", 1)[-1][:8]
                final_directory = (
                    target_parent_directory.rstrip("/")
                    + f"/datapilot-managed/{source['dataset_date']}-{suffix}"
                )
                initial_status = "queued" if source["status"] == "ready" else "preparing"
                db.execute(
                    """INSERT INTO dataset_transfers(
                    transfer_ref,node_id,node_ref_snapshot,source_manifest_id,status,
                    target_parent_directory,final_directory,error_code,error_message,
                    created_at,updated_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        transfer_ref,
                        node["id"],
                        node_ref,
                        source["id"],
                        initial_status,
                        target_parent_directory,
                        final_directory,
                        None,
                        None,
                        timestamp,
                        timestamp,
                        None,
                    ),
                )
                if initial_status == "queued":
                    self._insert_node_command(
                        db,
                        node,
                        "transfer_dataset",
                        {
                            "transfer_ref": transfer_ref,
                            "release_ref": source["release_ref"],
                            "dataset_date": source["dataset_date"],
                            "destination_parent": target_parent_directory,
                        },
                        timestamp,
                    )
                transfer_refs.append(transfer_ref)
                self._audit(db, actor, "dataset.transfer_requested", transfer_ref, {}, timestamp)
            db.execute(
                """INSERT INTO training_idempotency(
                scope,idempotency_key,payload_sha256,response_ref,created_at)
                VALUES('dataset_transfer_batch',?,?,?,?)""",
                (idempotency_key, digest, canonical_json(transfer_refs), timestamp),
            )
            return [self._get_dataset_transfer_in(db, ref) for ref in transfer_refs]

    def list_dataset_transfers(
        self, *, node_ref: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if node_ref is not None:
            clauses.append("transfer.node_ref_snapshot=?")
            values.append(node_ref)
        if status is not None:
            clauses.append("transfer.status=?")
            values.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            rows = db.execute(
                self._TRANSFER_SELECT + f" {where} ORDER BY transfer.id DESC",
                tuple(values),
            ).fetchall()
        return [self._safe_dataset_transfer(row) for row in rows]

    def get_dataset_transfer(self, transfer_ref: str) -> dict[str, Any]:
        with self.connection() as db:
            return self._get_dataset_transfer_in(db, transfer_ref)

    def _get_dataset_transfer_in(
        self, db: sqlite3.Connection, transfer_ref: str
    ) -> dict[str, Any]:
        row = db.execute(
            self._TRANSFER_SELECT + " WHERE transfer.transfer_ref=?", (transfer_ref,)
        ).fetchone()
        if row is None:
            raise TrainingNotFoundError(
                "dataset_transfer_not_found", "The dataset transfer was not found."
            )
        return self._safe_dataset_transfer(row)

    _TRANSFER_SELECT = """SELECT transfer.*,source.release_ref,source.dataset_date,
      source.inventory_sha256,source.file_count,source.total_bytes
      FROM dataset_transfers AS transfer
      JOIN dataset_source_manifests AS source ON source.id=transfer.source_manifest_id"""

    @staticmethod
    def _safe_dataset_transfer(row: sqlite3.Row) -> dict[str, Any]:
        total = int(row["total_bytes"])
        transferred = min(int(row["bytes_transferred"]), total)
        return {
            "transfer_ref": row["transfer_ref"],
            "node_ref": row["node_ref_snapshot"],
            "release_ref": row["release_ref"],
            "dataset_date": row["dataset_date"],
            "status": row["status"],
            "bytes_transferred": transferred,
            "total_bytes": total,
            "files_completed": int(row["files_completed"]),
            "file_count": int(row["file_count"]),
            "progress_percent": round(transferred * 100 / total, 2) if total else (100.0 if row["status"] == "succeeded" else 0.0),
            "target_parent_directory": row["target_parent_directory"],
            "final_directory": row["final_directory"],
            "inventory_sha256": row["inventory_sha256"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def pause_dataset_transfer(
        self, transfer_ref: str, actor: str
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            transfer = db.execute(
                self._TRANSFER_SELECT + " WHERE transfer.transfer_ref=?",
                (transfer_ref,),
            ).fetchone()
            if transfer is None:
                raise TrainingNotFoundError(
                    "dataset_transfer_not_found", "The dataset transfer was not found."
                )
            if transfer["status"] not in {"preparing", "queued", "running"}:
                raise TrainingConflictError(
                    "dataset_transfer_not_pausable",
                    "Only a preparing, queued, or running transfer can be paused.",
                    current=self._safe_dataset_transfer(transfer),
                )
            command_running = self._transfer_command_running(db, transfer_ref)
            next_status = "pause_requested" if transfer["status"] == "running" or command_running else "paused"
            db.execute(
                """UPDATE dataset_transfers SET status=?,updated_at=?,
                finished_at=CASE WHEN ?='paused' THEN ? ELSE finished_at END
                WHERE id=?""",
                (next_status, timestamp, next_status, timestamp, transfer["id"]),
            )
            if transfer["status"] == "queued" and not command_running:
                self._cancel_queued_transfer_commands(db, transfer_ref, timestamp)
            if next_status == "pause_requested":
                node = self._require_online_node_in(db, transfer["node_ref_snapshot"])
                self._insert_node_command(
                    db,
                    node,
                    "cancel_dataset_transfer",
                    self._dataset_stop_command_payload(transfer, action="pause"),
                    timestamp,
                )
            self._event(db, "dataset.transfer.updated", None, {"transfer_ref": transfer_ref, "status": next_status}, timestamp)
            self._audit(db, actor, "dataset.transfer_pause_requested", transfer_ref, {}, timestamp)
            return self._get_dataset_transfer_in(db, transfer_ref)

    def cancel_dataset_transfer(
        self, transfer_ref: str, actor: str
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            transfer = db.execute(
                self._TRANSFER_SELECT + " WHERE transfer.transfer_ref=?",
                (transfer_ref,),
            ).fetchone()
            if transfer is None:
                raise TrainingNotFoundError(
                    "dataset_transfer_not_found", "The dataset transfer was not found."
                )
            if transfer["status"] not in {"preparing", "queued", "running", "paused", "failed"}:
                raise TrainingConflictError(
                    "dataset_transfer_not_cancellable",
                    "Only an unfinished, paused, or failed transfer can be cancelled.",
                    current=self._safe_dataset_transfer(transfer),
                )
            command_running = self._transfer_command_running(db, transfer_ref)
            next_status = "cancelled" if transfer["status"] == "preparing" or (transfer["status"] == "queued" and not command_running) else "cancel_requested"
            db.execute(
                """UPDATE dataset_transfers SET status=?,updated_at=?,
                finished_at=CASE WHEN ?='cancelled' THEN ? ELSE finished_at END
                WHERE id=?""",
                (next_status, timestamp, next_status, timestamp, transfer["id"]),
            )
            if transfer["status"] == "queued" and not command_running:
                self._cancel_queued_transfer_commands(db, transfer_ref, timestamp)
            if next_status == "cancel_requested":
                node = self._require_online_node_in(db, transfer["node_ref_snapshot"])
                self._insert_node_command(
                    db,
                    node,
                    "cancel_dataset_transfer",
                    self._dataset_stop_command_payload(transfer, action="cancel"),
                    timestamp,
                )
            self._event(db, "dataset.transfer.updated", None, {"transfer_ref": transfer_ref, "status": next_status}, timestamp)
            self._audit(db, actor, "dataset.transfer_cancel_requested", transfer_ref, {}, timestamp)
            return self._get_dataset_transfer_in(db, transfer_ref)

    @staticmethod
    def _transfer_command_running(db: sqlite3.Connection, transfer_ref: str) -> bool:
        rows = db.execute(
            """SELECT request_json FROM training_node_commands
            WHERE kind='transfer_dataset' AND status='running'"""
        ).fetchall()
        return any(
            json.loads(command["request_json"]).get("transfer_ref") == transfer_ref
            for command in rows
        )

    @staticmethod
    def _dataset_stop_command_payload(transfer: sqlite3.Row, *, action: str) -> dict[str, Any]:
        return {
            "transfer_ref": transfer["transfer_ref"],
            "release_ref": transfer["release_ref"],
            "dataset_date": transfer["dataset_date"],
            "destination_parent": transfer["target_parent_directory"],
            "action": action,
        }

    @staticmethod
    def _cancel_queued_transfer_commands(
        db: sqlite3.Connection, transfer_ref: str, timestamp: str
    ) -> None:
        rows = db.execute(
            """SELECT id,request_json FROM training_node_commands
            WHERE kind='transfer_dataset' AND status='queued'"""
        ).fetchall()
        for command in rows:
            if json.loads(command["request_json"]).get("transfer_ref") != transfer_ref:
                continue
            result = canonical_json({
                "status": "cancelled",
                "transfer_ref": transfer_ref,
                "reason": "transfer_stopped_before_start",
            })
            db.execute(
                """UPDATE training_node_commands SET status='cancelled',result_json=?,
                finished_at=?,updated_at=? WHERE id=?""",
                (result, timestamp, timestamp, command["id"]),
            )

    def retry_dataset_transfer(
        self, transfer_ref: str, idempotency_key: str, actor: str
    ) -> dict[str, Any]:
        timestamp = now_iso()
        digest = payload_hash({"transfer_ref": transfer_ref})
        with self.transaction() as db:
            replay = db.execute(
                """SELECT payload_sha256,response_ref FROM training_idempotency
                WHERE scope='dataset_transfer_retry' AND idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["payload_sha256"] != digest or replay["response_ref"] != transfer_ref:
                    raise TrainingConflictError("idempotency_conflict", "Idempotency-Key was already used for another request.")
                return self._get_dataset_transfer_in(db, transfer_ref)
            transfer = db.execute(
                self._TRANSFER_SELECT + " WHERE transfer.transfer_ref=?", (transfer_ref,)
            ).fetchone()
            if transfer is None:
                raise TrainingNotFoundError("dataset_transfer_not_found", "The dataset transfer was not found.")
            if transfer["status"] not in {"failed", "paused"}:
                raise TrainingConflictError("dataset_transfer_not_retryable", "Only failed or paused transfers can be continued.")
            node = self._require_online_node_in(db, transfer["node_ref_snapshot"])
            source = db.execute("SELECT * FROM dataset_source_manifests WHERE id=?", (transfer["source_manifest_id"],)).fetchone()
            assert source is not None
            next_status = "queued" if source["status"] == "ready" else "preparing"
            db.execute(
                """UPDATE dataset_transfers SET status=?,error_code=NULL,
                error_message=NULL,started_at=NULL,finished_at=NULL,updated_at=? WHERE id=?""",
                (next_status, timestamp, transfer["id"]),
            )
            if next_status == "preparing":
                db.execute("UPDATE dataset_source_manifests SET status='preparing',error_code=NULL,error_message=NULL,preparation_lease_expires_at=NULL,updated_at=? WHERE id=?", (timestamp, source["id"]))
            else:
                self._insert_node_command(db, node, "transfer_dataset", {
                    "transfer_ref": transfer_ref,
                    "release_ref": transfer["release_ref"],
                    "dataset_date": transfer["dataset_date"],
                    "destination_parent": transfer["target_parent_directory"],
                }, timestamp)
            db.execute(
                """INSERT INTO training_idempotency(scope,idempotency_key,payload_sha256,response_ref,created_at)
                VALUES('dataset_transfer_retry',?,?,?,?)""",
                (idempotency_key, digest, transfer_ref, timestamp),
            )
            self._audit(db, actor, "dataset.transfer_retried", transfer_ref, {}, timestamp)
            return self._get_dataset_transfer_in(db, transfer_ref)

    def list_dataset_replicas(self, node_ref: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                self._REPLICA_SELECT
                + " WHERE replica.node_ref_snapshot=? AND replica.status='ready' ORDER BY source.dataset_date DESC",
                (node_ref,),
            ).fetchall()
        return [self._safe_dataset_replica(row) for row in rows]

    def resolve_dataset_selection(
        self,
        *,
        node_ref: str,
        train_replica_refs: list[str],
        test_replica_refs: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        train_set = set(train_replica_refs)
        test_set = set(test_replica_refs)
        if train_set & test_set:
            raise TrainingConflictError(
                "dataset_split_overlap",
                "A released date cannot be used in both the training and test sets.",
            )
        requested = [*train_replica_refs, *test_replica_refs]
        if not requested:
            return {"train": [], "test": []}
        placeholders = ",".join("?" for _ in requested)
        with self.connection() as db:
            rows = db.execute(
                self._REPLICA_SELECT
                + f" WHERE replica.replica_ref IN ({placeholders})",
                tuple(requested),
            ).fetchall()
        by_ref = {str(row["replica_ref"]): row for row in rows}
        if set(by_ref) != set(requested):
            raise TrainingNotFoundError(
                "dataset_replica_not_found",
                "One or more selected dataset dates were not found.",
            )
        for row in rows:
            if row["node_ref_snapshot"] != node_ref or row["status"] != "ready":
                raise TrainingConflictError(
                    "dataset_replica_unavailable",
                    "Every selected dataset date must be ready on the model's training node.",
                )

        def entry(ref: str) -> dict[str, Any]:
            row = by_ref[ref]
            return {
                "dataset_date": row["dataset_date"],
                "release_ref": row["release_ref"],
                "replica_ref": row["replica_ref"],
                "local_root": row["local_root"],
                "inventory_sha256": row["inventory_sha256"],
            }

        return {
            "train": [entry(ref) for ref in train_replica_refs],
            "test": [entry(ref) for ref in test_replica_refs],
        }

    def get_dataset_replica(self, replica_ref: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                self._REPLICA_SELECT + " WHERE replica.replica_ref=?", (replica_ref,)
            ).fetchone()
        if row is None:
            raise TrainingNotFoundError("dataset_replica_not_found", "The dataset replica was not found.")
        return self._safe_dataset_replica(row)

    _REPLICA_SELECT = """SELECT replica.*,source.release_ref,source.dataset_date
      FROM dataset_replicas AS replica
      JOIN dataset_source_manifests AS source ON source.id=replica.source_manifest_id"""

    @staticmethod
    def _safe_dataset_replica(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "replica_ref": row["replica_ref"],
            "node_ref": row["node_ref_snapshot"],
            "release_ref": row["release_ref"],
            "dataset_date": row["dataset_date"],
            "status": row["status"],
            "local_root": row["local_root"],
            "inventory_sha256": row["inventory_sha256"],
            "file_count": int(row["file_count"]),
            "total_bytes": int(row["total_bytes"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def remove_dataset_replica(self, replica_ref: str, actor: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute(
                self._REPLICA_SELECT + " WHERE replica.replica_ref=?", (replica_ref,)
            ).fetchone()
            if row is None:
                raise TrainingNotFoundError("dataset_replica_not_found", "The dataset replica was not found.")
            if row["status"] != "ready":
                raise TrainingConflictError("dataset_replica_not_removable", "The dataset replica is already being removed.")
            node = self._require_online_node_in(db, row["node_ref_snapshot"])
            db.execute("UPDATE dataset_replicas SET status='removing',updated_at=? WHERE id=?", (timestamp, row["id"]))
            self._insert_node_command(db, node, "remove_dataset_replica", {
                "replica_ref": replica_ref,
                "local_root": row["local_root"],
                "release_ref": row["release_ref"],
                "inventory_sha256": row["inventory_sha256"],
            }, timestamp)
            self._audit(db, actor, "dataset.replica_remove_requested", replica_ref, {}, timestamp)
            current = db.execute(self._REPLICA_SELECT + " WHERE replica.id=?", (row["id"],)).fetchone()
            assert current is not None
            return self._safe_dataset_replica(current)

    def claim_node_commands(
        self, node_ref: str, worker_token: str, worker_instance_id: str, limit: int
    ) -> list[dict[str, Any]]:
        worker_token_digest = self._token_digest(worker_token)
        timestamp = now_iso()
        with self.transaction() as db:
            node = db.execute("SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)).fetchone()
            if (
                node is None
                or node["worker_token_sha256"] is None
                or not secrets.compare_digest(
                    str(node["worker_token_sha256"]), worker_token_digest
                )
                or node["worker_instance_id"] != worker_instance_id
            ):
                raise TrainingForbiddenError(
                    "worker_authentication_failed",
                    "The Training Worker credential is invalid.",
                )
            stale = db.execute(
                """SELECT * FROM training_node_commands
                WHERE node_id=? AND status='running' AND lease_expires_at<?
                ORDER BY id""",
                (node["id"], timestamp),
            ).fetchall()
            for command in stale:
                if command["kind"] == "transfer_dataset":
                    request = json.loads(command["request_json"])
                    transfer = db.execute(
                        "SELECT status FROM dataset_transfers WHERE transfer_ref=?",
                        (request["transfer_ref"],),
                    ).fetchone()
                    if transfer is None or transfer["status"] not in {"queued", "running"}:
                        self._settle_abandoned_transfer_command(
                            db, command, request["transfer_ref"], transfer, timestamp
                        )
                        continue
                db.execute(
                    """UPDATE training_node_commands SET status='queued',worker_instance_id=NULL,
                    claim_token_sha256=NULL,lease_expires_at=NULL,started_at=NULL,
                    updated_at=? WHERE id=?""",
                    (timestamp, command["id"]),
                )
            rows = db.execute(
                """SELECT * FROM training_node_commands AS candidate
                WHERE candidate.node_id=? AND candidate.status='queued'
                AND (
                  candidate.kind!='transfer_dataset'
                  OR NOT EXISTS (
                    SELECT 1 FROM training_node_commands AS active
                    WHERE active.node_id=candidate.node_id
                      AND active.kind='transfer_dataset'
                      AND active.status='running'
                  )
                )
                ORDER BY id LIMIT ?""", (node["id"], limit)
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            claimed_transfer = False
            for row in rows:
                if row["kind"] == "transfer_dataset" and claimed_transfer:
                    continue
                if row["kind"] == "transfer_dataset":
                    request = json.loads(row["request_json"])
                    transfer = db.execute(
                        "SELECT status FROM dataset_transfers WHERE transfer_ref=?",
                        (request["transfer_ref"],),
                    ).fetchone()
                    if transfer is None or transfer["status"] not in {"queued", "running"}:
                        self._settle_abandoned_transfer_command(
                            db, row, request["transfer_ref"], transfer, timestamp
                        )
                        continue
                if db.execute(
                    """UPDATE training_node_commands SET status='running',worker_instance_id=?,
                    claim_token_sha256=?,lease_expires_at=?,
                    started_at=COALESCE(started_at,?),updated_at=?
                    WHERE id=? AND status='queued'""",
                    (
                        worker_instance_id,
                        self._token_digest(claim_token := "claim_" + secrets.token_urlsafe(32)),
                        (
                            datetime.now(UTC)
                            + timedelta(
                                minutes=30
                                if row["kind"] == "transfer_dataset"
                                else 1.5
                            )
                        ).isoformat(timespec="milliseconds"),
                        timestamp,
                        timestamp,
                        row["id"],
                    ),
                ).rowcount:
                    claimed.append(
                        {
                            "command_ref": row["command_ref"],
                            "claim_token": claim_token,
                            "kind": row["kind"],
                            "payload": json.loads(row["request_json"]),
                        }
                    )
                    claimed_transfer = claimed_transfer or row["kind"] == "transfer_dataset"
            return claimed

    def _settle_abandoned_transfer_command(
        self,
        db: sqlite3.Connection,
        command: sqlite3.Row,
        transfer_ref: str,
        transfer: sqlite3.Row | None,
        timestamp: str,
    ) -> None:
        result = {
            "status": "cancelled",
            "transfer_ref": transfer_ref,
            "reason": "transfer_no_longer_active",
        }
        db.execute(
            """UPDATE training_node_commands SET status='cancelled',result_json=?,
            worker_instance_id=NULL,claim_token_sha256=NULL,
            lease_expires_at=NULL,finished_at=?,updated_at=?
            WHERE id=?""",
            (canonical_json(result), timestamp, timestamp, command["id"]),
        )
        if transfer is not None and transfer["status"] in {"pause_requested", "cancel_requested"}:
            if transfer["status"] == "pause_requested":
                terminal_status = "paused"
                error_code = None
                error_message = None
            else:
                terminal_status = "failed"
                error_code = "dataset_partial_cleanup_unconfirmed"
                error_message = "The Worker could not confirm removal of the partial dataset."
            db.execute(
                """UPDATE dataset_transfers SET status=?,error_code=?,error_message=?,
                finished_at=?,updated_at=? WHERE transfer_ref=?""",
                (terminal_status, error_code, error_message, timestamp, timestamp, transfer_ref),
            )
            self._event(
                db,
                "dataset.transfer.updated",
                None,
                {"transfer_ref": transfer_ref, "status": terminal_status},
                timestamp,
            )

    def finish_node_command(
        self,
        node_ref: str,
        worker_token: str,
        command_ref: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        worker_instance_id = data["worker_instance_id"]
        worker_token_digest = self._token_digest(worker_token)
        claim_token_digest = self._token_digest(data["claim_token"])
        timestamp = now_iso()
        with self.transaction() as db:
            command = db.execute(
                """SELECT command.*,node.node_ref,
                node.worker_token_sha256 AS current_worker_token_sha256,
                node.worker_instance_id AS current_worker_instance_id
                FROM training_node_commands AS command
                LEFT JOIN training_nodes AS node ON node.id=command.node_id
                WHERE command.command_ref=? AND command.node_ref_snapshot=?""",
                (command_ref, node_ref),
            ).fetchone()
            if command is None:
                raise TrainingNotFoundError("training_node_command_not_found", "The Worker command was not found.")
            if (
                command["current_worker_token_sha256"] is None
                or not secrets.compare_digest(
                    str(command["current_worker_token_sha256"]),
                    worker_token_digest,
                )
                or command["current_worker_instance_id"] != worker_instance_id
            ):
                raise TrainingForbiddenError(
                    "worker_authentication_failed",
                    "The Training Worker credential is invalid.",
                )
            if (
                command["status"] != "running"
                or command["worker_instance_id"] != worker_instance_id
                or command["claim_token_sha256"] is None
                or not secrets.compare_digest(
                    str(command["claim_token_sha256"]), claim_token_digest
                )
            ):
                raise TrainingConflictError(
                    "training_node_command_claim_stale",
                    "This Worker command claim is no longer active.",
                )
            result_status = data["status"]
            if result_status == "running":
                self._update_running_command(db, command, data, timestamp)
                return {"command_ref": command_ref, "status": "running", "accepted_at": timestamp}
            final = "succeeded" if result_status == "succeeded" else "cancelled" if result_status in {"paused", "cancelled"} else "failed"
            db.execute(
                """UPDATE training_node_commands SET status=?,result_json=?,lease_expires_at=NULL,
                claim_token_sha256=NULL,finished_at=?,updated_at=? WHERE id=?""",
                (final, canonical_json(data), timestamp, timestamp, command["id"]),
            )
            self._apply_terminal_command_result(db, command, data, final, timestamp)
            return {"command_ref": command_ref, "status": final, "accepted_at": timestamp}

    def _update_running_command(self, db: sqlite3.Connection, command: sqlite3.Row, data: dict[str, Any], timestamp: str) -> None:
        expiry = (
            datetime.now(UTC)
            + timedelta(minutes=30 if command["kind"] == "transfer_dataset" else 1.5)
        ).isoformat(timespec="milliseconds")
        db.execute("UPDATE training_node_commands SET result_json=?,lease_expires_at=?,updated_at=? WHERE id=?", (canonical_json(data), expiry, timestamp, command["id"]))
        if command["kind"] == "transfer_dataset":
            progress = data.get("progress") or {}
            request = json.loads(command["request_json"])
            db.execute(
                """UPDATE dataset_transfers SET
                status=CASE WHEN status IN ('pause_requested','cancel_requested') THEN status ELSE 'running' END,
                bytes_transferred=?,
                files_completed=?,started_at=COALESCE(started_at,?),updated_at=?
                WHERE transfer_ref=? AND status IN ('queued','running','pause_requested','cancel_requested')""",
                (int(progress.get("bytes_transferred", 0)), int(progress.get("files_completed", 0)), timestamp, timestamp, request["transfer_ref"]),
            )
            current = db.execute(
                "SELECT status FROM dataset_transfers WHERE transfer_ref=?",
                (request["transfer_ref"],),
            ).fetchone()
            if current is not None:
                self._event(
                    db,
                    "dataset.transfer.updated",
                    None,
                    {
                        "transfer_ref": request["transfer_ref"],
                        "status": current["status"],
                    },
                    timestamp,
                )

    def _apply_terminal_command_result(self, db: sqlite3.Connection, command: sqlite3.Row, data: dict[str, Any], final: str, timestamp: str) -> None:
        request = json.loads(command["request_json"])
        error = data.get("error") or {}
        if command["kind"] == "transfer_dataset":
            transfer = db.execute(
                self._TRANSFER_SELECT + " WHERE transfer.transfer_ref=?", (request["transfer_ref"],)
            ).fetchone()
            if transfer is None:
                return
            result_status = data.get("status")
            transfer_status = "succeeded" if final == "succeeded" else result_status if result_status in {"paused", "cancelled"} else "failed"
            progress = data.get("progress") or {}
            db.execute(
                """UPDATE dataset_transfers SET status=?,bytes_transferred=?,files_completed=?,
                error_code=?,error_message=?,finished_at=?,updated_at=? WHERE id=?""",
                (transfer_status, int(progress.get("bytes_transferred", transfer["total_bytes"] if final == "succeeded" else transfer["bytes_transferred"])), int(progress.get("files_completed", transfer["file_count"] if final == "succeeded" else transfer["files_completed"])), error.get("code"), error.get("message"), timestamp, timestamp, transfer["id"]),
            )
            if final == "succeeded":
                replica_data = data.get("replica") or {}
                replica_ref = new_ref("replica")
                db.execute(
                    """INSERT INTO dataset_replicas(replica_ref,node_id,node_ref_snapshot,
                    source_manifest_id,status,local_root,inventory_sha256,file_count,total_bytes,
                    created_at,updated_at) VALUES(?,?,?,?,'ready',?,?,?,?,?,?)
                    ON CONFLICT(node_id,source_manifest_id) DO UPDATE SET status='ready',
                    local_root=excluded.local_root,inventory_sha256=excluded.inventory_sha256,
                    file_count=excluded.file_count,total_bytes=excluded.total_bytes,updated_at=excluded.updated_at""",
                    (replica_ref, transfer["node_id"], transfer["node_ref_snapshot"], transfer["source_manifest_id"], replica_data.get("local_root", transfer["final_directory"]), replica_data.get("inventory_sha256", transfer["inventory_sha256"]), int(replica_data.get("file_count", transfer["file_count"])), int(replica_data.get("total_bytes", transfer["total_bytes"])), timestamp, timestamp),
                )
                self._event(db, "dataset.replica.ready", None, {"transfer_ref": request["transfer_ref"], "release_ref": transfer["release_ref"]}, timestamp)
            self._event(db, "dataset.transfer.updated", None, {"transfer_ref": request["transfer_ref"], "status": transfer_status}, timestamp)
        elif command["kind"] == "remove_dataset_replica":
            if final == "succeeded":
                db.execute("DELETE FROM dataset_replicas WHERE replica_ref=?", (request["replica_ref"],))
                self._event(db, "dataset.replica.removed", None, {"replica_ref": request["replica_ref"]}, timestamp)
            else:
                db.execute("UPDATE dataset_replicas SET status='ready',updated_at=? WHERE replica_ref=?", (timestamp, request["replica_ref"]))
        elif command["kind"] == "cancel_dataset_transfer" and final == "succeeded":
            action = request.get("action", "pause")
            requested_status = "cancel_requested" if action == "cancel" else "pause_requested"
            terminal_status = "cancelled" if action == "cancel" else "paused"
            updated = db.execute(
                """UPDATE dataset_transfers SET status=?,finished_at=?,updated_at=?
                WHERE transfer_ref=? AND status=?""",
                (terminal_status, timestamp, timestamp, request["transfer_ref"], requested_status),
            ).rowcount
            if updated:
                self._event(db, "dataset.transfer.updated", None, {"transfer_ref": request["transfer_ref"], "status": terminal_status}, timestamp)

    @staticmethod
    def _insert_node_command(db: sqlite3.Connection, node: sqlite3.Row, kind: str, request: dict[str, Any], timestamp: str) -> str:
        command_ref = new_ref("command")
        db.execute(
            """INSERT INTO training_node_commands(command_ref,node_id,node_ref_snapshot,
            kind,status,request_json,created_at,updated_at)
            VALUES(?,?,?,?,'queued',?,?,?)""",
            (command_ref, node["id"], node["node_ref"], kind, canonical_json(request), timestamp, timestamp),
        )
        return command_ref

    @staticmethod
    def _require_online_node_in(db: sqlite3.Connection, node_ref: str) -> sqlite3.Row:
        node = db.execute("SELECT * FROM training_nodes WHERE node_ref=?", (node_ref,)).fetchone()
        if node is None:
            raise TrainingNotFoundError("training_node_not_found", "Training node was not found.")
        if node["status"] not in {TrainingNodeStatus.ONLINE.value, TrainingNodeStatus.DEGRADED.value}:
            raise TrainingConflictError("training_node_unavailable", "The Training Worker node must be online.")
        return node

    def active_gpu_leases(self) -> dict[str, str]:
        with self.connection() as db:
            return {row["gpu_uuid"]: row["run_ref"] for row in db.execute("SELECT g.gpu_uuid,r.run_ref FROM gpu_leases g JOIN training_runs r ON r.id=g.run_id")}

    @staticmethod
    def _log(db: sqlite3.Connection, run_id: int, level: str, message: str, timestamp: str, stage_id: int | None = None) -> int:
        seq = int(db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM run_logs WHERE run_id=?", (run_id,)).fetchone()[0])
        db.execute("INSERT INTO run_logs(run_id,seq,level,message,created_at,stage_id) VALUES(?,?,?,?,?,?)", (run_id, seq, level, message, timestamp, stage_id)); return seq

    @staticmethod
    def _event(db: sqlite3.Connection, event_type: str, run_ref: str, payload: dict[str, Any], timestamp: str) -> None:
        db.execute("INSERT INTO training_events(event_type,run_ref,payload_json,created_at) VALUES(?,?,?,?)", (event_type, run_ref, canonical_json(payload), timestamp))

    @staticmethod
    def _audit(db: sqlite3.Connection, actor: str, action: str, target_ref: str, payload: dict[str, Any], timestamp: str) -> None:
        db.execute("INSERT INTO audit_events(actor,action,target_ref,payload_json,created_at) VALUES(?,?,?,?,?)", (actor, action, target_ref, canonical_json(payload), timestamp))

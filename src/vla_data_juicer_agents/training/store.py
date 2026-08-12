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
            "ssh_username": row["ssh_username"],
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
                    data["ssh_username"],
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
                state_revision=state_revision+1,updated_at=? WHERE id=?""",
                (
                    "succeeded" if succeeded else "failed",
                    safe_message,
                    timestamp,
                    worker_version,
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
                status='online',state_revision=state_revision+1,
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
                status=?,state_revision=state_revision+1,last_heartbeat_at=?,
                worker_version=?,protocol_version=?,health_message=?,
                capabilities_json=?,updated_at=? WHERE id=?""",
                (
                    status,
                    timestamp,
                    data["worker_version"],
                    data["protocol_version"],
                    data.get("health_message"),
                    capabilities_json,
                    timestamp,
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
        model_ref, revision_ref = new_ref("model"), new_ref("mrev")
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO registered_models(model_ref,name,description,status,current_revision,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                (model_ref, data["name"], data.get("description") or "", "draft", timestamp, timestamp),
            )
            model_id = int(cursor.lastrowid)
            self._insert_revision(db, model_id, revision_ref, 1, data, timestamp)
            self._audit(db, actor, "model.created", model_ref, {"revision": 1}, timestamp)
        return self.get_model(model_ref, include_private=True)

    def update_model(self, model_ref: str, expected_revision: int, data: dict[str, Any], actor: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM registered_models WHERE model_ref=?", (model_ref,)).fetchone()
            if row is None:
                raise TrainingNotFoundError("model_not_found", "Training model was not found.")
            if row["status"] != "draft":
                raise TrainingConflictError("model_not_editable", "Only draft models can be edited.")
            if row["current_revision"] != expected_revision:
                raise TrainingConflictError("model_revision_conflict", "The model was edited by another request.", current={"revision": row["current_revision"]})
            revision = expected_revision + 1
            revision_ref = new_ref("mrev")
            merged = dict(data)
            if merged.get("name") is None:
                merged["name"] = row["name"]
            db.execute(
                "UPDATE registered_models SET name=?,description=?,current_revision=?,updated_at=? WHERE id=?",
                (merged["name"], merged.get("description") or "", revision, timestamp, row["id"]),
            )
            self._insert_revision(db, row["id"], revision_ref, revision, merged, timestamp)
            self._audit(db, actor, "model.updated", model_ref, {"revision": revision}, timestamp)
        return self.get_model(model_ref, include_private=True)

    def _insert_revision(self, db: sqlite3.Connection, model_id: int, revision_ref: str, revision: int, data: dict[str, Any], timestamp: str) -> None:
        template = data["launch_template"]
        db.execute(
            """INSERT INTO model_revisions(revision_ref,model_id,revision_number,working_directory,entrypoint,fixed_argv_json,output_template,parameter_definitions_json,launch_template_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (revision_ref, model_id, revision, template["working_directory"], template["entrypoint"], canonical_json(template.get("fixed_argv", [])), f"{template['output_root'].rstrip('/')}/{{run_ref}}", canonical_json(data["parameter_definitions"]), canonical_json(template), timestamp),
        )

    def list_models(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM registered_models ORDER BY id DESC").fetchall()
        return [self._safe_model(row) for row in rows]

    def get_model(self, model_ref: str, *, revision: int | None = None, include_private: bool = False) -> dict[str, Any]:
        with self.connection() as db:
            model = db.execute("SELECT * FROM registered_models WHERE model_ref=?", (model_ref,)).fetchone()
            if model is None:
                raise TrainingNotFoundError("model_not_found", "Training model was not found.")
            number = revision or int(model["current_revision"])
            rev = db.execute("SELECT * FROM model_revisions WHERE model_id=? AND revision_number=?", (model["id"], number)).fetchone()
        result = self._safe_model(model)
        result["revision"] = number
        result["revision_ref"] = rev["revision_ref"]
        result["parameter_definitions"] = json.loads(rev["parameter_definitions_json"])
        result["launch_template"] = json.loads(rev["launch_template_json"]) if include_private else {
            "domain": json.loads(rev["launch_template_json"])["domain"],
            "server_ref": json.loads(rev["launch_template_json"])["server_ref"],
        }
        if include_private:
            result["output_template"] = rev["output_template"]
        return result

    def get_model_record(self, model_ref: str, revision: int | None = None) -> dict[str, Any]:
        result = self.get_model(model_ref, revision=revision, include_private=True)
        return result

    @staticmethod
    def _safe_model(row: sqlite3.Row) -> dict[str, Any]:
        return {"model_ref": row["model_ref"], "name": row["name"], "description": row["description"], "status": row["status"], "revision": row["current_revision"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

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

    def create_run(self, *, data: dict[str, Any], run_spec_builder: Any, idempotency_key: str, actor: str) -> dict[str, Any]:
        digest = payload_hash(data)
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
            model = db.execute("SELECT id FROM registered_models WHERE model_ref=?", (data["model_ref"],)).fetchone()
            revision = db.execute("SELECT id FROM model_revisions WHERE revision_ref=?", (data["revision_ref"],)).fetchone()
            port = self.find_available_port(data["server_ref"], db=db)
            run_ref = new_ref("run")
            spec = run_spec_builder(run_ref, port)
            seed = int(hashlib.sha256(run_ref.encode()).hexdigest()[:8], 16)
            total_steps = int(data.get("total_steps", 20))
            cursor = db.execute(
                """INSERT INTO training_runs(run_ref,model_id,model_revision_id,mode,server_ref,gpu_uuids_json,parameters_json,run_spec_json,command_preview,status,state_revision,seed,total_steps,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'queued',1,?,?,?,?)""",
                (run_ref, model["id"], revision["id"], "simulation", data["server_ref"], canonical_json(data["gpu_uuids"]), canonical_json(data["parameters"]), canonical_json(spec["private_spec"]), spec["command_preview"], seed, total_steps, timestamp, timestamp),
            )
            run_id = int(cursor.lastrowid)
            db.executemany("INSERT INTO gpu_leases(gpu_uuid,run_id,acquired_at) VALUES(?,?,?)", [(gpu, run_id, timestamp) for gpu in data["gpu_uuids"]])
            db.execute("INSERT INTO port_leases(server_ref,master_port,run_id,acquired_at) VALUES(?,?,?,?)", (data["server_ref"], port, run_id, timestamp))
            db.execute("INSERT INTO training_idempotency(scope,idempotency_key,payload_sha256,response_ref,created_at) VALUES('create_run',?,?,?,?)", (idempotency_key, digest, run_ref, timestamp))
            self._log(db, run_id, "info", "Simulation run queued.", timestamp)
            self._event(db, "run.updated", run_ref, {}, timestamp)
            self._audit(db, actor, "run.created", run_ref, {"mode": "simulation"}, timestamp)
            return self._get_run_in(db, run_ref)

    def list_runs(self, *, status: str | None, after: str | None, limit: int) -> dict[str, Any]:
        clauses, values = [], []
        if status:
            clauses.append("status=?"); values.append(status)
        if after:
            try: cursor_id = int(after)
            except ValueError: cursor_id = 0
            clauses.append("id<?"); values.append(cursor_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM training_runs {where} ORDER BY id DESC LIMIT ?", (*values, limit + 1)).fetchall()
        return {"items": [self._safe_run(row) for row in rows[:limit]], "next_after": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    def get_run(self, run_ref: str) -> dict[str, Any]:
        with self.connection() as db:
            return self._get_run_in(db, run_ref)

    def _get_run_in(self, db: sqlite3.Connection, run_ref: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM training_runs WHERE run_ref=?", (run_ref,)).fetchone()
        if row is None:
            raise TrainingNotFoundError("run_not_found", "Training run was not found.")
        return self._safe_run(row)

    @staticmethod
    def _safe_run(row: sqlite3.Row) -> dict[str, Any]:
        spec = json.loads(row["run_spec_json"])
        params = spec.get("parameters", {})
        sensitive = set(spec.get("sensitive_parameters", []))
        return {"run_ref": row["run_ref"], "status": row["status"], "state_revision": row["state_revision"], "mode": row["mode"], "server_ref": row["server_ref"], "gpu_uuids": json.loads(row["gpu_uuids_json"]), "model_ref": spec["model_ref"], "model_name": spec.get("model_name", spec["model_ref"]), "model_revision": spec["model_revision"], "parameters": {key: "********" if key in sensitive else value for key, value in params.items()}, "run_spec": spec.get("public_spec"), "command_preview": spec.get("safe_command_preview", ""), "current_step": row["current_step"], "total_steps": row["total_steps"], "progress": row["current_step"] / row["total_steps"] if row["total_steps"] else 0, "failure": {"code": row["failure_code"], "message": row["failure_message"]} if row["failure_code"] else None, "created_at": row["created_at"], "updated_at": row["updated_at"], "started_at": row["started_at"], "finished_at": row["finished_at"]}

    def stop_run(self, run_ref: str, expected_revision: int, idempotency_key: str, actor: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            idem = db.execute("SELECT response_ref FROM training_idempotency WHERE scope='stop_run' AND idempotency_key=?", (idempotency_key,)).fetchone()
            if idem is not None:
                return self._get_run_in(db, idem["response_ref"])
            row = db.execute("SELECT * FROM training_runs WHERE run_ref=?", (run_ref,)).fetchone()
            if row is None:
                raise TrainingNotFoundError("run_not_found", "Training run was not found.")
            if row["state_revision"] != expected_revision:
                raise TrainingConflictError("run_revision_conflict", "The run state changed.", current=self._safe_run(row))
            if RunStatus(row["status"]) in TERMINAL_RUN_STATUSES:
                raise TrainingConflictError("run_already_finished", "The run is already finished.", current=self._safe_run(row))
            if row["status"] == "queued":
                result = self._finish_in(db, row, "cancelled", timestamp)
            else:
                db.execute("UPDATE training_runs SET status='stop_requested',state_revision=state_revision+1,updated_at=? WHERE id=?", (timestamp, row["id"]))
                self._event(db, "run.updated", run_ref, {}, timestamp)
                result = self._get_run_in(db, run_ref)
            db.execute("INSERT INTO training_idempotency(scope,idempotency_key,payload_sha256,response_ref,created_at) VALUES('stop_run',?,?,?,?)", (idempotency_key, payload_hash({"run_ref": run_ref, "expected_revision": expected_revision}), run_ref, timestamp))
            self._audit(db, actor, "run.stop_requested", run_ref, {}, timestamp)
            return result

    def claim_next_run(self, worker_id: str, lease_seconds: float = 10) -> dict[str, Any] | None:
        timestamp = now_iso(); expiry = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(timespec="milliseconds")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM training_runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None: return None
            db.execute("UPDATE training_runs SET status='preparing',state_revision=state_revision+1,owner_id=?,owner_epoch=owner_epoch+1,lease_expires_at=?,heartbeat_at=?,updated_at=? WHERE id=?", (worker_id, expiry, timestamp, timestamp, row["id"]))
            self._event(db, "run.updated", row["run_ref"], {}, timestamp)
            return self._get_run_in(db, row["run_ref"])

    def transition_running(self, run_ref: str, worker_id: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM training_runs WHERE run_ref=? AND owner_id=?", (run_ref, worker_id)).fetchone()
            if row is None: raise TrainingConflictError("worker_lease_lost", "Worker no longer owns this run.")
            if row["status"] == "stop_requested": return self._finish_in(db, row, "cancelled", timestamp)
            if row["status"] != "preparing": raise TrainingConflictError("invalid_run_transition", "Run cannot enter running state.")
            db.execute("UPDATE training_runs SET status='running',state_revision=state_revision+1,started_at=?,updated_at=? WHERE id=?", (timestamp, timestamp, row["id"]))
            self._log(db, row["id"], "info", "Simulation training started.", timestamp)
            self._event(db, "run.updated", run_ref, {}, timestamp)
            return self._get_run_in(db, run_ref)

    def append_step(self, run_ref: str, worker_id: str, metric: dict[str, Any], message: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM training_runs WHERE run_ref=? AND owner_id=?", (run_ref, worker_id)).fetchone()
            if row is None: raise TrainingConflictError("worker_lease_lost", "Worker no longer owns this run.")
            if row["status"] == "stop_requested": return self._finish_in(db, row, "cancelled", timestamp)
            if row["status"] != "running": return self._safe_run(row)
            seq = int(db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM metric_samples WHERE run_id=?", (row["id"],)).fetchone()[0])
            db.execute("""INSERT INTO metric_samples(run_id,seq,step,total_steps,epoch,loss,learning_rate,grad_norm,elapsed_seconds,gpu_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (row["id"], seq, metric["step"], metric["total_steps"], metric["epoch"], metric["loss"], metric["learning_rate"], metric["grad_norm"], metric["elapsed_seconds"], canonical_json(metric["gpus"]), timestamp))
            db.execute("UPDATE training_runs SET current_step=?,heartbeat_at=?,lease_expires_at=?,updated_at=? WHERE id=?", (metric["step"], timestamp, (datetime.now(UTC)+timedelta(seconds=10)).isoformat(timespec="milliseconds"), timestamp, row["id"]))
            log_seq = self._log(db, row["id"], "info", message, timestamp)
            self._event(db, "run.metric.appended", run_ref, {"item_seq": seq}, timestamp)
            self._event(db, "run.log.appended", run_ref, {"item_seq": log_seq}, timestamp)
            return self._get_run_in(db, run_ref)

    def finish_run(self, run_ref: str, worker_id: str, status: str = "succeeded", failure_code: str | None = None, failure_message: str | None = None) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM training_runs WHERE run_ref=? AND owner_id=?", (run_ref, worker_id)).fetchone()
            if row is None: raise TrainingConflictError("worker_lease_lost", "Worker no longer owns this run.")
            if row["status"] == "stop_requested": status = "cancelled"
            return self._finish_in(db, row, status, timestamp, failure_code, failure_message)

    def _finish_in(self, db: sqlite3.Connection, row: sqlite3.Row, status: str, timestamp: str, failure_code: str | None = None, failure_message: str | None = None) -> dict[str, Any]:
        db.execute("UPDATE training_runs SET status=?,state_revision=state_revision+1,owner_id=NULL,lease_expires_at=NULL,finished_at=?,updated_at=?,failure_code=?,failure_message=? WHERE id=?", (status, timestamp, timestamp, failure_code, failure_message, row["id"]))
        db.execute("DELETE FROM gpu_leases WHERE run_id=?", (row["id"],)); db.execute("DELETE FROM port_leases WHERE run_id=?", (row["id"],))
        self._log(db, row["id"], "info" if status in {"succeeded", "cancelled"} else "error", f"Simulation run {status}.", timestamp)
        self._event(db, "run.updated", row["run_ref"], {}, timestamp)
        return self._get_run_in(db, row["run_ref"])

    def recover_stale_runs(self) -> int:
        timestamp = now_iso()
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM training_runs WHERE status IN ('preparing','running','stop_requested') AND lease_expires_at IS NOT NULL AND lease_expires_at<?", (timestamp,)).fetchall()
            for row in rows:
                self._finish_in(db, row, "lost", timestamp, "worker_lease_expired", "The simulation worker lease expired.")
            return len(rows)

    def list_logs(self, run_ref: str, after_seq: int, limit: int) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT id FROM training_runs WHERE run_ref=?", (run_ref,)).fetchone()
            if row is None: raise TrainingNotFoundError("run_not_found", "Training run was not found.")
            items = [dict(item) for item in db.execute("SELECT seq,level,message,created_at FROM run_logs WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (row["id"], after_seq, limit + 1)).fetchall()]
        return {"items": items[:limit], "next_after": items[limit-1]["seq"] if len(items) > limit else None}

    def list_metrics(self, run_ref: str, after_seq: int, limit: int) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT id FROM training_runs WHERE run_ref=?", (run_ref,)).fetchone()
            if row is None: raise TrainingNotFoundError("run_not_found", "Training run was not found.")
            rows = db.execute("SELECT * FROM metric_samples WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (row["id"], after_seq, limit + 1)).fetchall()
        items = [{**{key: item[key] for key in ("seq","step","total_steps","epoch","loss","learning_rate","grad_norm","elapsed_seconds","created_at")}, "gpus": json.loads(item["gpu_json"])} for item in rows]
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

    def active_gpu_leases(self) -> dict[str, str]:
        with self.connection() as db:
            return {row["gpu_uuid"]: row["run_ref"] for row in db.execute("SELECT g.gpu_uuid,r.run_ref FROM gpu_leases g JOIN training_runs r ON r.id=g.run_id")}

    @staticmethod
    def _log(db: sqlite3.Connection, run_id: int, level: str, message: str, timestamp: str) -> int:
        seq = int(db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM run_logs WHERE run_id=?", (run_id,)).fetchone()[0])
        db.execute("INSERT INTO run_logs(run_id,seq,level,message,created_at) VALUES(?,?,?,?,?)", (run_id, seq, level, message, timestamp)); return seq

    @staticmethod
    def _event(db: sqlite3.Connection, event_type: str, run_ref: str, payload: dict[str, Any], timestamp: str) -> None:
        db.execute("INSERT INTO training_events(event_type,run_ref,payload_json,created_at) VALUES(?,?,?,?)", (event_type, run_ref, canonical_json(payload), timestamp))

    @staticmethod
    def _audit(db: sqlite3.Connection, actor: str, action: str, target_ref: str, payload: dict[str, Any], timestamp: str) -> None:
        db.execute("INSERT INTO audit_events(actor,action,target_ref,payload_json,created_at) VALUES(?,?,?,?,?)", (actor, action, target_ref, canonical_json(payload), timestamp))

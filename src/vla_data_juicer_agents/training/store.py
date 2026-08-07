from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .errors import TrainingConflictError, TrainingNotFoundError
from .migrations import apply_training_migrations
from .models import RunStatus, TERMINAL_RUN_STATUSES


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

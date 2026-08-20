from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Literal, Protocol, Sequence


ACTIVE_RUN_STATES = ("running", "stopping")
KNOWN_RUN_STATES = (
    "accepted",
    "running",
    "stopping",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
)


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    pid: int
    process_start_marker: str | None
    argv_digest: str | None


ProbeStatus = Literal["matched", "missing", "mismatch", "unverifiable"]


@dataclass(frozen=True, slots=True)
class ProcessProbeResult:
    status: ProbeStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    run_ref: str
    status: ProbeStatus
    previous_state: str
    current_state: str
    detail: str

    def to_payload(self) -> dict[str, str]:
        return asdict(self)


class ProcessProbe(Protocol):
    def inspect(self, observation: ProcessObservation) -> ProcessProbeResult: ...


class LocalProcessProbe:
    """Read-only Linux process identity probe.

    The probe never sends a terminating signal.  On Linux it compares both the
    process start marker and argv digest so a recycled PID is not reattached.
    Other platforms conservatively report the process as unverifiable.
    """

    def inspect(self, observation: ProcessObservation) -> ProcessProbeResult:
        proc_dir = Path("/proc") / str(observation.pid)
        if proc_dir.exists():
            current_start = _read_linux_process_start_marker(proc_dir)
            current_digest = _read_linux_argv_digest(proc_dir)
            if current_start is None or current_digest is None:
                return ProcessProbeResult("unverifiable", "process identity cannot be read")
            if observation.process_start_marker != current_start:
                return ProcessProbeResult("mismatch", "process start marker changed")
            if observation.argv_digest != current_digest:
                return ProcessProbeResult("mismatch", "process argv digest changed")
            return ProcessProbeResult("matched", "process identity matched local ledger")

        try:
            os.kill(observation.pid, 0)
        except ProcessLookupError:
            return ProcessProbeResult("missing", "process no longer exists")
        except PermissionError:
            return ProcessProbeResult("unverifiable", "process exists but cannot be inspected")
        except OSError:
            return ProcessProbeResult("unverifiable", "process status cannot be inspected")
        return ProcessProbeResult(
            "unverifiable",
            "process exists but this operating system cannot verify its identity",
        )


class WorkerLedger:
    """Node-local durable observations used after worker restarts.

    Schema v2 also persists the supervisor identity, log cursor and a durable
    update outbox.  Process signalling remains in the execution manager; the
    ledger itself is only a state and delivery journal.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_ledger_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_runs (
                    run_ref TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN (
                        'accepted', 'running', 'stopping', 'succeeded',
                        'failed', 'cancelled', 'unknown'
                    )),
                    pid INTEGER,
                    process_start_marker TEXT,
                    argv_digest TEXT,
                    gpu_uuids_json TEXT NOT NULL DEFAULT '[]',
                    working_directory TEXT,
                    stdout_path TEXT,
                    stderr_path TEXT,
                    last_reconciliation TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO worker_ledger_metadata(key, value)
                VALUES ('schema_version', '1');
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(worker_runs)")
            }
            additions = {
                "stage_ref": "TEXT",
                "stage_number": "INTEGER",
                "owner_epoch": "INTEGER NOT NULL DEFAULT 0",
                "worker_seq": "INTEGER NOT NULL DEFAULT 0",
                "version_label": "TEXT",
                "output_directory": "TEXT",
                "supervisor_state_path": "TEXT",
                "monitoring_format": "TEXT NOT NULL DEFAULT 'plain'",
                "log_offset": "INTEGER NOT NULL DEFAULT 0",
                "redactions_json": "TEXT NOT NULL DEFAULT '[]'",
                "launch_token": "TEXT",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE worker_runs ADD COLUMN {name} {declaration}"
                    )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_run_updates (
                    update_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_ref TEXT NOT NULL,
                    owner_epoch INTEGER NOT NULL,
                    worker_seq INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_ref, owner_epoch, worker_seq)
                );
                CREATE INDEX IF NOT EXISTS idx_worker_run_updates_delivery
                ON worker_run_updates(run_ref, owner_epoch, worker_seq);
                UPDATE worker_ledger_metadata SET value = '2'
                WHERE key = 'schema_version';
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def record_process_observation(
        self,
        *,
        run_ref: str,
        state: str,
        pid: int | None = None,
        process_start_marker: str | None = None,
        argv_digest: str | None = None,
        gpu_uuids: Sequence[str] = (),
        working_directory: str | None = None,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
        stage_ref: str | None = None,
        stage_number: int | None = None,
        owner_epoch: int = 0,
        version_label: str | None = None,
        output_directory: str | None = None,
        supervisor_state_path: str | None = None,
        monitoring_format: str = "plain",
        log_offset: int = 0,
        redactions: Sequence[str] = (),
        launch_token: str | None = None,
    ) -> None:
        if not run_ref:
            raise ValueError("run_ref must not be empty")
        if state not in KNOWN_RUN_STATES:
            raise ValueError(f"unsupported worker run state: {state}")
        if state in ACTIVE_RUN_STATES and (pid is None or pid <= 0):
            raise ValueError("an active process observation requires a positive pid")
        if pid is not None and pid <= 0:
            raise ValueError("pid must be positive")
        if owner_epoch < 0:
            raise ValueError("owner_epoch must not be negative")
        if stage_number is not None and stage_number < 1:
            raise ValueError("stage_number must be positive")
        if monitoring_format not in {"plain", "transformers", "jsonl"}:
            raise ValueError("monitoring_format is unsupported")
        if log_offset < 0:
            raise ValueError("log_offset must not be negative")
        sampled_at = datetime.now(timezone.utc).isoformat()
        gpu_payload = json.dumps(sorted(set(gpu_uuids)))
        redactions_payload = json.dumps(sorted(set(redactions)))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_runs(
                    run_ref, state, pid, process_start_marker, argv_digest,
                    gpu_uuids_json, working_directory, stdout_path, stderr_path,
                    last_reconciliation, created_at, updated_at, stage_ref,
                    stage_number, owner_epoch, version_label, output_directory,
                    supervisor_state_path, monitoring_format, log_offset,
                    redactions_json, launch_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_ref) DO UPDATE SET
                    state = excluded.state,
                    pid = excluded.pid,
                    process_start_marker = excluded.process_start_marker,
                    argv_digest = excluded.argv_digest,
                    gpu_uuids_json = excluded.gpu_uuids_json,
                    working_directory = excluded.working_directory,
                    stdout_path = excluded.stdout_path,
                    stderr_path = excluded.stderr_path,
                    stage_ref = excluded.stage_ref,
                    stage_number = excluded.stage_number,
                    owner_epoch = excluded.owner_epoch,
                    version_label = excluded.version_label,
                    output_directory = excluded.output_directory,
                    supervisor_state_path = excluded.supervisor_state_path,
                    monitoring_format = excluded.monitoring_format,
                    log_offset = excluded.log_offset,
                    redactions_json = excluded.redactions_json,
                    launch_token = excluded.launch_token,
                    updated_at = excluded.updated_at
                """,
                (
                    run_ref,
                    state,
                    pid,
                    process_start_marker,
                    argv_digest,
                    gpu_payload,
                    working_directory,
                    stdout_path,
                    stderr_path,
                    sampled_at,
                    sampled_at,
                    stage_ref,
                    stage_number,
                    owner_epoch,
                    version_label,
                    output_directory,
                    supervisor_state_path,
                    monitoring_format,
                    log_offset,
                    redactions_payload,
                    launch_token,
                ),
            )

    def list_launch_intents(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM worker_runs WHERE state = 'accepted' ORDER BY run_ref"
            ).fetchall()
        return [_row_payload(row) for row in rows]

    def attach_launch_supervisor(
        self,
        run_ref: str,
        *,
        pid: int,
        process_start_marker: str | None,
        argv_digest: str | None,
    ) -> None:
        if pid <= 0:
            raise ValueError("supervisor pid must be positive")
        sampled_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE worker_runs
                SET pid = ?, process_start_marker = ?, argv_digest = ?, updated_at = ?
                WHERE run_ref = ? AND state = 'accepted'""",
                (pid, process_start_marker, argv_digest, sampled_at, run_ref),
            )
        if cursor.rowcount != 1:
            raise KeyError(run_ref)

    def list_active_runs(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM worker_runs WHERE state IN ('running', 'stopping') ORDER BY run_ref"
            ).fetchall()
        return [_row_payload(row) for row in rows]

    def update_run_state(self, run_ref: str, state: str) -> None:
        if state not in KNOWN_RUN_STATES:
            raise ValueError(f"unsupported worker run state: {state}")
        sampled_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE worker_runs SET state = ?, updated_at = ? WHERE run_ref = ?",
                (state, sampled_at, run_ref),
            )
        if cursor.rowcount != 1:
            raise KeyError(run_ref)

    def update_log_offset(self, run_ref: str, offset: int) -> None:
        if offset < 0:
            raise ValueError("log offset must not be negative")
        sampled_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE worker_runs SET log_offset = ?, updated_at = ? WHERE run_ref = ?",
                (offset, sampled_at, run_ref),
            )

    def enqueue_update(
        self,
        run_ref: str,
        owner_epoch: int,
        payload: dict[str, object],
    ) -> int:
        if not run_ref or owner_epoch < 0 or not isinstance(payload, dict):
            raise ValueError("invalid run update")
        sampled_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT worker_seq FROM worker_runs WHERE run_ref = ?",
                (run_ref,),
            ).fetchone()
            if row is None:
                raise KeyError(run_ref)
            worker_seq = int(row["worker_seq"]) + 1
            connection.execute(
                "UPDATE worker_runs SET worker_seq = ?, updated_at = ? WHERE run_ref = ?",
                (worker_seq, sampled_at, run_ref),
            )
            connection.execute(
                """INSERT INTO worker_run_updates(
                    run_ref,owner_epoch,worker_seq,payload_json,created_at
                ) VALUES(?,?,?,?,?)""",
                (
                    run_ref,
                    owner_epoch,
                    worker_seq,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    sampled_at,
                ),
            )
        return worker_seq

    def pending_updates(self, run_ref: str, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit is out of range")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT owner_epoch,worker_seq,payload_json
                FROM worker_run_updates WHERE run_ref = ?
                ORDER BY owner_epoch,worker_seq LIMIT ?""",
                (run_ref, limit),
            ).fetchall()
        return [
            {
                "owner_epoch": int(row["owner_epoch"]),
                "worker_seq": int(row["worker_seq"]),
                **json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def pending_run_refs(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT run_ref FROM worker_run_updates ORDER BY run_ref"
            ).fetchall()
        return [str(row["run_ref"]) for row in rows]

    def acknowledge_updates(self, run_ref: str, owner_epoch: int, through_seq: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """DELETE FROM worker_run_updates
                WHERE run_ref = ? AND owner_epoch = ? AND worker_seq <= ?""",
                (run_ref, owner_epoch, through_seq),
            )

    def get_run(self, run_ref: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_runs WHERE run_ref = ?",
                (run_ref,),
            ).fetchone()
        return _row_payload(row) if row is not None else None

    def state_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM worker_runs GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def reconcile_active_runs(
        self,
        probe: ProcessProbe | None = None,
    ) -> list[ReconciliationResult]:
        process_probe = probe or LocalProcessProbe()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM worker_runs WHERE state IN ('running', 'stopping') ORDER BY run_ref"
            ).fetchall()

        results: list[ReconciliationResult] = []
        for row in rows:
            observation = ProcessObservation(
                pid=int(row["pid"]),
                process_start_marker=row["process_start_marker"],
                argv_digest=row["argv_digest"],
            )
            probe_result = process_probe.inspect(observation)
            previous_state = str(row["state"])
            current_state = previous_state if probe_result.status == "matched" else "unknown"
            sampled_at = datetime.now(timezone.utc).isoformat()
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE worker_runs
                    SET state = ?, last_reconciliation = ?, updated_at = ?
                    WHERE run_ref = ?
                    """,
                    (current_state, probe_result.status, sampled_at, row["run_ref"]),
                )
            results.append(
                ReconciliationResult(
                    run_ref=str(row["run_ref"]),
                    status=probe_result.status,
                    previous_state=previous_state,
                    current_state=current_state,
                    detail=probe_result.detail,
                )
            )
        return results


def process_identity_for_pid(pid: int) -> ProcessObservation | None:
    """Capture a Linux process identity without changing the process."""

    proc_dir = Path("/proc") / str(pid)
    if not proc_dir.exists():
        return None
    start_marker = _read_linux_process_start_marker(proc_dir)
    argv_digest = _read_linux_argv_digest(proc_dir)
    if start_marker is None or argv_digest is None:
        return None
    return ProcessObservation(pid, start_marker, argv_digest)


def _read_linux_process_start_marker(proc_dir: Path) -> str | None:
    try:
        stat = (proc_dir / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The command name is parenthesized and may contain spaces or parentheses;
    # fields after the final ')' begin at Linux proc field 3.  Starttime is 22.
    closing = stat.rfind(")")
    if closing < 0:
        return None
    remaining = stat[closing + 2 :].split()
    if len(remaining) <= 19:
        return None
    return remaining[19]


def _read_linux_argv_digest(proc_dir: Path) -> str | None:
    try:
        argv = (proc_dir / "cmdline").read_bytes()
    except OSError:
        return None
    if not argv:
        return None
    return hashlib.sha256(argv).hexdigest()


def _row_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "run_ref": str(row["run_ref"]),
        "state": str(row["state"]),
        "pid": row["pid"],
        "process_start_marker": row["process_start_marker"],
        "argv_digest": row["argv_digest"],
        "gpu_uuids": json.loads(row["gpu_uuids_json"]),
        "working_directory": row["working_directory"],
        "stdout_path": row["stdout_path"],
        "stderr_path": row["stderr_path"],
        "last_reconciliation": row["last_reconciliation"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "stage_ref": row["stage_ref"],
        "stage_number": row["stage_number"],
        "owner_epoch": int(row["owner_epoch"] or 0),
        "worker_seq": int(row["worker_seq"] or 0),
        "version_label": row["version_label"],
        "output_directory": row["output_directory"],
        "supervisor_state_path": row["supervisor_state_path"],
        "monitoring_format": row["monitoring_format"] or "plain",
        "log_offset": int(row["log_offset"] or 0),
        "redactions": json.loads(row["redactions_json"] or "[]"),
        "launch_token": row["launch_token"],
    }

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from vla_data_juicer_agents.navigation.plan_models import (
    ExecutionStepRecord,
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
    NavigationPlanRecord,
    PlanSubmissionAttempt,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask, utc_now
from vla_data_juicer_agents.navigation.task_store import (
    ensure_navigation_task_step_ledger_columns,
)


PLAN_CONTRACT_VERSION = "navigation-plan-v2"
MAX_EXECUTION_READ_CHARS = 4000
MAX_RESULT_OUTBOX_CHARS = 262_144
MAX_HUMAN_DECISION_CHARS = 16_384
HUMAN_DECISION_DELIVERY_LEASE_SECONDS = 60
_SENSITIVE_KEYS = {
    "password",
    "token",
    "secret",
    "authorization",
    "api_key",
    "cookie",
}
PlanPhase = Literal["extract_sync", "finish_processing"]
ExecutionStatus = Literal[
    "pending",
    "running",
    "waiting_user",
    "completed",
    "failed",
    "needs_replan",
]


class _StrictReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActivePlanExecutionConflict(RuntimeError):
    """Raised when a replacement would orphan in-flight active-plan work."""


class StagedStepResult(_StrictReadModel):
    plan_id: str
    step_id: str
    task_id: str
    plan_revision: int
    target_status: Literal["completed", "failed"]
    expected_statuses: list[ExecutionStatus]
    full_result: dict[str, Any]
    result_summary: dict[str, Any]
    result_ref: str | None = None


class HumanDecisionHandoff(_StrictReadModel):
    plan_id: str
    step_id: str
    task_id: str
    decision_key: str
    decision: dict[str, Any]
    status: Literal["pending", "recovery_required", "quarantined"] = "pending"
    delivery_status: Literal[
        "pending", "delivering", "delivered", "recovery_required", "quarantined"
    ] = "pending"
    delivery_owner: str | None = None
    delivery_token: str | None = None
    leased_at: str | None = None
    expires_at: str | None = None
    recovery_reason_code: str | None = None
    recovery_reason: str | None = None
    recovered_at: str | None = None


class CompactExecutionStep(_StrictReadModel):
    step_id: str
    action: str
    status: ExecutionStatus


class CompactExecutionOverview(_StrictReadModel):
    plan_id: str
    plan_revision: int
    status: str
    total_steps: int
    completed_steps: int
    current_step_id: str | None
    steps: list[CompactExecutionStep]


class SqliteNavigationPlanRepository:
    def __init__(self, db_path: str | Path, *, initialize: bool = True) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            ensure_navigation_task_step_ledger_columns(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS navigation_plans (
                    plan_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('extract_sync', 'finish_processing')),
                    plan_revision INTEGER NOT NULL,
                    contract_version TEXT NOT NULL,
                    observation_revision INTEGER NOT NULL,
                    plan_json TEXT NOT NULL,
                    validation_summary_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'superseded', 'completed', 'invalidated')
                    ),
                    invalidation_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (task_id, phase, plan_revision),
                    FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS navigation_step_result_outbox (
                    plan_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    plan_revision INTEGER NOT NULL,
                    target_status TEXT NOT NULL CHECK (target_status IN ('completed', 'failed')),
                    expected_statuses_json TEXT NOT NULL,
                    full_result_json TEXT NOT NULL,
                    result_summary_json TEXT NOT NULL,
                    result_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plan_id, step_id),
                    FOREIGN KEY (plan_id) REFERENCES navigation_plans(plan_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS navigation_human_decision_handoffs (
                    plan_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    decision_key TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'recovery_required', 'quarantined')
                    ),
                    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                        delivery_status IN (
                            'pending', 'delivering', 'delivered',
                            'recovery_required', 'quarantined'
                        )
                    ),
                    delivery_owner TEXT,
                    delivery_token TEXT,
                    leased_at TEXT,
                    expires_at TEXT,
                    recovery_reason_code TEXT,
                    recovery_reason TEXT,
                    recovered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plan_id, step_id),
                    FOREIGN KEY (plan_id) REFERENCES navigation_plans(plan_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
                        ON DELETE CASCADE
                )
                """
            )
            self._ensure_handoff_recovery_schema(connection)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_plans_active_task_phase
                ON navigation_plans (task_id, phase)
                WHERE status = 'active'
                """
            )
            handoff_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(navigation_human_decision_handoffs)"
                ).fetchall()
            }
            if "delivery_status" not in handoff_columns:
                connection.execute(
                    """ALTER TABLE navigation_human_decision_handoffs
                       ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'pending'"""
                )
            for name in (
                "delivery_owner",
                "delivery_token",
                "leased_at",
                "expires_at",
                "recovery_reason_code",
                "recovery_reason",
                "recovered_at",
            ):
                if name not in handoff_columns:
                    connection.execute(
                        f"ALTER TABLE navigation_human_decision_handoffs ADD COLUMN {name} TEXT"
                    )
            self._backfill_null_result_refs(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS navigation_plan_submission_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('extract_sync', 'finish_processing')),
                    planning_context_revision TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_navigation_plan_attempts_task_phase_created
                ON navigation_plan_submission_attempts (task_id, phase, created_at)
                """
            )

    @staticmethod
    def _ensure_handoff_recovery_schema(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'navigation_human_decision_handoffs'"
        ).fetchone()
        if row is None or "recovery_required" in str(row["sql"]):
            return
        connection.execute(
            """CREATE TABLE navigation_human_decision_handoffs_recovery (
                plan_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                decision_key TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'recovery_required', 'quarantined')
                ),
                delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    delivery_status IN (
                        'pending', 'delivering', 'delivered',
                        'recovery_required', 'quarantined'
                    )
                ),
                delivery_owner TEXT,
                delivery_token TEXT,
                leased_at TEXT,
                expires_at TEXT,
                recovery_reason_code TEXT,
                recovery_reason TEXT,
                recovered_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, step_id),
                FOREIGN KEY (plan_id) REFERENCES navigation_plans(plan_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES navigation_tasks(task_id)
                    ON DELETE CASCADE
            )"""
        )
        old_columns = {
            item["name"]
            for item in connection.execute(
                "PRAGMA table_info(navigation_human_decision_handoffs)"
            ).fetchall()
        }
        expressions = {
            name: name if name in old_columns else "NULL"
            for name in (
                "delivery_owner",
                "delivery_token",
                "leased_at",
                "expires_at",
            )
        }
        delivery_status = (
            "COALESCE(delivery_status, 'pending')"
            if "delivery_status" in old_columns
            else "'pending'"
        )
        connection.execute(
            f"""INSERT INTO navigation_human_decision_handoffs_recovery (
                    plan_id, step_id, task_id, decision_key, decision_json,
                    status, delivery_status, delivery_owner, delivery_token,
                    leased_at, expires_at, recovery_reason_code, recovery_reason,
                    recovered_at, created_at, updated_at
                )
                SELECT plan_id, step_id, task_id, decision_key, decision_json,
                       status, {delivery_status}, {expressions['delivery_owner']},
                       {expressions['delivery_token']}, {expressions['leased_at']},
                       {expressions['expires_at']}, NULL, NULL, NULL,
                       created_at, updated_at
                FROM navigation_human_decision_handoffs"""
        )
        connection.execute("DROP TABLE navigation_human_decision_handoffs")
        connection.execute(
            "ALTER TABLE navigation_human_decision_handoffs_recovery "
            "RENAME TO navigation_human_decision_handoffs"
        )

    def record_attempt(self, attempt: PlanSubmissionAttempt, *,
                       expected_web_session_id: str | None = None,
                       expected_agentscope_session_id: str | None = None) -> PlanSubmissionAttempt:
        stored = PlanSubmissionAttempt.model_validate(attempt)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if expected_agentscope_session_id is not None and connection.execute(
                """SELECT 1 FROM navigation_tasks WHERE task_id=?
                AND created_by_web_session_id IS ? AND latest_web_session_id IS ?
                AND agentscope_session_id IS ?""",
                (stored.task_id, expected_web_session_id, expected_web_session_id,
                 expected_agentscope_session_id),
            ).fetchone() is None:
                raise PermissionError("navigation task session mismatch")
            connection.execute(
                """
                INSERT INTO navigation_plan_submission_attempts (
                    attempt_id, task_id, phase, planning_context_revision,
                    candidate_json, validation_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.attempt_id,
                    stored.task_id,
                    stored.phase,
                    stored.planning_context_revision,
                    self._canonical_json(stored.candidate),
                    self._canonical_json(stored.validation.model_dump(mode="json")),
                    stored.created_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return stored

    def activate(
        self,
        task: NavigationTask,
        phase: PlanPhase | str,
        observation_revision: int,
        plan: ExtractSyncPlanInput | FinishProcessingPlanInput | dict[str, Any],
        *, expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> NavigationPlanRecord:
        phase_value = self._normalize_phase(phase)
        canonical_plan = self._validate_plan_for_phase(phase_value, plan)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task_row = connection.execute(
                """SELECT 1 FROM navigation_tasks WHERE task_id=? AND (? IS NULL OR (
                created_by_web_session_id IS ? AND latest_web_session_id IS ?
                AND agentscope_session_id IS ?))""",
                (task.task_id, expected_agentscope_session_id, expected_web_session_id,
                 expected_web_session_id, expected_agentscope_session_id),
            ).fetchone()
            if task_row is None:
                raise KeyError(task.task_id)
            revision_row = connection.execute(
                """
                SELECT COALESCE(MAX(plan_revision), 0) AS latest_revision
                FROM navigation_plans
                WHERE task_id = ? AND phase = ?
                """,
                (task.task_id, phase_value),
            ).fetchone()
            plan_revision = int(revision_row["latest_revision"]) + 1
            timestamp = utc_now()
            active_row = connection.execute(
                """
                SELECT plan_id FROM navigation_plans
                WHERE task_id = ? AND phase = ? AND status = 'active'
                """,
                (task.task_id, phase_value),
            ).fetchone()
            if self._task_phase_has_in_flight_work(
                connection,
                task.task_id,
                phase_value,
            ):
                raise ActivePlanExecutionConflict(
                    "cannot supersede an active navigation plan with running, "
                    "waiting-user, or staged outbox work"
                )
            connection.execute(
                """
                UPDATE navigation_plans
                SET status = 'superseded', updated_at = ?
                WHERE task_id = ? AND phase = ? AND status = 'active'
                """,
                (timestamp, task.task_id, phase_value),
            )
            record = NavigationPlanRecord(
                plan_id=f"nav_plan_{uuid4().hex}",
                task_id=task.task_id,
                phase=phase_value,
                plan_revision=plan_revision,
                contract_version=PLAN_CONTRACT_VERSION,
                observation_revision=observation_revision,
                status="active",
                plan=canonical_plan,
                created_at=timestamp,
            )
            connection.execute(
                """
                INSERT INTO navigation_plans (
                    plan_id, task_id, phase, plan_revision, contract_version,
                    observation_revision, plan_json, validation_summary_json,
                    status, invalidation_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    record.plan_id,
                    record.task_id,
                    record.phase,
                    record.plan_revision,
                    record.contract_version,
                    record.observation_revision,
                    self._canonical_json(record.plan.model_dump(mode="json")),
                    self._canonical_json({"ok": True, "errors": [], "warnings": []}),
                    record.status,
                    record.created_at,
                    record.created_at,
                ),
            )
            self._insert_ledger_rows(connection, record)
            connection.commit()
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _active_plan_has_in_flight_work(
        connection: sqlite3.Connection,
        plan_id: str,
    ) -> bool:
        step = connection.execute(
            """
            SELECT 1 FROM navigation_task_steps
            WHERE plan_id = ? AND status IN ('running', 'waiting_user')
            LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        if step is not None:
            return True
        outbox = connection.execute(
            """
            SELECT 1 FROM navigation_step_result_outbox
            WHERE plan_id = ? LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        if outbox is not None:
            return True
        handoff = connection.execute(
            """
            SELECT 1 FROM navigation_human_decision_handoffs
            WHERE plan_id = ? AND status != 'quarantined' LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        return handoff is not None

    @staticmethod
    def _task_phase_has_in_flight_work(
        connection: sqlite3.Connection,
        task_id: str,
        phase: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM navigation_plans AS plans
            LEFT JOIN navigation_task_steps AS steps
              ON steps.plan_id = plans.plan_id
            LEFT JOIN navigation_step_result_outbox AS outbox
              ON outbox.plan_id = plans.plan_id
            LEFT JOIN navigation_human_decision_handoffs AS handoffs
              ON handoffs.plan_id = plans.plan_id
            WHERE plans.task_id = ? AND plans.phase = ?
              AND (
                steps.status IN ('running', 'waiting_user')
                OR outbox.plan_id IS NOT NULL
                OR (handoffs.plan_id IS NOT NULL AND handoffs.status != 'quarantined')
              )
            LIMIT 1
            """,
            (task_id, phase),
        ).fetchone()
        return row is not None

    def get_active(
        self,
        task_id: str,
        phase: PlanPhase | str,
    ) -> NavigationPlanRecord | None:
        phase_value = self._normalize_phase(phase)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM navigation_plans
                WHERE task_id = ? AND phase = ? AND status = 'active'
                """,
                (task_id, phase_value),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def get(self, plan_id: str) -> NavigationPlanRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM navigation_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def invalidate(self, plan_id: str, reason: str) -> NavigationPlanRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, phase FROM navigation_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError(plan_id)
            if self._active_plan_has_in_flight_work(connection, plan_id):
                raise ActivePlanExecutionConflict(
                    "cannot invalidate a navigation plan with in-flight work"
                )
            connection.execute(
                """
                UPDATE navigation_plans
                SET status = 'invalidated', invalidation_reason = ?, updated_at = ?
                WHERE plan_id = ?
                """,
                (reason, utc_now(), plan_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        record = self.get(plan_id)
        if record is None:
            raise KeyError(plan_id)
        return record

    def get_execution_overview(self, plan_id: str) -> CompactExecutionOverview:
        plan = self._require_plan(plan_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT step_id, tool_name, status
                FROM navigation_task_steps
                WHERE plan_id = ? AND plan_revision = ?
                ORDER BY sequence ASC
                """,
                (plan.plan_id, plan.plan_revision),
            ).fetchall()
        steps = [
            CompactExecutionStep(
                step_id=row["step_id"],
                action=row["tool_name"],
                status=row["status"],
            )
            for row in rows
        ]
        overview = CompactExecutionOverview(
            plan_id=plan.plan_id,
            plan_revision=plan.plan_revision,
            status=plan.status,
            total_steps=len(steps),
            completed_steps=sum(step.status == "completed" for step in steps),
            current_step_id=next(
                (step.step_id for step in steps if step.status != "completed"),
                None,
            ),
            steps=steps,
        )
        self._ensure_within_limit(
            overview.model_dump(mode="json"),
            label="execution overview",
        )
        return overview

    def get_current_step(self, plan_id: str) -> dict[str, Any] | None:
        plan = self._require_plan(plan_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, plan_id, plan_revision, sequence, step_id,
                       tool_name, status, result_summary_json, result_ref, retry_count
                FROM navigation_task_steps
                WHERE plan_id = ? AND plan_revision = ? AND status != 'completed'
                ORDER BY sequence ASC
                LIMIT 1
                """,
                (plan.plan_id, plan.plan_revision),
            ).fetchone()
        if row is None:
            return None
        step = ExecutionStepRecord(
            id=row["id"],
            plan_id=row["plan_id"],
            plan_revision=row["plan_revision"],
            sequence=row["sequence"],
            step_id=row["step_id"],
            action=row["tool_name"],
            status=row["status"],
            result_summary=(
                json.loads(row["result_summary_json"])
                if row["result_summary_json"] is not None
                else None
            ),
            result_ref=row["result_ref"],
            retry_count=row["retry_count"],
        )
        decision_refs = next(
            (
                list(plan_step.decision_refs)
                for plan_step in plan.plan.steps
                if plan_step.step_id == step.step_id
            ),
            [],
        )
        payload = {
            "plan_id": plan.plan_id,
            "plan_revision": plan.plan_revision,
            "step": step.model_dump(mode="json"),
            "decision_refs": decision_refs,
        }
        self._ensure_within_limit(payload, label="current step")
        return payload

    def claim_step(
        self,
        plan_id: str,
        step_id: str,
        action: str,
        *,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Atomically claim one pending ledger step for exactly-once invocation."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE navigation_task_steps
                SET status = 'running', started_at = ?
                WHERE plan_id = ? AND step_id = ? AND tool_name = ?
                  AND status = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM navigation_plans
                      JOIN navigation_tasks
                        ON navigation_tasks.task_id = navigation_plans.task_id
                      WHERE navigation_plans.plan_id = navigation_task_steps.plan_id
                        AND navigation_plans.status = 'active'
                        AND (? IS NULL OR (
                            navigation_tasks.created_by_web_session_id = ?
                            AND navigation_tasks.latest_web_session_id = ?
                            AND navigation_tasks.agentscope_session_id = ?
                        ))
                  )
                """,
                (
                    utc_now(), plan_id, step_id, action,
                    expected_agentscope_session_id,
                    expected_web_session_id,
                    expected_web_session_id,
                    expected_agentscope_session_id,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """UPDATE navigation_tasks
                       SET state_revision = state_revision + 1
                       WHERE task_id = (
                           SELECT task_id FROM navigation_plans WHERE plan_id = ?
                       )""",
                    (plan_id,),
                )
        return cursor.rowcount == 1

    def mark_waiting_user(
        self,
        plan_id: str,
        step_id: str,
        action: str,
        *,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Atomically expose one pending external step as waiting for the user."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE navigation_task_steps
                SET status = 'waiting_user', started_at = COALESCE(started_at, ?)
                WHERE plan_id = ? AND step_id = ? AND tool_name = ?
                  AND status = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM navigation_plans
                      JOIN navigation_tasks
                        ON navigation_tasks.task_id = navigation_plans.task_id
                      WHERE navigation_plans.plan_id = navigation_task_steps.plan_id
                        AND navigation_plans.status = 'active'
                        AND (? IS NULL OR (
                            navigation_tasks.created_by_web_session_id = ?
                            AND navigation_tasks.latest_web_session_id = ?
                            AND navigation_tasks.agentscope_session_id = ?
                        ))
                  )
                """,
                (
                    utc_now(), plan_id, step_id, action,
                    expected_agentscope_session_id,
                    expected_web_session_id,
                    expected_web_session_id,
                    expected_agentscope_session_id,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """UPDATE navigation_tasks
                       SET state_revision = state_revision + 1
                       WHERE task_id = (
                           SELECT task_id FROM navigation_plans WHERE plan_id = ?
                       )""",
                    (plan_id,),
                )
        return cursor.rowcount == 1

    def stage_step_result(
        self,
        plan_id: str,
        step_id: str,
        *,
        target_status: Literal["completed", "failed"],
        full_result: dict[str, Any],
        result_summary: dict[str, Any],
        expected_statuses: tuple[ExecutionStatus, ...] = ("running",),
    ) -> StagedStepResult:
        """Durably stage a post-side-effect result before crossing to file evidence."""
        self._ensure_within_limit(result_summary, label="execution result summary")
        if not expected_statuses:
            raise ValueError("expected_statuses must not be empty")
        canonical_full = self._canonical_json(full_result)
        if len(canonical_full.encode("utf-8")) > MAX_RESULT_OUTBOX_CHARS:
            raise ValueError(
                f"execution result exceeds {MAX_RESULT_OUTBOX_CHARS} byte outbox limit"
            )
        canonical_summary = self._canonical_json(result_summary)
        canonical_expected = self._canonical_json(list(expected_statuses))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM navigation_step_result_outbox
                WHERE plan_id = ? AND step_id = ?
                """,
                (plan_id, step_id),
            ).fetchone()
            if existing is not None:
                staged = self._staged_result_from_row(existing)
                if (
                    staged.target_status != target_status
                    or staged.full_result != full_result
                    or staged.result_summary != result_summary
                    or staged.expected_statuses != list(expected_statuses)
                ):
                    raise RuntimeError("conflicting staged result for navigation step")
                connection.commit()
                return staged
            placeholders = ",".join("?" for _ in expected_statuses)
            step = connection.execute(
                f"""
                SELECT steps.task_id, steps.plan_revision, plans.observation_revision
                FROM navigation_task_steps AS steps
                JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                WHERE steps.plan_id = ? AND steps.step_id = ?
                  AND steps.status IN ({placeholders})
                  AND plans.status = 'active'
                """,
                (plan_id, step_id, *expected_statuses),
            ).fetchone()
            if step is None:
                raise RuntimeError("navigation step is not claimable for result staging")
            timestamp = utc_now()
            connection.execute(
                """
                INSERT INTO navigation_step_result_outbox (
                    plan_id, step_id, task_id, plan_revision, target_status,
                    expected_statuses_json, full_result_json, result_summary_json,
                    result_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    step_id,
                    step["task_id"],
                    step["plan_revision"],
                    target_status,
                    canonical_expected,
                    canonical_full,
                    canonical_summary,
                    self._intended_result_ref(
                        step["task_id"],
                        int(step["observation_revision"]),
                        plan_id,
                        step_id,
                        canonical_full,
                    ),
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        staged = self.get_staged_step_result(plan_id, step_id)
        if staged is None:
            raise RuntimeError("staged navigation result was not persisted")
        return staged

    def get_staged_step_result(
        self,
        plan_id: str,
        step_id: str,
    ) -> StagedStepResult | None:
        with self._connect() as connection:
            self._backfill_null_result_refs(
                connection, plan_id=plan_id, step_id=step_id
            )
            row = connection.execute(
                """
                SELECT * FROM navigation_step_result_outbox
                WHERE plan_id = ? AND step_id = ?
                """,
                (plan_id, step_id),
            ).fetchone()
        return self._staged_result_from_row(row) if row is not None else None

    def attach_staged_result_evidence(
        self,
        plan_id: str,
        step_id: str,
        result_ref: str,
    ) -> bool:
        """Attach task-scoped evidence to a staged result without finishing it."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT result_ref FROM navigation_step_result_outbox
                WHERE plan_id = ? AND step_id = ?
                """,
                (plan_id, step_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            if row["result_ref"] not in {None, result_ref}:
                raise RuntimeError("staged result already references different evidence")
            connection.execute(
                """
                UPDATE navigation_step_result_outbox
                SET result_ref = ?, updated_at = ?
                WHERE plan_id = ? AND step_id = ?
                """,
                (result_ref, utc_now(), plan_id, step_id),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finalize_staged_step(self, plan_id: str, step_id: str) -> bool:
        """Atomically finish one staged ledger step and clear its result outbox."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM navigation_step_result_outbox
                WHERE plan_id = ? AND step_id = ?
                """,
                (plan_id, step_id),
            ).fetchone()
            if row is None or row["result_ref"] is None:
                connection.rollback()
                return False
            staged = self._staged_result_from_row(row)
            placeholders = ",".join("?" for _ in staged.expected_statuses)
            cursor = connection.execute(
                f"""
                UPDATE navigation_task_steps
                SET status = ?, result_summary_json = ?, result_ref = ?, finished_at = ?
                WHERE plan_id = ? AND step_id = ?
                  AND status IN ({placeholders})
                  AND EXISTS (
                      SELECT 1 FROM navigation_plans
                      WHERE navigation_plans.plan_id = navigation_task_steps.plan_id
                        AND navigation_plans.status = 'active'
                  )
                """,
                (
                    staged.target_status,
                    self._canonical_json(staged.result_summary),
                    staged.result_ref,
                    utc_now(),
                    plan_id,
                    step_id,
                    *staged.expected_statuses,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """UPDATE navigation_tasks
                   SET state_revision = state_revision + 1
                   WHERE task_id = ?""",
                (staged.task_id,),
            )
            if staged.target_status == "completed":
                remaining = connection.execute(
                    """
                    SELECT 1 FROM navigation_task_steps
                    WHERE plan_id = ? AND status != 'completed'
                    LIMIT 1
                    """,
                    (plan_id,),
                ).fetchone()
                if remaining is None:
                    connection.execute(
                        """
                        UPDATE navigation_plans
                        SET status = 'completed', updated_at = ?
                        WHERE plan_id = ? AND status = 'active'
                        """,
                        (utc_now(), plan_id),
                    )
            else:
                connection.execute(
                    """UPDATE navigation_tasks
                       SET status = 'failed', updated_at = ?, state_revision = state_revision + 1
                       WHERE task_id = ?""",
                    (utc_now(), staged.task_id),
                )
            connection.execute(
                """
                DELETE FROM navigation_step_result_outbox
                WHERE plan_id = ? AND step_id = ?
                """,
                (plan_id, step_id),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_running_step_without_result(
        self,
        plan_id: str,
        step_id: str,
        reason: str,
    ) -> bool:
        """Conservatively invalidate an unrecoverable running step without rerunning it."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT plans.task_id
                FROM navigation_task_steps AS steps
                JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                WHERE steps.plan_id = ? AND steps.step_id = ?
                  AND steps.status = 'running' AND plans.status = 'active'
                  AND NOT EXISTS (
                      SELECT 1 FROM navigation_step_result_outbox AS outbox
                      WHERE outbox.plan_id = steps.plan_id
                        AND outbox.step_id = steps.step_id
                  )
                """,
                (plan_id, step_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            handoff = connection.execute(
                """SELECT handoffs.plan_id, handoffs.step_id
                   FROM navigation_human_decision_handoffs AS handoffs
                   JOIN navigation_plans AS handoff_plans
                     ON handoff_plans.plan_id = handoffs.plan_id
                   JOIN navigation_plans AS target_plan
                     ON target_plan.task_id = handoff_plans.task_id
                    AND target_plan.phase = handoff_plans.phase
                   WHERE target_plan.plan_id = ?
                     AND handoffs.status != 'quarantined'
                   LIMIT 1""",
                (plan_id,),
            ).fetchone()
            if handoff is not None:
                raise ActivePlanExecutionConflict(
                    "running-step force recovery is blocked by an unacknowledged "
                    f"human handoff ({handoff['plan_id']}/{handoff['step_id']}); "
                    "recover or acknowledge the human handoff first"
                )
            timestamp = utc_now()
            connection.execute(
                """
                UPDATE navigation_plans
                SET status = 'invalidated', invalidation_reason = ?, updated_at = ?
                WHERE plan_id = ? AND status = 'active'
                """,
                (reason, timestamp, plan_id),
            )
            connection.execute(
                """
                UPDATE navigation_task_steps
                SET status = 'needs_replan', finished_at = ?
                WHERE plan_id = ? AND status != 'completed'
                """,
                (timestamp, plan_id),
            )
            connection.execute(
                """
                UPDATE navigation_tasks
                SET status = 'needs_replan', updated_at = ?, state_revision = state_revision + 1
                WHERE task_id = ?
                """,
                (timestamp, row["task_id"]),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def stage_human_decision_handoff(
        self,
        plan_id: str,
        step_id: str,
        *,
        decision_key: str,
        decision: dict[str, Any],
        target_status: Literal["completed", "failed"],
        full_result: dict[str, Any],
        result_summary: dict[str, Any],
    ) -> Literal["created", "existing", "conflict"]:
        """Persist a decision handoff and its terminal result outbox atomically."""
        self._ensure_within_limit(result_summary, label="human decision result summary")
        canonical_decision = self._canonical_json(decision)
        canonical_full = self._canonical_json(full_result)
        if len(canonical_decision.encode("utf-8")) > MAX_HUMAN_DECISION_CHARS:
            raise ValueError(
                f"human decision exceeds {MAX_HUMAN_DECISION_CHARS} byte limit"
            )
        if len(canonical_full.encode("utf-8")) > MAX_RESULT_OUTBOX_CHARS:
            raise ValueError(
                f"human result exceeds {MAX_RESULT_OUTBOX_CHARS} byte outbox limit"
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT decision_key, decision_json
                FROM navigation_human_decision_handoffs
                WHERE plan_id = ? AND step_id = ?
                """,
                (plan_id, step_id),
            ).fetchone()
            if existing is not None:
                outcome: Literal["existing", "conflict"] = (
                    "existing"
                    if existing["decision_key"] == decision_key
                    and existing["decision_json"] == canonical_decision
                    else "conflict"
                )
                connection.commit()
                return outcome
            row = connection.execute(
                """
                SELECT steps.task_id, steps.plan_revision, plans.observation_revision
                FROM navigation_task_steps AS steps
                JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                WHERE steps.plan_id = ? AND steps.step_id = ?
                  AND steps.tool_name = 'confirm_navigation_calibration_params'
                  AND steps.status IN ('pending', 'waiting_user')
                  AND plans.status = 'active'
                """,
                (plan_id, step_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return "conflict"
            timestamp = utc_now()
            connection.execute(
                """
                INSERT INTO navigation_human_decision_handoffs (
                    plan_id, step_id, task_id, decision_key, decision_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    plan_id,
                    step_id,
                    row["task_id"],
                    decision_key,
                    canonical_decision,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO navigation_step_result_outbox (
                    plan_id, step_id, task_id, plan_revision, target_status,
                    expected_statuses_json, full_result_json, result_summary_json,
                    result_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    step_id,
                    row["task_id"],
                    row["plan_revision"],
                    target_status,
                    self._canonical_json(["pending", "waiting_user"]),
                    canonical_full,
                    self._canonical_json(result_summary),
                    self._intended_result_ref(
                        row["task_id"],
                        int(row["observation_revision"]),
                        plan_id,
                        step_id,
                        canonical_full,
                    ),
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
            return "created"
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_human_decision_handoff(
        self,
        plan_id: str,
        step_id: str,
    ) -> HumanDecisionHandoff | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM navigation_human_decision_handoffs
                WHERE plan_id = ? AND step_id = ?
                """,
                (plan_id, step_id),
            ).fetchone()
        if row is None:
            return None
        return HumanDecisionHandoff(
            plan_id=row["plan_id"],
            step_id=row["step_id"],
            task_id=row["task_id"],
            decision_key=row["decision_key"],
            decision=json.loads(row["decision_json"]),
            status=row["status"],
            delivery_status=row["delivery_status"],
            delivery_owner=row["delivery_owner"],
            delivery_token=row["delivery_token"],
            leased_at=row["leased_at"],
            expires_at=row["expires_at"],
            recovery_reason_code=row["recovery_reason_code"],
            recovery_reason=row["recovery_reason"],
            recovered_at=row["recovered_at"],
        )

    def acknowledge_human_decision_handoff(
        self,
        plan_id: str,
        step_id: str,
        decision_key: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM navigation_human_decision_handoffs
                WHERE plan_id = ? AND step_id = ? AND decision_key = ?
                  AND status = 'pending'
                """,
                (plan_id, step_id, decision_key),
            )
        return cursor.rowcount == 1

    def claim_human_decision_delivery(
        self, plan_id: str, step_id: str, decision_key: str, *, owner: str
    ) -> tuple[
        Literal["claimed", "busy", "delivered", "missing", "recovery_required"],
        str | None,
    ]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT status, delivery_status, decision_key, expires_at
                   FROM navigation_human_decision_handoffs
                   WHERE plan_id = ? AND step_id = ?""",
                (plan_id, step_id),
            ).fetchone()
            if row is None or row["decision_key"] != decision_key:
                connection.rollback()
                return "missing", None
            if row["status"] == "quarantined":
                connection.commit()
                return "missing", None
            if row["status"] == "recovery_required":
                connection.commit()
                return "recovery_required", None
            if row["delivery_status"] == "delivered":
                connection.commit()
                return "delivered", None
            now = datetime.now(UTC)
            if (
                row["delivery_status"] == "delivering"
                and row["expires_at"] is not None
                and datetime.fromisoformat(row["expires_at"]) > now
            ):
                connection.commit()
                return "busy", None
            if row["delivery_status"] == "delivering":
                connection.execute(
                    """UPDATE navigation_human_decision_handoffs
                       SET status = 'recovery_required',
                           delivery_status = 'recovery_required',
                           recovery_reason_code = 'delivery_lease_expired',
                           updated_at = ?
                       WHERE plan_id = ? AND step_id = ? AND decision_key = ?
                         AND status = 'pending' AND delivery_status = 'delivering'""",
                    (utc_now(), plan_id, step_id, decision_key),
                )
                connection.commit()
                return "recovery_required", None
            token = uuid4().hex
            leased_at = now.isoformat(timespec="milliseconds")
            expires_at = (
                now + timedelta(seconds=HUMAN_DECISION_DELIVERY_LEASE_SECONDS)
            ).isoformat(timespec="milliseconds")
            connection.execute(
                """UPDATE navigation_human_decision_handoffs
                   SET delivery_status = 'delivering', delivery_owner = ?,
                       delivery_token = ?, leased_at = ?, expires_at = ?, updated_at = ?
                   WHERE plan_id = ? AND step_id = ? AND decision_key = ?""",
                (
                    owner,
                    token,
                    leased_at,
                    expires_at,
                    utc_now(),
                    plan_id,
                    step_id,
                    decision_key,
                ),
            )
            connection.commit()
            return "claimed", token
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_human_decision_recovery_required(
        self,
        plan_id: str,
        step_id: str,
        *,
        reason_code: str,
    ) -> bool:
        safe_code = re.sub(r"[^a-z0-9_:-]", "_", str(reason_code).lower())[:128]
        if not safe_code:
            safe_code = "ambiguous_delivery_state"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT status, delivery_status
                   FROM navigation_human_decision_handoffs
                   WHERE plan_id = ? AND step_id = ?""",
                (plan_id, step_id),
            ).fetchone()
            if row is None or row["status"] == "quarantined":
                connection.rollback()
                return False
            if row["status"] == "recovery_required":
                connection.commit()
                return True
            if row["delivery_status"] == "delivered":
                connection.rollback()
                return False
            cursor = connection.execute(
                """UPDATE navigation_human_decision_handoffs
                   SET status = 'recovery_required',
                       delivery_status = 'recovery_required',
                       recovery_reason_code = ?, updated_at = ?
                   WHERE plan_id = ? AND step_id = ? AND status = 'pending'
                     AND delivery_status IN ('pending', 'delivering')""",
                (safe_code, utc_now(), plan_id, step_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def quarantine_human_decision_handoff(
        self,
        plan_id: str,
        step_id: str,
        *,
        expected_web_session_id: str,
        reason: str,
    ) -> dict[str, Any]:
        safe_reason = self._sanitize_recovery_reason(reason)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT handoffs.status, handoffs.delivery_status,
                          handoffs.task_id, tasks.created_by_web_session_id
                   FROM navigation_human_decision_handoffs AS handoffs
                   JOIN navigation_tasks AS tasks ON tasks.task_id = handoffs.task_id
                   WHERE handoffs.plan_id = ? AND handoffs.step_id = ?""",
                (plan_id, step_id),
            ).fetchone()
            if row is None:
                raise ActivePlanExecutionConflict("human handoff was not found")
            if row["created_by_web_session_id"] != expected_web_session_id:
                raise ActivePlanExecutionConflict(
                    "human handoff does not belong to the requested Web session"
                )
            if (
                row["status"] != "recovery_required"
                or row["delivery_status"] != "recovery_required"
            ):
                raise ActivePlanExecutionConflict(
                    "only a recovery_required human handoff may be quarantined"
                )
            timestamp = utc_now()
            connection.execute(
                """UPDATE navigation_human_decision_handoffs
                   SET status = 'quarantined', delivery_status = 'quarantined',
                       recovery_reason = ?, recovered_at = ?, updated_at = ?
                   WHERE plan_id = ? AND step_id = ?
                     AND status = 'recovery_required'
                     AND delivery_status = 'recovery_required'""",
                (safe_reason, timestamp, timestamp, plan_id, step_id),
            )
            connection.execute(
                "DELETE FROM navigation_step_result_outbox WHERE plan_id = ? AND step_id = ?",
                (plan_id, step_id),
            )
            connection.execute(
                """UPDATE navigation_plans
                   SET status = 'invalidated',
                       invalidation_reason = 'human_handoff_quarantined', updated_at = ?
                   WHERE plan_id = ?""",
                (timestamp, plan_id),
            )
            connection.execute(
                """UPDATE navigation_task_steps
                   SET status = 'needs_replan', finished_at = ?
                   WHERE plan_id = ? AND status != 'completed'""",
                (timestamp, plan_id),
            )
            connection.execute(
                """UPDATE navigation_tasks SET status = 'needs_replan', updated_at = ?,
                   state_revision = state_revision + 1
                   WHERE task_id = ?""",
                (timestamp, row["task_id"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "recovered": True,
            "plan_id": plan_id,
            "step_id": step_id,
            "handoff_status": "quarantined",
            "task_status": "needs_replan",
            "next_action": "submit_complete_plan",
        }

    @staticmethod
    def _sanitize_recovery_reason(reason: str) -> str:
        value = str(reason).strip()
        if not value:
            raise ValueError("recovery reason must not be empty")
        value = re.sub(
            r"(?i)\b(password|token|secret|authorization|api_key|cookie)\b"
            r"\s*[:=]\s*[^\s,;]+",
            lambda match: f"{match.group(1)}=[REDACTED]",
            value,
        )
        return value[:4000]

    def finish_human_decision_delivery(
        self,
        plan_id: str,
        step_id: str,
        decision_key: str,
        *,
        token: str,
        delivered: bool,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE navigation_human_decision_handoffs
                   SET delivery_status = ?, delivery_owner = NULL,
                       delivery_token = NULL, leased_at = NULL, expires_at = NULL,
                       updated_at = ?
                   WHERE plan_id = ? AND step_id = ? AND decision_key = ?
                     AND delivery_status = 'delivering' AND delivery_token = ?""",
                (
                    "delivered" if delivered else "pending",
                    utc_now(), plan_id, step_id, decision_key, token,
                ),
            )
        return cursor.rowcount == 1

    def mark_consumed_human_decision_delivery(
        self, plan_id: str, step_id: str, decision_key: str
    ) -> bool:
        """Record durable AgentScope consumption observed outside our lease state."""
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE navigation_human_decision_handoffs
                   SET status = 'pending', delivery_status = 'delivered', delivery_owner = NULL,
                       delivery_token = NULL, leased_at = NULL, expires_at = NULL,
                       updated_at = ?
                   WHERE plan_id = ? AND step_id = ? AND decision_key = ?
                     AND status IN ('pending', 'recovery_required')
                     AND delivery_status != 'delivered'""",
                (utc_now(), plan_id, step_id, decision_key),
            )
        return cursor.rowcount == 1

    def mark_needs_replan(self, plan_id: str, reason: str) -> bool:
        """Invalidate one active plan and its unfinished ledger in one transaction."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id FROM navigation_plans WHERE plan_id = ? AND status = 'active'",
                (plan_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            if self._active_plan_has_in_flight_work(connection, plan_id):
                raise ActivePlanExecutionConflict(
                    "cannot mark a navigation plan needs-replan with in-flight work"
                )
            timestamp = utc_now()
            connection.execute(
                """
                UPDATE navigation_plans
                SET status = 'invalidated', invalidation_reason = ?, updated_at = ?
                WHERE plan_id = ? AND status = 'active'
                """,
                (reason, timestamp, plan_id),
            )
            connection.execute(
                """
                UPDATE navigation_task_steps
                SET status = 'needs_replan', finished_at = ?
                WHERE plan_id = ? AND status != 'completed'
                """,
                (timestamp, plan_id),
            )
            connection.execute(
                """
                UPDATE navigation_tasks
                SET status = 'needs_replan', updated_at = ?, state_revision = state_revision + 1
                WHERE task_id = ?
                """,
                (timestamp, row["task_id"]),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def dependency_statuses(
        self,
        plan_id: str,
        step_ids: list[str],
    ) -> dict[str, ExecutionStatus]:
        if not step_ids:
            return {}
        placeholders = ",".join("?" for _ in step_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT step_id, status FROM navigation_task_steps
                WHERE plan_id = ? AND step_id IN ({placeholders})
                """,
                (plan_id, *step_ids),
            ).fetchall()
        return {row["step_id"]: cast(ExecutionStatus, row["status"]) for row in rows}

    def _insert_ledger_rows(
        self,
        connection: sqlite3.Connection,
        record: NavigationPlanRecord,
    ) -> None:
        for sequence, step in enumerate(record.plan.steps):
            connection.execute(
                """
                INSERT INTO navigation_task_steps (
                    id, task_id, phase, step_id, tool_name, status,
                    started_at, finished_at, plan_id, plan_revision, sequence,
                    result_summary_json, result_ref, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    f"nav_step_{uuid4().hex}",
                    record.task_id,
                    record.phase,
                    step.step_id,
                    step.action,
                    "pending",
                    record.plan_id,
                    record.plan_revision,
                    sequence,
                    0,
                ),
            )

    def _require_plan(self, plan_id: str) -> NavigationPlanRecord:
        plan = self.get(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan

    @staticmethod
    def _normalize_phase(phase: PlanPhase | str) -> PlanPhase:
        value = str(phase)
        if value not in {"extract_sync", "finish_processing"}:
            raise ValueError(f"unsupported navigation plan phase: {value}")
        return cast(PlanPhase, value)

    @staticmethod
    def _validate_plan_for_phase(
        phase: PlanPhase,
        plan: ExtractSyncPlanInput | FinishProcessingPlanInput | dict[str, Any],
    ) -> ExtractSyncPlanInput | FinishProcessingPlanInput:
        model = ExtractSyncPlanInput if phase == "extract_sync" else FinishProcessingPlanInput
        return model.model_validate(plan)

    @staticmethod
    def _canonical_json(payload: Any) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _intended_result_ref(
        task_id: str,
        observation_revision: int,
        plan_id: str,
        step_id: str,
        canonical_full: str,
    ) -> str:
        from vla_data_juicer_agents.navigation.evidence_store import (
            FileNavigationEvidenceStore,
        )

        digest = hashlib.sha256(canonical_full.encode("utf-8")).hexdigest()
        return FileNavigationEvidenceStore(Path(".")).deterministic_ref(
            task_id,
            observation_revision,
            f"{plan_id}:{step_id}:{digest}",
        )

    def _backfill_null_result_refs(
        self,
        connection: sqlite3.Connection,
        *,
        plan_id: str | None = None,
        step_id: str | None = None,
    ) -> None:
        where = "WHERE outbox.result_ref IS NULL"
        params: list[Any] = []
        if plan_id is not None:
            where += " AND outbox.plan_id = ?"
            params.append(plan_id)
        if step_id is not None:
            where += " AND outbox.step_id = ?"
            params.append(step_id)
        rows = connection.execute(
            f"""SELECT outbox.plan_id, outbox.step_id, outbox.task_id,
                       outbox.full_result_json, plans.observation_revision
                FROM navigation_step_result_outbox AS outbox
                JOIN navigation_plans AS plans ON plans.plan_id = outbox.plan_id
                {where}""",
            params,
        ).fetchall()
        for row in rows:
            canonical_full = self._canonical_json(
                self._redact_sensitive(json.loads(row["full_result_json"]))
            )
            result_ref = self._intended_result_ref(
                row["task_id"],
                int(row["observation_revision"]),
                row["plan_id"],
                row["step_id"],
                canonical_full,
            )
            connection.execute(
                """UPDATE navigation_step_result_outbox
                   SET full_result_json = ?, result_ref = ?, updated_at = ?
                   WHERE plan_id = ? AND step_id = ? AND result_ref IS NULL""",
                (
                    canonical_full,
                    result_ref,
                    utc_now(),
                    row["plan_id"],
                    row["step_id"],
                ),
            )

    @classmethod
    def _redact_sensitive(cls, payload: Any) -> Any:
        if isinstance(payload, dict):
            return {
                key: (
                    "[REDACTED]"
                    if str(key).lower() in _SENSITIVE_KEYS
                    else cls._redact_sensitive(value)
                )
                for key, value in payload.items()
            }
        if isinstance(payload, list):
            return [cls._redact_sensitive(value) for value in payload]
        return payload

    @classmethod
    def _ensure_within_limit(cls, payload: Any, *, label: str) -> None:
        if len(cls._canonical_json(payload)) > MAX_EXECUTION_READ_CHARS:
            raise ValueError(f"{label} exceeds {MAX_EXECUTION_READ_CHARS} characters")

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> NavigationPlanRecord:
        plan_model = (
            ExtractSyncPlanInput
            if row["phase"] == "extract_sync"
            else FinishProcessingPlanInput
        )
        return NavigationPlanRecord(
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            phase=row["phase"],
            plan_revision=row["plan_revision"],
            contract_version=row["contract_version"],
            observation_revision=row["observation_revision"],
            status=row["status"],
            plan=plan_model.model_validate_json(row["plan_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _staged_result_from_row(row: sqlite3.Row) -> StagedStepResult:
        return StagedStepResult(
            plan_id=row["plan_id"],
            step_id=row["step_id"],
            task_id=row["task_id"],
            plan_revision=row["plan_revision"],
            target_status=row["target_status"],
            expected_statuses=json.loads(row["expected_statuses_json"]),
            full_result=json.loads(row["full_result_json"]),
            result_summary=json.loads(row["result_summary_json"]),
            result_ref=row["result_ref"],
        )

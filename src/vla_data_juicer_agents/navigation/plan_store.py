from __future__ import annotations

import json
import sqlite3
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
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_plans_active_task_phase
                ON navigation_plans (task_id, phase)
                WHERE status = 'active'
                """
            )
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

    def record_attempt(self, attempt: PlanSubmissionAttempt) -> PlanSubmissionAttempt:
        stored = PlanSubmissionAttempt.model_validate(attempt)
        with self._connect() as connection:
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
        return stored

    def activate(
        self,
        task: NavigationTask,
        phase: PlanPhase | str,
        observation_revision: int,
        plan: ExtractSyncPlanInput | FinishProcessingPlanInput | dict[str, Any],
    ) -> NavigationPlanRecord:
        phase_value = self._normalize_phase(phase)
        canonical_plan = self._validate_plan_for_phase(phase_value, plan)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task_row = connection.execute(
                "SELECT 1 FROM navigation_tasks WHERE task_id = ?",
                (task.task_id,),
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
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE navigation_plans
                SET status = 'invalidated', invalidation_reason = ?, updated_at = ?
                WHERE plan_id = ?
                """,
                (reason, utc_now(), plan_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(plan_id)
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

    def claim_step(self, plan_id: str, step_id: str, action: str) -> bool:
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
                      WHERE navigation_plans.plan_id = navigation_task_steps.plan_id
                        AND navigation_plans.status = 'active'
                  )
                """,
                (utc_now(), plan_id, step_id, action),
            )
        return cursor.rowcount == 1

    def mark_waiting_user(self, plan_id: str, step_id: str, action: str) -> bool:
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
                      WHERE navigation_plans.plan_id = navigation_task_steps.plan_id
                        AND navigation_plans.status = 'active'
                  )
                """,
                (utc_now(), plan_id, step_id, action),
            )
        return cursor.rowcount == 1

    def finish_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        status: Literal["completed", "failed"],
        result_summary: dict[str, Any],
        result_ref: str,
        expected_statuses: tuple[ExecutionStatus, ...] = ("running",),
    ) -> bool:
        """Finish a claimed step once and complete the plan when all steps succeed."""
        self._ensure_within_limit(result_summary, label="execution result summary")
        placeholders = ",".join("?" for _ in expected_statuses)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE navigation_task_steps
                SET status = ?, result_summary_json = ?, result_ref = ?, finished_at = ?
                WHERE plan_id = ? AND step_id = ? AND status IN ({placeholders})
                  AND EXISTS (
                      SELECT 1 FROM navigation_plans
                      WHERE navigation_plans.plan_id = navigation_task_steps.plan_id
                        AND navigation_plans.status = 'active'
                  )
                """,
                (
                    status,
                    self._canonical_json(result_summary),
                    result_ref,
                    utc_now(),
                    plan_id,
                    step_id,
                    *expected_statuses,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            if status == "completed":
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
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
                SET status = 'needs_replan', updated_at = ?
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

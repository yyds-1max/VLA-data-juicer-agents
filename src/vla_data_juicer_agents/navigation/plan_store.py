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
    status: Literal["pending"] = "pending"


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
                    status TEXT NOT NULL CHECK (status = 'pending'),
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
            active_row = connection.execute(
                """
                SELECT plan_id FROM navigation_plans
                WHERE task_id = ? AND phase = ? AND status = 'active'
                """,
                (task.task_id, phase_value),
            ).fetchone()
            if active_row is not None and self._active_plan_has_in_flight_work(
                connection,
                active_row["plan_id"],
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
            WHERE plan_id = ? LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        return handoff is not None

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
                SELECT steps.task_id, steps.plan_revision
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
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
                SELECT steps.task_id, steps.plan_revision
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    plan_id,
                    step_id,
                    row["task_id"],
                    row["plan_revision"],
                    target_status,
                    self._canonical_json(["pending", "waiting_user"]),
                    self._canonical_json(full_result),
                    self._canonical_json(result_summary),
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
                """,
                (plan_id, step_id, decision_key),
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

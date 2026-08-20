from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from vla_data_juicer_agents.navigation.catalog import (
    list_navigation_tool_capabilities,
)

from vla_data_juicer_agents.navigation.plan_models import (
    ExecutionStepRecord,
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
    NavigationPlanRecord,
    PlanSubmissionAttempt,
    TrajectoryReviewPlanInput,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask, utc_now
from vla_data_juicer_agents.navigation.schema import initialize_navigation_schema
from vla_data_juicer_agents.navigation.task_store import (
    NavigationTaskNotCurrentError,
    SqliteNavigationTaskStore,
    authorize_navigation_task_write,
    navigation_targets_overlap,
    normalize_segments,
)
from vla_data_juicer_agents.navigation.planning_context import (
    compute_planning_context_revision,
)


PLAN_CONTRACT_VERSION = "navigation-plan-v4"
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
PlanPhase = Literal["extract_sync", "finish_processing", "trajectory_review"]
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


class StepClaimOutcome(str, Enum):
    CLAIMED = "claimed"
    NOT_CLAIMABLE = "not_claimable"
    NAVIGATION_DATA_BUSY = "navigation_data_busy"

    def __bool__(self) -> bool:
        return self is StepClaimOutcome.CLAIMED


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


class ActionablePlanStep(_StrictReadModel):
    """Minimal model-facing identity for one authorized execution step."""

    plan_id: str
    step_id: str
    action: str
    status: ExecutionStatus


def project_actionable_plan_step(current: dict[str, Any]) -> dict[str, Any]:
    """Remove ledger identifiers and result metadata from a current-step snapshot."""
    step = current.get("step")
    if not isinstance(step, dict):
        raise ValueError("current navigation step is malformed")
    return ActionablePlanStep(
        plan_id=current["plan_id"],
        step_id=step["step_id"],
        action=step["action"],
        status=step["status"],
    ).model_dump(mode="json")


@dataclass(frozen=True)
class NavigationExecutionSnapshot:
    task: NavigationTask
    active_plan: NavigationPlanRecord | None
    overview: CompactExecutionOverview | None
    current: dict[str, Any] | None
    dependency_statuses: dict[str, ExecutionStatus]
    staged_result: StagedStepResult | None
    handoff: HumanDecisionHandoff | None
    activity: Literal["planning", "execution", "recovery_required"]


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

    @staticmethod
    def _authorize_plan_write(
        connection: sqlite3.Connection,
        plan_id: str,
        *,
        expected_web_session_id: str | None,
        expected_agentscope_session_id: str | None,
    ) -> None:
        row = connection.execute(
            "SELECT task_id FROM navigation_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is not None:
            authorize_navigation_task_write(
                connection,
                row["task_id"],
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )

    @staticmethod
    def _authorize_claim_terminalization(
        connection: sqlite3.Connection,
        plan_id: str,
        step_id: str,
        action: str,
        *,
        expected_web_session_id: str | None,
        expected_agentscope_session_id: str | None,
    ) -> sqlite3.Row:
        """Authorize completion of an already-durable execution claim.

        Starting new work is fenced to the newest attempt elsewhere. Once a step is
        durably claimed, however, its exact owner must be able to record the result
        even if that same owner creates a newer attempt while the action is running.
        """
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                action,
                expected_web_session_id,
                expected_agentscope_session_id,
            )
        ):
            raise PermissionError("navigation task session mismatch")
        row = connection.execute(
            """SELECT steps.task_id, steps.status, steps.tool_name,
                      plans.status AS plan_status
               FROM navigation_task_steps AS steps
               JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
               JOIN navigation_tasks AS tasks ON tasks.task_id = steps.task_id
               WHERE steps.plan_id = ? AND steps.step_id = ?
                 AND steps.tool_name = ? AND plans.status = 'active'
                 AND tasks.created_by_web_session_id = ?
                 AND tasks.agentscope_session_id = ?""",
            (
                plan_id,
                step_id,
                action,
                expected_web_session_id,
                expected_agentscope_session_id,
            ),
        ).fetchone()
        if row is None:
            raise PermissionError("navigation task session mismatch")
        return row

    @staticmethod
    def _authorize_handoff_terminalization(
        connection: sqlite3.Connection,
        plan_id: str,
        step_id: str,
        *,
        expected_web_session_id: str | None,
        expected_agentscope_session_id: str | None,
    ) -> sqlite3.Row:
        """Authorize delivery/recovery of an already-durable human handoff."""
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                expected_web_session_id,
                expected_agentscope_session_id,
            )
        ):
            raise PermissionError("navigation task session mismatch")
        row = connection.execute(
            """SELECT handoffs.task_id, handoffs.status, handoffs.delivery_status,
                      steps.tool_name
               FROM navigation_human_decision_handoffs AS handoffs
               JOIN navigation_task_steps AS steps
                 ON steps.plan_id = handoffs.plan_id
                AND steps.step_id = handoffs.step_id
               JOIN navigation_tasks AS tasks ON tasks.task_id = handoffs.task_id
               WHERE handoffs.plan_id = ? AND handoffs.step_id = ?
                 AND tasks.created_by_web_session_id = ?
                 AND tasks.agentscope_session_id = ?""",
            (
                plan_id,
                step_id,
                expected_web_session_id,
                expected_agentscope_session_id,
            ),
        ).fetchone()
        if row is None:
            raise PermissionError("navigation task session mismatch")
        return row

    def _init_schema(self) -> None:
        initialize_navigation_schema(self.db_path)

    def record_attempt(self, attempt: PlanSubmissionAttempt, *,
                       expected_web_session_id: str | None = None,
                       expected_agentscope_session_id: str | None = None) -> PlanSubmissionAttempt:
        stored = PlanSubmissionAttempt.model_validate(attempt)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            authorize_navigation_task_write(
                connection,
                stored.task_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
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
        plan: (
            ExtractSyncPlanInput
            | FinishProcessingPlanInput
            | TrajectoryReviewPlanInput
            | dict[str, Any]
        ),
        *, expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
        expected_planning_context_revision: str | None = None,
        capability_revision: str | None = None,
    ) -> NavigationPlanRecord:
        phase_value = self._normalize_phase(phase)
        canonical_plan = self._validate_plan_for_phase(phase_value, plan)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task_row = connection.execute(
                "SELECT * FROM navigation_tasks WHERE task_id=?", (task.task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(task.task_id)
            authorize_navigation_task_write(
                connection,
                task.task_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
            if expected_planning_context_revision is not None:
                if capability_revision is None:
                    raise ValueError("capability revision is required for context fencing")
                latest_observation = connection.execute(
                    """SELECT MAX(revision) AS revision
                       FROM navigation_observation_revisions WHERE task_id = ?""",
                    (task.task_id,),
                ).fetchone()
                if (
                    latest_observation is None
                    or latest_observation["revision"] is None
                    or int(latest_observation["revision"]) != observation_revision
                ):
                    raise RuntimeError("navigation planning context observation changed")
                durable_task = SqliteNavigationTaskStore(
                    self.db_path, initialize=False
                )._task_from_row(task_row)
                durable_revision = compute_planning_context_revision(
                    task=durable_task,
                    observation_revision=observation_revision,
                    capability_revision=capability_revision,
                )
                if durable_revision != expected_planning_context_revision:
                    raise RuntimeError("navigation planning context changed")
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
            if self._task_has_in_flight_work(
                connection,
                task.task_id,
            ):
                raise ActivePlanExecutionConflict(
                    "cannot supersede an active navigation plan with running, "
                    "waiting-user, or staged outbox work"
                )
            connection.execute(
                """
                UPDATE navigation_plans
                SET status = 'superseded', updated_at = ?
                WHERE task_id = ? AND status = 'active'
                """,
                (timestamp, task.task_id),
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
            cursor = connection.execute(
                """UPDATE navigation_tasks
                   SET accepted_plan_phase = ?, updated_at = ?,
                       state_revision = state_revision + 1
                   WHERE task_id = ?""",
                (phase_value, timestamp, task.task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task.task_id)
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
    def _task_has_in_flight_work(
        connection: sqlite3.Connection,
        task_id: str,
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
            WHERE plans.task_id = ?
              AND (
                steps.status IN ('running', 'waiting_user')
                OR outbox.plan_id IS NOT NULL
                OR (handoffs.plan_id IS NOT NULL AND handoffs.status != 'quarantined')
              )
            LIMIT 1
            """,
            (task_id,),
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

    def get_active_for_task(self, task_id: str) -> NavigationPlanRecord | None:
        """Return only the plan selected by the task's durable accepted phase."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT plans.*
                   FROM navigation_tasks AS tasks
                   JOIN navigation_plans AS plans
                     ON plans.task_id = tasks.task_id
                    AND plans.phase = tasks.accepted_plan_phase
                   WHERE tasks.task_id = ? AND plans.status = 'active'""",
                (task_id,),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def get_latest_accepted_for_task(self, task_id: str) -> NavigationPlanRecord | None:
        """Return the newest active/completed Plan for the task's accepted phase."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT plans.*
                   FROM navigation_tasks AS tasks
                   JOIN navigation_plans AS plans
                     ON plans.task_id = tasks.task_id
                    AND plans.phase = tasks.accepted_plan_phase
                   WHERE tasks.task_id = ?
                     AND plans.status IN ('active', 'completed')
                   ORDER BY plans.updated_at DESC, plans.rowid DESC
                   LIMIT 1""",
                (task_id,),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def get_finish_observation_revision_floor(self, task_id: str) -> int:
        """Return the observation fence created by the latest completed extract Plan.

        Finish facts captured at or before this revision may describe artifacts
        while extract/sync was still running, so they must be refreshed.
        """
        with self._connect() as connection:
            plan_row = connection.execute(
                """
                SELECT updated_at
                FROM navigation_plans
                WHERE task_id = ?
                  AND phase = 'extract_sync'
                  AND status = 'completed'
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if plan_row is None:
                return 0
            observation_row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM navigation_observation_revisions
                WHERE task_id = ? AND julianday(created_at) <= julianday(?)
                """,
                (task_id, plan_row["updated_at"]),
            ).fetchone()
        return int(observation_row["revision"])

    def read_execution_snapshot(
        self,
        *,
        web_session_id: str | None,
        agentscope_session_id: str | None,
        task_id: str | None = None,
    ) -> NavigationExecutionSnapshot | None:
        """Read exact attempt ownership and its accepted execution state once."""
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            params: tuple[Any, ...] = (web_session_id, agentscope_session_id)
            task_row = connection.execute(
                """SELECT * FROM navigation_tasks
                    WHERE created_by_web_session_id IS ?
                      AND agentscope_session_id IS ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1""",
                params,
            ).fetchone()
            if task_row is None or (
                task_id is not None and task_row["task_id"] != task_id
            ):
                connection.commit()
                return None
            task = SqliteNavigationTaskStore(
                self.db_path,
                initialize=False,
            )._task_from_row(task_row)
            plan_row = connection.execute(
                """SELECT plans.*
                   FROM navigation_plans AS plans
                   WHERE plans.task_id = ?
                     AND plans.phase = ?
                     AND plans.status = 'active'
                   LIMIT 1""",
                (
                    task.task_id,
                    task.accepted_plan_phase,
                ),
            ).fetchone()
            if plan_row is None:
                connection.commit()
                return NavigationExecutionSnapshot(
                    task=task,
                    active_plan=None,
                    overview=None,
                    current=None,
                    dependency_statuses={},
                    staged_result=None,
                    handoff=None,
                    activity="planning",
                )
            plan = self._record_from_row(plan_row)
            step_rows = connection.execute(
                """SELECT id, plan_id, plan_revision, sequence, step_id,
                          tool_name, status, result_summary_json, result_ref,
                          retry_count
                   FROM navigation_task_steps
                   WHERE plan_id = ? AND plan_revision = ?
                   ORDER BY sequence ASC""",
                (plan.plan_id, plan.plan_revision),
            ).fetchall()
            compact_steps = [
                CompactExecutionStep(
                    step_id=row["step_id"],
                    action=row["tool_name"],
                    status=row["status"],
                )
                for row in step_rows
            ]
            overview = CompactExecutionOverview(
                plan_id=plan.plan_id,
                plan_revision=plan.plan_revision,
                status=plan.status,
                total_steps=len(compact_steps),
                completed_steps=sum(
                    step.status == "completed" for step in compact_steps
                ),
                current_step_id=next(
                    (
                        step.step_id
                        for step in compact_steps
                        if step.status != "completed"
                    ),
                    None,
                ),
                steps=compact_steps,
            )
            current_row = next(
                (row for row in step_rows if row["status"] != "completed"),
                None,
            )
            current: dict[str, Any] | None = None
            dependencies: dict[str, ExecutionStatus] = {}
            staged: StagedStepResult | None = None
            handoff: HumanDecisionHandoff | None = None
            if current_row is not None:
                step_record = ExecutionStepRecord(
                    id=current_row["id"],
                    plan_id=current_row["plan_id"],
                    plan_revision=current_row["plan_revision"],
                    sequence=current_row["sequence"],
                    step_id=current_row["step_id"],
                    action=current_row["tool_name"],
                    status=current_row["status"],
                    result_summary=(
                        json.loads(current_row["result_summary_json"])
                        if current_row["result_summary_json"] is not None
                        else None
                    ),
                    result_ref=current_row["result_ref"],
                    retry_count=current_row["retry_count"],
                )
                plan_step = next(
                    (
                        item
                        for item in plan.plan.steps
                        if item.step_id == step_record.step_id
                    ),
                    None,
                )
                decision_refs = (
                    list(plan_step.decision_refs) if plan_step is not None else []
                )
                current = {
                    "plan_id": plan.plan_id,
                    "plan_revision": plan.plan_revision,
                    "step": step_record.model_dump(mode="json"),
                    "decision_refs": decision_refs,
                }
                dependency_ids = (
                    list(plan_step.depends_on) if plan_step is not None else []
                )
                if dependency_ids:
                    placeholders = ",".join("?" for _ in dependency_ids)
                    dependency_rows = connection.execute(
                        f"""SELECT step_id, status FROM navigation_task_steps
                            WHERE plan_id = ? AND step_id IN ({placeholders})""",
                        (plan.plan_id, *dependency_ids),
                    ).fetchall()
                    dependencies = {
                        row["step_id"]: cast(ExecutionStatus, row["status"])
                        for row in dependency_rows
                    }
                staged_row = connection.execute(
                    """SELECT * FROM navigation_step_result_outbox
                       WHERE plan_id = ? AND step_id = ?""",
                    (plan.plan_id, step_record.step_id),
                ).fetchone()
                if staged_row is not None:
                    staged = self._staged_result_from_row(staged_row)
                handoff_row = connection.execute(
                    """SELECT * FROM navigation_human_decision_handoffs
                       WHERE plan_id = ? AND step_id = ?""",
                    (plan.plan_id, step_record.step_id),
                ).fetchone()
                if handoff_row is not None:
                    handoff = self._handoff_from_row(handoff_row)
            current_status = ((current or {}).get("step") or {}).get("status")
            activity: Literal["planning", "execution", "recovery_required"]
            if handoff is not None and handoff.status == "recovery_required":
                activity = "recovery_required"
            elif current_status in {"pending", "running", "waiting_user"}:
                activity = "execution"
            else:
                activity = "planning"
            self._ensure_within_limit(
                overview.model_dump(mode="json"),
                label="execution overview",
            )
            if current is not None:
                self._ensure_within_limit(current, label="current step")
            connection.commit()
            return NavigationExecutionSnapshot(
                task=task,
                active_plan=plan,
                overview=overview,
                current=current,
                dependency_statuses=dependencies,
                staged_result=staged,
                handoff=handoff,
                activity=activity,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_claim_terminalization_snapshot(
        self,
        *,
        plan_id: str,
        step_id: str,
        action: str,
        expected_web_session_id: str | None,
        expected_agentscope_session_id: str | None,
    ) -> NavigationExecutionSnapshot | None:
        """Read only a durable running claim whose result is already staged.

        This is the retry capability for finishing an in-flight side effect after
        its owner created a newer Attempt. It never makes pending work executable.
        """
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                plan_id,
                step_id,
                action,
                expected_web_session_id,
                expected_agentscope_session_id,
            )
        ):
            return None
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            task_row = connection.execute(
                """SELECT tasks.*
                   FROM navigation_tasks AS tasks
                   JOIN navigation_plans AS plans ON plans.task_id = tasks.task_id
                   JOIN navigation_task_steps AS steps
                     ON steps.plan_id = plans.plan_id
                   JOIN navigation_step_result_outbox AS outbox
                     ON outbox.plan_id = steps.plan_id
                    AND outbox.step_id = steps.step_id
                    AND outbox.task_id = steps.task_id
                    AND outbox.plan_revision = steps.plan_revision
                   WHERE plans.plan_id = ? AND steps.step_id = ?
                     AND steps.tool_name = ? AND steps.status = 'running'
                     AND plans.status = 'active'
                     AND tasks.created_by_web_session_id = ?
                     AND tasks.agentscope_session_id = ?
                     AND EXISTS (
                         SELECT 1 FROM json_each(outbox.expected_statuses_json)
                         WHERE json_each.value = 'running'
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM navigation_task_steps AS prior
                         WHERE prior.plan_id = steps.plan_id
                           AND prior.sequence < steps.sequence
                           AND prior.status != 'completed'
                     )""",
                (
                    plan_id,
                    step_id,
                    action,
                    expected_web_session_id,
                    expected_agentscope_session_id,
                ),
            ).fetchone()
            if task_row is None:
                connection.commit()
                return None
            plan_row = connection.execute(
                "SELECT * FROM navigation_plans WHERE plan_id = ? AND status = 'active'",
                (plan_id,),
            ).fetchone()
            staged_row = connection.execute(
                """SELECT * FROM navigation_step_result_outbox
                   WHERE plan_id = ? AND step_id = ?""",
                (plan_id, step_id),
            ).fetchone()
            if plan_row is None or staged_row is None:
                connection.commit()
                return None
            task = SqliteNavigationTaskStore(
                self.db_path,
                initialize=False,
            )._task_from_row(task_row)
            plan = self._record_from_row(plan_row)
            step_rows = connection.execute(
                """SELECT id, plan_id, plan_revision, sequence, step_id,
                          tool_name, status, result_summary_json, result_ref,
                          retry_count
                   FROM navigation_task_steps
                   WHERE plan_id = ? AND plan_revision = ?
                   ORDER BY sequence ASC""",
                (plan.plan_id, plan.plan_revision),
            ).fetchall()
            claimed_row = next(
                (row for row in step_rows if row["step_id"] == step_id),
                None,
            )
            if claimed_row is None or claimed_row["status"] != "running":
                connection.commit()
                return None
            compact_steps = [
                CompactExecutionStep(
                    step_id=row["step_id"],
                    action=row["tool_name"],
                    status=row["status"],
                )
                for row in step_rows
            ]
            overview = CompactExecutionOverview(
                plan_id=plan.plan_id,
                plan_revision=plan.plan_revision,
                status=plan.status,
                total_steps=len(compact_steps),
                completed_steps=sum(
                    item.status == "completed" for item in compact_steps
                ),
                current_step_id=step_id,
                steps=compact_steps,
            )
            step_record = ExecutionStepRecord(
                id=claimed_row["id"],
                plan_id=claimed_row["plan_id"],
                plan_revision=claimed_row["plan_revision"],
                sequence=claimed_row["sequence"],
                step_id=claimed_row["step_id"],
                action=claimed_row["tool_name"],
                status=claimed_row["status"],
                result_summary=(
                    json.loads(claimed_row["result_summary_json"])
                    if claimed_row["result_summary_json"] is not None
                    else None
                ),
                result_ref=claimed_row["result_ref"],
                retry_count=claimed_row["retry_count"],
            )
            plan_step = next(
                (item for item in plan.plan.steps if item.step_id == step_id),
                None,
            )
            if plan_step is None or plan_step.action != action:
                connection.commit()
                return None
            dependency_ids = list(plan_step.depends_on)
            dependencies: dict[str, ExecutionStatus] = {}
            if dependency_ids:
                placeholders = ",".join("?" for _ in dependency_ids)
                dependency_rows = connection.execute(
                    f"""SELECT step_id, status FROM navigation_task_steps
                        WHERE plan_id = ? AND step_id IN ({placeholders})""",
                    (plan_id, *dependency_ids),
                ).fetchall()
                dependencies = {
                    row["step_id"]: cast(ExecutionStatus, row["status"])
                    for row in dependency_rows
                }
            current = {
                "plan_id": plan.plan_id,
                "plan_revision": plan.plan_revision,
                "step": step_record.model_dump(mode="json"),
                "decision_refs": list(plan_step.decision_refs),
            }
            staged = self._staged_result_from_row(staged_row)
            self._ensure_within_limit(
                overview.model_dump(mode="json"),
                label="execution overview",
            )
            self._ensure_within_limit(current, label="current step")
            connection.commit()
            return NavigationExecutionSnapshot(
                task=task,
                active_plan=plan,
                overview=overview,
                current=current,
                dependency_statuses=dependencies,
                staged_result=staged,
                handoff=None,
                activity="execution",
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, plan_id: str) -> NavigationPlanRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM navigation_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def invalidate(
        self,
        plan_id: str,
        reason: str,
        *,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> NavigationPlanRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, phase FROM navigation_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError(plan_id)
            self._authorize_plan_write(
                connection,
                plan_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
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
    ) -> StepClaimOutcome:
        """Atomically claim one pending ledger step for exactly-once invocation."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._authorize_plan_write(
                    connection,
                    plan_id,
                    expected_web_session_id=expected_web_session_id,
                    expected_agentscope_session_id=expected_agentscope_session_id,
                )
            except NavigationTaskNotCurrentError:
                connection.rollback()
                return StepClaimOutcome.NOT_CLAIMABLE
            candidate = connection.execute(
                """
                SELECT steps.status, tasks.task_id, tasks.dry_run, tasks.date,
                       tasks.segments_json, plans.status AS plan_status
                FROM navigation_task_steps AS steps
                JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                JOIN navigation_tasks AS tasks ON tasks.task_id = steps.task_id
                JOIN json_each(plans.plan_json, '$.steps') AS plan_step
                  ON json_extract(plan_step.value, '$.step_id') = steps.step_id
                 AND json_extract(plan_step.value, '$.action') = steps.tool_name
                WHERE steps.plan_id = ? AND steps.step_id = ? AND steps.tool_name = ?
                """,
                (plan_id, step_id, action),
            ).fetchone()
            if (
                candidate is None
                or candidate["status"] != "pending"
                or candidate["plan_status"] != "active"
            ):
                connection.rollback()
                return StepClaimOutcome.NOT_CLAIMABLE

            locking_actions = {
                capability.tool_name
                for capability in list_navigation_tool_capabilities()
                if capability.locks_navigation_target
            }
            if not bool(candidate["dry_run"]) and action in locking_actions:
                placeholders = ", ".join("?" for _ in locking_actions)
                running_rows = connection.execute(
                    f"""
                    SELECT tasks.date, tasks.segments_json
                    FROM navigation_task_steps AS steps
                    JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                    JOIN navigation_tasks AS tasks ON tasks.task_id = steps.task_id
                    JOIN json_each(plans.plan_json, '$.steps') AS plan_step
                      ON json_extract(plan_step.value, '$.step_id') = steps.step_id
                     AND json_extract(plan_step.value, '$.action') = steps.tool_name
                    WHERE steps.status = 'running'
                      AND plans.status = 'active'
                      AND tasks.dry_run = 0
                      AND tasks.date = ?
                      AND steps.tool_name IN ({placeholders})
                      AND NOT (steps.plan_id = ? AND steps.step_id = ?)
                    """,
                    (
                        candidate["date"],
                        *sorted(locking_actions),
                        plan_id,
                        step_id,
                    ),
                ).fetchall()
                candidate_segments = normalize_segments(
                    json.loads(candidate["segments_json"])
                    if candidate["segments_json"] is not None
                    else None
                )
                for running in running_rows:
                    running_segments = normalize_segments(
                        json.loads(running["segments_json"])
                        if running["segments_json"] is not None
                        else None
                    )
                    if navigation_targets_overlap(
                        left_date=candidate["date"],
                        left_segments=candidate_segments,
                        right_date=running["date"],
                        right_segments=running_segments,
                    ):
                        connection.rollback()
                        return StepClaimOutcome.NAVIGATION_DATA_BUSY

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
                        AND navigation_tasks.created_by_web_session_id IS ?
                        AND navigation_tasks.agentscope_session_id IS ?
                  )
                """,
                (
                    utc_now(), plan_id, step_id, action,
                    expected_web_session_id,
                    expected_agentscope_session_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return StepClaimOutcome.NOT_CLAIMABLE
            connection.commit()
            return StepClaimOutcome.CLAIMED
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_plan_write(
                connection,
                plan_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
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
                        AND navigation_tasks.created_by_web_session_id IS ?
                        AND navigation_tasks.agentscope_session_id IS ?
                  )
                """,
                (
                    utc_now(), plan_id, step_id, action,
                    expected_web_session_id,
                    expected_agentscope_session_id,
                ),
            )
        return cursor.rowcount == 1

    def resume_waiting_workflow_step(
        self,
        plan_id: str,
        step_id: str,
        action: str,
        *,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Atomically transfer one external workflow from user wait to Runtime."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_plan_write(
                connection,
                plan_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
            row = connection.execute(
                """
                SELECT steps.status AS step_status, tasks.status AS task_status,
                       tasks.task_id
                FROM navigation_task_steps AS steps
                JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                JOIN navigation_tasks AS tasks ON tasks.task_id = steps.task_id
                WHERE steps.plan_id = ? AND steps.step_id = ?
                  AND steps.tool_name = ? AND plans.status = 'active'
                """,
                (plan_id, step_id, action),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            if (
                row["step_status"] == "running"
                and row["task_status"] == "active"
            ):
                connection.commit()
                return True
            if (
                row["step_status"] != "waiting_user"
                or row["task_status"] not in {"waiting_user", "active"}
            ):
                connection.rollback()
                return False
            timestamp = utc_now()
            cursor = connection.execute(
                """
                UPDATE navigation_task_steps
                SET status = 'running', started_at = COALESCE(started_at, ?)
                WHERE plan_id = ? AND step_id = ? AND tool_name = ?
                  AND status = 'waiting_user'
                """,
                (timestamp, plan_id, step_id, action),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE navigation_tasks
                SET status = 'active', updated_at = ?,
                    state_revision = state_revision + 1
                WHERE task_id = ? AND status IN ('waiting_user', 'active')
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

    def mark_workflow_step_waiting_user(
        self,
        plan_id: str,
        step_id: str,
        action: str,
        *,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Atomically transfer an owned external workflow to a human wait."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_plan_write(
                connection,
                plan_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
            row = connection.execute(
                """
                SELECT steps.status AS step_status, tasks.status AS task_status,
                       tasks.task_id
                FROM navigation_task_steps AS steps
                JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                JOIN navigation_tasks AS tasks ON tasks.task_id = steps.task_id
                WHERE steps.plan_id = ? AND steps.step_id = ?
                  AND steps.tool_name = ? AND plans.status = 'active'
                """,
                (plan_id, step_id, action),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            if (
                row["step_status"] == "waiting_user"
                and row["task_status"] == "waiting_user"
            ):
                connection.commit()
                return True
            if (
                row["step_status"] not in {"pending", "running"}
                or row["task_status"] not in {"active", "waiting_user"}
            ):
                connection.rollback()
                return False
            timestamp = utc_now()
            cursor = connection.execute(
                """
                UPDATE navigation_task_steps
                SET status = 'waiting_user',
                    started_at = COALESCE(started_at, ?)
                WHERE plan_id = ? AND step_id = ? AND tool_name = ?
                  AND status = ?
                """,
                (
                    timestamp,
                    plan_id,
                    step_id,
                    action,
                    row["step_status"],
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            task_cursor = connection.execute(
                """
                UPDATE navigation_tasks
                SET status = 'waiting_user', updated_at = ?,
                    state_revision = state_revision + 1
                WHERE task_id = ? AND status IN ('active', 'waiting_user')
                """,
                (timestamp, row["task_id"]),
            )
            if task_cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def stage_step_result(
        self,
        plan_id: str,
        step_id: str,
        *,
        expected_action: str,
        target_status: Literal["completed", "failed"],
        full_result: dict[str, Any],
        result_summary: dict[str, Any],
        expected_statuses: tuple[ExecutionStatus, ...] = ("running",),
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> StagedStepResult:
        """Durably stage a post-side-effect result before crossing to file evidence."""
        self._ensure_within_limit(result_summary, label="execution result summary")
        workflow_handoff_actions = {
            "run_annotation_tracking_workflow",
            "run_annotation_postprocessing_workflow",
            "open_trajectory_fix_workbench",
            "validate_trajectory_review_outcome",
        }
        if expected_statuses != ("running",) and not (
            expected_action in workflow_handoff_actions
            and set(expected_statuses).issubset({"pending", "waiting_user"})
            and expected_statuses
        ):
            raise ValueError(
                "execution results must be staged from a claimed run or "
                "an authorized workflow handoff"
            )
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
            self._authorize_claim_terminalization(
                connection,
                plan_id,
                step_id,
                expected_action,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
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
        *, expected_action: str,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Attach task-scoped evidence to a staged result without finishing it."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_claim_terminalization(
                connection, plan_id, step_id, expected_action,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id)
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

    def finalize_staged_step(
        self, plan_id: str, step_id: str, *,
        expected_action: str,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Atomically finish one staged ledger step and clear its result outbox."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_claim_terminalization(
                connection, plan_id, step_id, expected_action,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id)
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
                    plan_completed_at = datetime.now(UTC).isoformat()
                    connection.execute(
                        """
                        UPDATE navigation_plans
                        SET status = 'completed', updated_at = ?
                        WHERE plan_id = ? AND status = 'active'
                        """,
                        (plan_completed_at, plan_id),
                    )
                    plan_row = connection.execute(
                        "SELECT phase, task_id FROM navigation_plans WHERE plan_id = ?",
                        (plan_id,),
                    ).fetchone()
                    if plan_row is not None:
                        terminal_validation = connection.execute(
                            """SELECT 1
                               FROM navigation_task_steps
                               WHERE plan_id = ?
                                 AND tool_name = 'validate_navigation_outputs'
                                 AND status = 'completed'
                                 AND sequence = (
                                     SELECT MAX(sequence) FROM navigation_task_steps
                                     WHERE plan_id = ?
                                 )""",
                            (plan_id, plan_id),
                        ).fetchone()
                        attempt_status = "active"
                        if (
                            plan_row["phase"] == "finish_processing"
                            and terminal_validation is not None
                        ):
                            attempt_status = "completed"
                            self._record_task_outcome_in_transaction(
                                connection,
                                task_id=plan_row["task_id"],
                                completion_outcome=(
                                    "postprocessing_completed_fix_pending"
                                ),
                            )
                        elif plan_row["phase"] == "trajectory_review":
                            review_validation = connection.execute(
                                """SELECT 1
                                   FROM navigation_task_steps
                                   WHERE plan_id = ?
                                     AND tool_name =
                                         'validate_trajectory_review_outcome'
                                     AND status = 'completed'
                                     AND sequence = (
                                         SELECT MAX(sequence)
                                         FROM navigation_task_steps
                                         WHERE plan_id = ?
                                     )""",
                                (plan_id, plan_id),
                            ).fetchone()
                            if review_validation is not None:
                                attempt_status = "completed"
                                self._record_task_outcome_in_transaction(
                                    connection,
                                    task_id=plan_row["task_id"],
                                    completion_outcome=(
                                        "trajectory_review_completed"
                                    ),
                                )
                        connection.execute(
                            """UPDATE navigation_tasks
                               SET status = ?, updated_at = ?
                               WHERE task_id = ?""",
                            (attempt_status, utc_now(), plan_row["task_id"]),
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
        *, expected_action: str,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Conservatively invalidate an unrecoverable running step without rerunning it."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_claim_terminalization(
                connection, plan_id, step_id, expected_action,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id)
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
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
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
            self._authorize_plan_write(
                connection,
                plan_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
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
        return self._handoff_from_row(row)

    @staticmethod
    def _handoff_from_row(row: sqlite3.Row) -> HumanDecisionHandoff:
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
        *, expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_handoff_terminalization(
                connection, plan_id, step_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id)
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
        self, plan_id: str, step_id: str, decision_key: str, *, owner: str,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> tuple[
        Literal["claimed", "busy", "delivered", "missing", "recovery_required"],
        str | None,
    ]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_handoff_terminalization(
                connection, plan_id, step_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id)
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
        request_anchor: dict[str, Any] | None = None,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
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
            if row is None:
                self._authorize_plan_write(
                    connection,
                    plan_id,
                    expected_web_session_id=expected_web_session_id,
                    expected_agentscope_session_id=expected_agentscope_session_id,
                )
            else:
                self._authorize_handoff_terminalization(
                    connection,
                    plan_id,
                    step_id,
                    expected_web_session_id=expected_web_session_id,
                    expected_agentscope_session_id=expected_agentscope_session_id,
                )
            if row is None and request_anchor is not None:
                canonical_anchor = self._canonical_json(request_anchor)
                if len(canonical_anchor.encode("utf-8")) > MAX_HUMAN_DECISION_CHARS:
                    raise ValueError(
                        f"human decision request anchor exceeds {MAX_HUMAN_DECISION_CHARS} byte limit"
                    )
                step_row = connection.execute(
                    """SELECT steps.task_id
                       FROM navigation_task_steps AS steps
                       JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                       WHERE steps.plan_id = ? AND steps.step_id = ?
                         AND steps.tool_name = 'confirm_navigation_calibration_params'
                         AND steps.status = 'waiting_user'
                         AND plans.status = 'active'""",
                    (plan_id, step_id),
                ).fetchone()
                if step_row is None:
                    connection.rollback()
                    return False
                timestamp = utc_now()
                connection.execute(
                    """INSERT INTO navigation_human_decision_handoffs (
                           plan_id, step_id, task_id, decision_key, decision_json,
                           status, delivery_status, recovery_reason_code,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, 'recovery_required',
                           'recovery_required', ?, ?, ?)""",
                    (
                        plan_id,
                        step_id,
                        step_row["task_id"],
                        hashlib.sha256(canonical_anchor.encode("utf-8")).hexdigest(),
                        canonical_anchor,
                        safe_code,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.commit()
                return True
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
                          handoffs.task_id, tasks.created_by_web_session_id,
                          tasks.agentscope_session_id
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
            self._authorize_handoff_terminalization(
                connection,
                plan_id,
                step_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=row["agentscope_session_id"],
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
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_handoff_terminalization(
                connection, plan_id, step_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id)
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
        self, plan_id: str, step_id: str, decision_key: str, *,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Record durable AgentScope consumption observed outside our lease state."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_handoff_terminalization(
                connection, plan_id, step_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id)
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

    def mark_needs_replan(
        self, plan_id: str, reason: str, *,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Invalidate one active plan and its unfinished ledger in one transaction."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_plan_write(connection, plan_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id)
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

    def mark_terminalized_claim_needs_replan(
        self,
        plan_id: str,
        step_id: str,
        reason: str,
        *,
        expected_action: str,
        expected_web_session_id: str | None = None,
        expected_agentscope_session_id: str | None = None,
    ) -> bool:
        """Invalidate after a claimed step durably finalized an unusable result."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_claim_terminalization(
                connection,
                plan_id,
                step_id,
                expected_action,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
            row = connection.execute(
                """SELECT plans.task_id
                   FROM navigation_task_steps AS steps
                   JOIN navigation_plans AS plans ON plans.plan_id = steps.plan_id
                   WHERE steps.plan_id = ? AND steps.step_id = ?
                     AND steps.status IN ('completed', 'failed')
                     AND steps.result_ref IS NOT NULL
                     AND plans.status = 'active'
                     AND NOT EXISTS (
                         SELECT 1 FROM navigation_step_result_outbox AS outbox
                         WHERE outbox.plan_id = steps.plan_id
                           AND outbox.step_id = steps.step_id
                     )""",
                (plan_id, step_id),
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
                """UPDATE navigation_plans
                   SET status = 'invalidated', invalidation_reason = ?, updated_at = ?
                   WHERE plan_id = ? AND status = 'active'""",
                (reason, timestamp, plan_id),
            )
            connection.execute(
                """UPDATE navigation_task_steps
                   SET status = 'needs_replan', finished_at = ?
                   WHERE plan_id = ? AND status != 'completed'""",
                (timestamp, plan_id),
            )
            connection.execute(
                """UPDATE navigation_tasks
                   SET status = 'needs_replan', updated_at = ?,
                       state_revision = state_revision + 1
                   WHERE task_id = ?""",
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

    @staticmethod
    def _record_task_outcome_in_transaction(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        completion_outcome: str,
    ) -> None:
        timestamp = utc_now()
        connection.execute(
            """INSERT INTO navigation_task_outcomes (
                   task_id, requested_outcome, completion_outcome, revision,
                   metadata_json, created_at, updated_at
               ) VALUES (?, 'auto', ?, 1, '{}', ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                   completion_outcome = excluded.completion_outcome,
                   revision = navigation_task_outcomes.revision + 1,
                   updated_at = excluded.updated_at""",
            (task_id, completion_outcome, timestamp, timestamp),
        )

    def _require_plan(self, plan_id: str) -> NavigationPlanRecord:
        plan = self.get(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan

    @staticmethod
    def _normalize_phase(phase: PlanPhase | str) -> PlanPhase:
        value = str(phase)
        if value not in {
            "extract_sync",
            "finish_processing",
            "trajectory_review",
        }:
            raise ValueError(f"unsupported navigation plan phase: {value}")
        return cast(PlanPhase, value)

    @staticmethod
    def _validate_plan_for_phase(
        phase: PlanPhase,
        plan: (
            ExtractSyncPlanInput
            | FinishProcessingPlanInput
            | TrajectoryReviewPlanInput
            | dict[str, Any]
        ),
    ) -> ExtractSyncPlanInput | FinishProcessingPlanInput | TrajectoryReviewPlanInput:
        model = {
            "extract_sync": ExtractSyncPlanInput,
            "finish_processing": FinishProcessingPlanInput,
            "trajectory_review": TrajectoryReviewPlanInput,
        }[phase]
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
            if row["phase"] == "finish_processing"
            else TrajectoryReviewPlanInput
        )
        plan_payload = json.loads(row["plan_json"])
        if (
            row["contract_version"]
            in {"navigation-plan-v1", "navigation-plan-v2"}
            and row["phase"] == "extract_sync"
        ):
            time_sync = plan_payload.get("decisions", {}).get("time_sync")
            if isinstance(time_sync, dict):
                time_sync.pop("tolerance_ms", None)
        if row["contract_version"] == "navigation-plan-v1":
            for step in plan_payload.get("steps", []):
                if not isinstance(step, dict):
                    continue
                step.setdefault("depends_on", [])
                step.setdefault("failure_policy", "stop")
                step.setdefault("decision_refs", [])
                step.setdefault("arguments", {})
        return NavigationPlanRecord(
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            phase=row["phase"],
            plan_revision=row["plan_revision"],
            contract_version=row["contract_version"],
            observation_revision=row["observation_revision"],
            status=row["status"],
            plan=plan_model.model_validate(plan_payload),
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

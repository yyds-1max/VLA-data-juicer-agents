import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    PlanSubmissionAttempt,
)
from vla_data_juicer_agents.navigation.plan_store import SqliteNavigationPlanRepository
from vla_data_juicer_agents.navigation.plan_store import ActivePlanExecutionConflict
from vla_data_juicer_agents.navigation.task_state import NavigationTask
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


def valid_extract_plan(*, decision_ref: str = "sensor_bindings") -> ExtractSyncPlanInput:
    return ExtractSyncPlanInput.model_validate(
        {
            "decisions": {
                "sensor_bindings": {
                    "bindings": {
                        "fisheye_front": "/camera/front/image",
                        "lidar": "/lidar/points",
                        "odom": "/localization/odom",
                    },
                    "reason": "Observed matching message types and rates.",
                    "evidence_refs": ["evidence:sensors"],
                },
                "topic_selection": {
                    "topic_whitelist": [
                        "/camera/front/image",
                        "/lidar/points",
                        "/localization/odom",
                    ],
                    "topic_map": {
                        "/camera/front/image": "fisheye_front",
                        "/lidar/points": "lidar",
                        "/localization/odom": "odom",
                    },
                    "query_dir": "/data/query",
                    "reason": "All selected topics were observed.",
                    "evidence_refs": ["evidence:topics"],
                },
                "time_sync": {
                    "reference_sensor": "lidar",
                    "method": "nearest_timestamp",
                    "tolerance_ms": 50,
                    "reason": "Lidar timestamps cover the selected streams.",
                    "evidence_refs": ["evidence:timing"],
                },
            },
            "steps": [
                {
                    "step_id": "prepare",
                    "action": "prepare_raw_data",
                    "variant": "default",
                    "arguments": {},
                    "depends_on": [],
                    "failure_policy": "stop",
                    "decision_refs": [],
                },
                {
                    "step_id": "sync",
                    "action": "extract_and_sync_navigation_data",
                    "variant": "explicit_topic_params",
                    "arguments": {"processes_num": 8},
                    "depends_on": ["prepare"],
                    "failure_policy": "stop",
                    "decision_refs": [decision_ref],
                },
            ],
        }
    )


def stores_with_task(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_or_update_task(
        date="20270623",
        segments=["20260623_101010"],
        scene_mode=None,
    )
    return SqliteNavigationPlanRepository(db_path), task


def test_repository_migrates_legacy_pending_only_handoff_schema(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"
    SqliteNavigationTaskStore(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """CREATE TABLE navigation_human_decision_handoffs (
                plan_id TEXT NOT NULL, step_id TEXT NOT NULL, task_id TEXT NOT NULL,
                decision_key TEXT NOT NULL, decision_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status = 'pending'),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, step_id)
            )"""
        )

    SqliteNavigationPlanRepository(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(navigation_human_decision_handoffs)"
            )
        }
    assert {
        "delivery_status",
        "delivery_owner",
        "delivery_token",
        "leased_at",
        "expires_at",
        "recovery_reason_code",
        "recovery_reason",
        "recovered_at",
    } <= columns


def test_repository_migrates_delivery_lease_and_fails_closed_after_expiry(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    plan = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            """INSERT INTO navigation_human_decision_handoffs (
                   plan_id, step_id, task_id, decision_key, decision_json,
                   status, delivery_status, created_at, updated_at
               ) VALUES (?, 'prepare', ?, 'decision', '{}', 'pending',
                         'delivering', ?, ?)""",
            (plan.plan_id, task.task_id, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        connection.execute(
            """UPDATE navigation_human_decision_handoffs
               SET delivery_owner = 'old-owner', delivery_token = 'old-token',
                   leased_at = ?, expires_at = ?
               WHERE plan_id = ? AND step_id = 'prepare'""",
            (
                datetime.now(UTC).isoformat(),
                (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                plan.plan_id,
            ),
        )

    assert repo.claim_human_decision_delivery(
        plan.plan_id, "prepare", "decision", owner="new-owner"
    ) == ("busy", None)
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_human_decision_handoffs SET expires_at = ? WHERE plan_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), plan.plan_id),
        )
    status, token = repo.claim_human_decision_delivery(
        plan.plan_id, "prepare", "decision", owner="new-owner"
    )
    assert status == "recovery_required" and token is None
    handoff = repo.get_human_decision_handoff(plan.plan_id, "prepare")
    assert handoff is not None
    assert handoff.status == "recovery_required"
    assert handoff.delivery_status == "recovery_required"
    assert handoff.delivery_token == "old-token"


def test_controlled_handoff_recovery_preserves_audit_and_unblocks_replacement(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    task = SqliteNavigationTaskStore(repo.db_path).update_task(
        task.task_id,
        created_by_web_session_id="web-owner",
        latest_web_session_id="web-owner",
    )
    plan = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            """INSERT INTO navigation_human_decision_handoffs (
                   plan_id, step_id, task_id, decision_key, decision_json,
                   status, delivery_status, created_at, updated_at
               ) VALUES (?, 'prepare', ?, 'decision', '{"action":"confirm"}',
                         'pending', 'pending', ?, ?)""",
            (plan.plan_id, task.task_id, timestamp, timestamp),
        )

    assert repo.mark_human_decision_recovery_required(
        plan.plan_id, "prepare", reason_code="missing_agentscope_session"
    )
    assert repo.mark_human_decision_recovery_required(
        plan.plan_id, "prepare", reason_code="missing_agentscope_session"
    )
    with pytest.raises(ActivePlanExecutionConflict, match="Web session"):
        repo.quarantine_human_decision_handoff(
            plan.plan_id,
            "prepare",
            expected_web_session_id="wrong-owner",
            reason="operator requested recovery",
        )

    recovered = repo.quarantine_human_decision_handoff(
        plan.plan_id,
        "prepare",
        expected_web_session_id="web-owner",
        reason="operator requested recovery token=do-not-store",
    )

    assert recovered == {
        "recovered": True,
        "plan_id": plan.plan_id,
        "step_id": "prepare",
        "handoff_status": "quarantined",
        "task_status": "needs_replan",
        "next_action": "submit_complete_plan",
    }
    handoff = repo.get_human_decision_handoff(plan.plan_id, "prepare")
    assert handoff is not None
    assert handoff.status == "quarantined"
    assert handoff.delivery_status == "quarantined"
    assert handoff.decision == {"action": "confirm"}
    assert handoff.recovered_at is not None
    assert "do-not-store" not in (handoff.recovery_reason or "")
    assert "[REDACTED]" in (handoff.recovery_reason or "")
    assert repo.get(plan.plan_id).status == "invalidated"
    assert repo.get_current_step(plan.plan_id)["step"]["status"] == "needs_replan"
    task = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert task.status.value == "needs_replan"
    replacement = repo.activate(task, "extract_sync", 2, valid_extract_plan())
    assert replacement.status == "active"
    assert not repo.claim_step(plan.plan_id, "prepare", "prepare_raw_data")


@pytest.mark.parametrize("delivery_status", ["pending", "delivering", "delivered", "quarantined"])
def test_controlled_handoff_recovery_rejects_non_recovery_states(
    tmp_path: Path, delivery_status: str
):
    repo, task = stores_with_task(tmp_path)
    task = SqliteNavigationTaskStore(repo.db_path).update_task(
        task.task_id,
        created_by_web_session_id="web-owner",
        latest_web_session_id="web-owner",
    )
    plan = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    timestamp = datetime.now(UTC).isoformat()
    status = "quarantined" if delivery_status == "quarantined" else "pending"
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            """INSERT INTO navigation_human_decision_handoffs (
                   plan_id, step_id, task_id, decision_key, decision_json,
                   status, delivery_status, created_at, updated_at
               ) VALUES (?, 'prepare', ?, 'decision', '{}', ?, ?, ?, ?)""",
            (plan.plan_id, task.task_id, status, delivery_status, timestamp, timestamp),
        )
    with pytest.raises(ActivePlanExecutionConflict, match="recovery_required"):
        repo.quarantine_human_decision_handoff(
            plan.plan_id,
            "prepare",
            expected_web_session_id="web-owner",
            reason="operator requested recovery",
        )


def test_repository_backfills_legacy_null_result_ref_and_lazy_recovers(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    plan = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    assert repo.claim_step(plan.plan_id, "prepare", "prepare_raw_data")
    staged = repo.stage_step_result(
        plan.plan_id,
        "prepare",
        target_status="completed",
        full_result={"message": "done", "ok": True, "tool_name": "prepare_raw_data"},
        result_summary={"message": "done", "ok": True, "tool_name": "prepare_raw_data"},
    )
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_step_result_outbox SET result_ref = NULL WHERE plan_id = ?",
            (plan.plan_id,),
        )

    reopened = SqliteNavigationPlanRepository(repo.db_path)
    migrated = reopened.get_staged_step_result(plan.plan_id, "prepare")
    assert migrated is not None
    assert migrated.result_ref == staged.result_ref
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_step_result_outbox SET result_ref = NULL WHERE plan_id = ?",
            (plan.plan_id,),
        )
    assert reopened.get_staged_step_result(plan.plan_id, "prepare").result_ref == staged.result_ref
    assert reopened.finalize_staged_step(plan.plan_id, "prepare") is True
    with sqlite3.connect(repo.db_path) as connection:
        status = connection.execute(
            "SELECT status FROM navigation_task_steps WHERE plan_id = ? AND step_id = 'prepare'",
            (plan.plan_id,),
        ).fetchone()[0]
    assert status == "completed"


def test_step_ledger_transitions_increment_task_state_revision(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    plan = repo.activate(task, "extract_sync", 1, valid_extract_plan())

    assert repo.claim_step(plan.plan_id, "prepare", "prepare_raw_data")
    after_claim = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert after_claim.state_revision == task.state_revision + 1

    repo.stage_step_result(
        plan.plan_id,
        "prepare",
        target_status="completed",
        full_result={"message": "done", "ok": True, "tool_name": "prepare_raw_data"},
        result_summary={"message": "done", "ok": True, "tool_name": "prepare_raw_data"},
    )
    assert repo.finalize_staged_step(plan.plan_id, "prepare")
    after_finalize = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert after_finalize.state_revision == after_claim.state_revision + 1

    assert repo.mark_waiting_user(
        plan.plan_id, "sync", "extract_and_sync_navigation_data"
    )
    after_waiting = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert after_waiting.state_revision == after_finalize.state_revision + 1


def test_activate_plan_and_ledger_is_atomic(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)

    record = repo.activate(task, "extract_sync", 3, valid_extract_plan())

    assert repo.get_active(task.task_id, "extract_sync").plan_id == record.plan_id
    assert [
        step.step_id for step in repo.get_execution_overview(record.plan_id).steps
    ] == ["prepare", "sync"]


def test_failed_activation_does_not_supersede_active_plan(
    tmp_path: Path,
    monkeypatch,
):
    repo, task = stores_with_task(tmp_path)
    first = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    monkeypatch.setattr(
        repo,
        "_insert_ledger_rows",
        lambda *args: (_ for _ in ()).throw(sqlite3.IntegrityError()),
    )

    with pytest.raises(sqlite3.IntegrityError):
        repo.activate(task, "extract_sync", 2, valid_extract_plan())

    assert repo.get_active(task.task_id, "extract_sync").plan_id == first.plan_id
    with sqlite3.connect(repo.db_path) as connection:
        plans = connection.execute(
            "SELECT plan_id, status FROM navigation_plans ORDER BY plan_revision"
        ).fetchall()
        steps = connection.execute(
            "SELECT plan_id, step_id FROM navigation_task_steps WHERE plan_id IS NOT NULL"
        ).fetchall()
    assert plans == [(first.plan_id, "active")]
    assert steps == [(first.plan_id, "prepare"), (first.plan_id, "sync")]


def test_record_attempt_is_audit_only_and_preserves_full_failure(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    candidate = {
        "decisions": {"model_owned": {"nested": ["full", "candidate"]}},
        "steps": [{"action": "invented_action", "arguments": {"raw": True}}],
    }
    attempt = PlanSubmissionAttempt(
        attempt_id="attempt-invalid-1",
        task_id=task.task_id,
        phase="extract_sync",
        planning_context_revision="context-revision-7",
        candidate=candidate,
        validation={
            "ok": False,
            "errors": [
                {
                    "path": "plan.steps.0.action",
                    "code": "unknown_action",
                    "message": "The action is not in the capability catalog.",
                    "allowed_values": ["prepare_raw_data"],
                }
            ],
            "warnings": [],
        },
        created_at="2026-07-10T12:00:00.000+00:00",
    )

    assert repo.record_attempt(attempt) == attempt

    with sqlite3.connect(repo.db_path) as connection:
        row = connection.execute(
            "SELECT candidate_json, validation_json "
            "FROM navigation_plan_submission_attempts WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
    assert json.loads(row[0]) == candidate
    assert json.loads(row[1]) == attempt.validation.model_dump(mode="json")
    assert repo.get(attempt.attempt_id) is None
    assert repo.get_active(task.task_id, "extract_sync") is None


def test_activation_creates_immutable_revisions_and_supersedes_previous(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    first_plan = valid_extract_plan()
    first = repo.activate(task, "extract_sync", 1, first_plan)
    first_json = first.plan.model_dump_json()
    second = repo.activate(task, "extract_sync", 2, valid_extract_plan())

    assert (first.plan_revision, second.plan_revision) == (1, 2)
    assert repo.get(first.plan_id).status == "superseded"
    assert repo.get(first.plan_id).plan.model_dump_json() == first_json
    assert repo.get(second.plan_id).status == "active"
    assert repo.get_active(task.task_id, "extract_sync").plan_id == second.plan_id


@pytest.mark.parametrize("in_flight_status", ["running", "waiting_user"])
def test_activation_rejects_supersede_while_active_step_is_in_flight(
    tmp_path: Path,
    in_flight_status: str,
):
    repo, task = stores_with_task(tmp_path)
    first = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    transition = (
        repo.claim_step(first.plan_id, "prepare", "prepare_raw_data")
        if in_flight_status == "running"
        else repo.mark_waiting_user(first.plan_id, "prepare", "prepare_raw_data")
    )
    assert transition is True

    with pytest.raises(ActivePlanExecutionConflict):
        repo.activate(task, "extract_sync", 2, valid_extract_plan())

    assert repo.get_active(task.task_id, "extract_sync").plan_id == first.plan_id
    assert repo.get_current_step(first.plan_id)["step"]["status"] == in_flight_status


def test_activation_rejects_supersede_while_result_outbox_exists(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    first = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    assert repo.claim_step(first.plan_id, "prepare", "prepare_raw_data")
    repo.stage_step_result(
        first.plan_id,
        "prepare",
        target_status="completed",
        full_result={"ok": True, "tool_name": "prepare_raw_data", "message": "done"},
        result_summary={"ok": True, "tool_name": "prepare_raw_data", "message": "done"},
    )

    with pytest.raises(ActivePlanExecutionConflict):
        repo.activate(task, "extract_sync", 2, valid_extract_plan())

    assert repo.get_staged_step_result(first.plan_id, "prepare") is not None


def test_invalidate_removes_plan_from_active_lookup_without_mutating_plan(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    canonical = record.plan.model_dump_json()

    invalidated = repo.invalidate(record.plan_id, "artifact drift")

    assert invalidated.status == "invalidated"
    assert invalidated.plan.model_dump_json() == canonical
    assert repo.get_active(task.task_id, "extract_sync") is None
    with sqlite3.connect(repo.db_path) as connection:
        reason = connection.execute(
            "SELECT invalidation_reason FROM navigation_plans WHERE plan_id = ?",
            (record.plan_id,),
        ).fetchone()[0]
    assert reason == "artifact drift"


@pytest.mark.parametrize("mutation", ["invalidate", "mark_needs_replan"])
def test_plan_mutations_reject_claimed_execution_in_same_transaction(
    tmp_path: Path, mutation: str
):
    repo, task = stores_with_task(tmp_path)
    record = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    assert repo.claim_step(record.plan_id, "prepare", "prepare_raw_data")

    with pytest.raises(ActivePlanExecutionConflict):
        getattr(repo, mutation)(record.plan_id, "artifact drift")

    assert repo.get(record.plan_id).status == "active"
    assert repo.get_current_step(record.plan_id)["step"]["status"] == "running"


def test_invalidation_wins_before_claim_and_prevents_execution_claim(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = repo.activate(task, "extract_sync", 1, valid_extract_plan())

    assert repo.invalidate(record.plan_id, "artifact drift").status == "invalidated"
    assert repo.claim_step(record.plan_id, "prepare", "prepare_raw_data") is False


def test_compact_reads_expose_execution_status_and_no_legacy_payloads(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = repo.activate(task, "extract_sync", 1, valid_extract_plan())

    overview = repo.get_execution_overview(record.plan_id)
    current = repo.get_current_step(record.plan_id)

    assert overview.model_dump(mode="json") == {
        "plan_id": record.plan_id,
        "plan_revision": 1,
        "status": "active",
        "total_steps": 2,
        "completed_steps": 0,
        "current_step_id": "prepare",
        "steps": [
            {"step_id": "prepare", "action": "prepare_raw_data", "status": "pending"},
            {
                "step_id": "sync",
                "action": "extract_and_sync_navigation_data",
                "status": "pending",
            },
        ],
    }
    assert current == {
        "plan_id": record.plan_id,
        "plan_revision": 1,
        "step": {
            "id": current["step"]["id"],
            "plan_id": record.plan_id,
            "plan_revision": 1,
            "sequence": 0,
            "step_id": "prepare",
            "action": "prepare_raw_data",
            "status": "pending",
            "result_summary": None,
            "result_ref": None,
            "retry_count": 0,
        },
        "decision_refs": [],
    }
    assert len(json.dumps(overview.model_dump(mode="json"), separators=(",", ":"))) <= 4000
    assert len(json.dumps(current, separators=(",", ":"))) <= 4000

    with sqlite3.connect(repo.db_path) as connection:
        rows = connection.execute(
            "SELECT arguments_json, result_json, produced_paths_json "
            "FROM navigation_task_steps WHERE plan_id = ? ORDER BY sequence",
            (record.plan_id,),
        ).fetchall()
    assert rows == [(None, None, None), (None, None, None)]


def test_current_step_returns_its_stored_decision_refs(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = 'completed' "
            "WHERE plan_id = ? AND step_id = 'prepare'",
            (record.plan_id,),
        )

    current = repo.get_current_step(record.plan_id)

    assert current["step"]["step_id"] == "sync"
    assert current["decision_refs"] == ["sensor_bindings"]


def test_ledger_reads_reject_non_execution_status(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = repo.activate(task, "extract_sync", 1, valid_extract_plan())
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = 'needs_reconcile' "
            "WHERE plan_id = ? AND step_id = 'prepare'",
            (record.plan_id,),
        )

    with pytest.raises(ValidationError):
        repo.get_current_step(record.plan_id)


def test_compact_reads_enforce_4000_character_limit(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    oversized_ref = "decision_" + "x" * 4100
    record = repo.activate(
        task,
        "extract_sync",
        1,
        valid_extract_plan(decision_ref=oversized_ref),
    )
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = 'completed' "
            "WHERE plan_id = ? AND step_id = 'prepare'",
            (record.plan_id,),
        )

    with pytest.raises(ValueError, match="current step exceeds 4000 characters"):
        repo.get_current_step(record.plan_id)


def test_activation_requires_a_persisted_task_and_leaves_no_partial_rows(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"
    SqliteNavigationTaskStore(db_path)
    repo = SqliteNavigationPlanRepository(db_path)
    missing_task = NavigationTask(task_id="nav-missing", date="20270623")

    with pytest.raises(KeyError, match="nav-missing"):
        repo.activate(missing_task, "extract_sync", 1, valid_extract_plan())

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM navigation_plans").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM navigation_task_steps WHERE plan_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )

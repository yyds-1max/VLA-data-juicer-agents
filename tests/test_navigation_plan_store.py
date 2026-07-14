import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from vla_data_juicer_agents.navigation.plan_models import (
    ExtractSyncPlanInput,
    FinishProcessingPlanInput,
    PlanSubmissionAttempt,
)
from vla_data_juicer_agents.navigation.plan_store import (
    ActivePlanExecutionConflict,
    SqliteNavigationPlanRepository,
    StepClaimOutcome,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.task_state import NavigationTask
from vla_data_juicer_agents.navigation.task_store import (
    NavigationStateResetRequired,
    SqliteNavigationTaskStore,
)


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


def valid_finish_plan() -> FinishProcessingPlanInput:
    return FinishProcessingPlanInput.model_validate(
        {
            "decisions": {
                "localization": {
                    "source": "odom",
                    "conversion": "odom_to_ins",
                    "reason": "Observed odometry.",
                    "evidence_refs": ["evidence:localization"],
                },
                "gridmap": {
                    "source": "existing_gridmap",
                    "reason": "Observed gridmap.",
                    "evidence_refs": ["evidence:gridmap"],
                },
                "calibration": {
                    "mode": "hardcoded_with_user_confirmation",
                    "selected_sensor_source": "fisheye_front",
                    "requires_user_confirmation": True,
                    "reason": "Observed calibration source.",
                    "evidence_refs": ["evidence:calibration"],
                },
            },
            "steps": [
                {
                    "step_id": "confirm",
                    "action": "confirm_navigation_calibration_params",
                    "variant": "default",
                    "arguments": {},
                    "depends_on": [],
                    "failure_policy": "stop",
                    "decision_refs": ["calibration"],
                }
            ],
        }
    )


def _activate_owned(repo, task, *args, **kwargs):
    kwargs.setdefault(
        "expected_web_session_id", task.created_by_web_session_id
    )
    kwargs.setdefault(
        "expected_agentscope_session_id", task.agentscope_session_id
    )
    return repo.activate(task, *args, **kwargs)


def stores_with_task(
    tmp_path: Path,
    *,
    web_session_id: str = "web-test",
    agentscope_session_id: str = "as-test",
):
    db_path = tmp_path / "navigation_tasks.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_task_attempt(
        request="Process navigation data",
        target="20270623",
        date="20270623",
        segments=["20260623_101010"],
        scene_mode=None,
        dry_run=False,
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    ).task
    return SqliteNavigationPlanRepository(db_path), task


def attempt_with_extract_plan(
    repo: SqliteNavigationPlanRepository,
    *,
    owner: str,
    date: str = "20270623",
    segments: list[str] | None = None,
    dry_run: bool = False,
):
    agentscope_session_id = f"{owner}-agent"
    task = SqliteNavigationTaskStore(repo.db_path).create_task_attempt(
        request="process navigation data",
        target=date,
        date=date,
        segments=segments,
        scene_mode=None,
        dry_run=dry_run,
        web_session_id=owner,
        agentscope_session_id=agentscope_session_id,
    ).task
    plan = _activate_owned(repo,
        task,
        "extract_sync",
        1,
        valid_extract_plan(),
        expected_web_session_id=owner,
        expected_agentscope_session_id=agentscope_session_id,
    )
    return task, plan, agentscope_session_id


def claim_prepare(repo, plan, *, owner: str, agentscope_session_id: str):
    return repo.claim_step(
        plan.plan_id,
        "prepare",
        "prepare_raw_data",
        expected_web_session_id=owner,
        expected_agentscope_session_id=agentscope_session_id,
    )


def finalize_claimed_step(
    repo: SqliteNavigationPlanRepository,
    plan,
    *,
    step_id: str,
    action: str,
    owner: str,
    agentscope_session_id: str,
    target_status: str,
):
    assert repo.claim_step(
        plan.plan_id,
        step_id,
        action,
        expected_web_session_id=owner,
        expected_agentscope_session_id=agentscope_session_id,
    ) is StepClaimOutcome.CLAIMED
    staged = repo.stage_step_result(
        plan.plan_id,
        step_id,
        expected_action=action,
        target_status=target_status,
        full_result={"ok": target_status == "completed", "message": target_status},
        result_summary={"ok": target_status == "completed", "message": target_status},
        expected_web_session_id=owner,
        expected_agentscope_session_id=agentscope_session_id,
    )
    assert staged.result_ref is not None
    assert repo.attach_staged_result_evidence(
        plan.plan_id,
        step_id,
        staged.result_ref,
        expected_action=action,
        expected_web_session_id=owner,
        expected_agentscope_session_id=agentscope_session_id,
    )
    assert repo.finalize_staged_step(
        plan.plan_id,
        step_id,
        expected_action=action,
        expected_web_session_id=owner,
        expected_agentscope_session_id=agentscope_session_id,
    )


@pytest.mark.parametrize(
    ("left_date", "left_segments", "right_date", "right_segments", "expected"),
    [
        ("20270623", None, "20270623", ["segment-a"], StepClaimOutcome.NAVIGATION_DATA_BUSY),
        ("20270623", ["segment-a"], "20270623", None, StepClaimOutcome.NAVIGATION_DATA_BUSY),
        (
            "20270623",
            ["segment-a", "segment-b"],
            "20270623",
            ["segment-b", "segment-c"],
            StepClaimOutcome.NAVIGATION_DATA_BUSY,
        ),
        ("20270623", ["segment-a"], "20270623", ["segment-b"], StepClaimOutcome.CLAIMED),
        ("20270623", None, "20270624", None, StepClaimOutcome.CLAIMED),
    ],
)
def test_claim_step_locks_only_overlapping_navigation_targets(
    tmp_path: Path,
    left_date: str,
    left_segments: list[str] | None,
    right_date: str,
    right_segments: list[str] | None,
    expected: StepClaimOutcome,
):
    repo = SqliteNavigationPlanRepository(tmp_path / "navigation.sqlite")
    _left_task, left_plan, left_agent = attempt_with_extract_plan(
        repo, owner="web-left", date=left_date, segments=left_segments
    )
    _right_task, right_plan, right_agent = attempt_with_extract_plan(
        repo, owner="web-right", date=right_date, segments=right_segments
    )

    assert claim_prepare(
        repo, left_plan, owner="web-left", agentscope_session_id=left_agent
    ) is StepClaimOutcome.CLAIMED
    assert claim_prepare(
        repo, right_plan, owner="web-right", agentscope_session_id=right_agent
    ) is expected


def test_dry_run_claims_neither_acquire_nor_conflict_with_target_lock(tmp_path: Path):
    repo = SqliteNavigationPlanRepository(tmp_path / "navigation.sqlite")
    _dry_task, dry_plan, dry_agent = attempt_with_extract_plan(
        repo,
        owner="web-dry",
        segments=["segment-a"],
        dry_run=True,
    )
    _real_task, real_plan, real_agent = attempt_with_extract_plan(
        repo,
        owner="web-real",
        segments=["segment-a"],
    )

    assert claim_prepare(
        repo, dry_plan, owner="web-dry", agentscope_session_id=dry_agent
    ) is StepClaimOutcome.CLAIMED
    assert claim_prepare(
        repo, real_plan, owner="web-real", agentscope_session_id=real_agent
    ) is StepClaimOutcome.CLAIMED

    second_repo = SqliteNavigationPlanRepository(tmp_path / "second.sqlite")
    _real_task, first_real, first_real_agent = attempt_with_extract_plan(
        second_repo,
        owner="web-real-first",
        segments=["segment-a"],
    )
    _dry_task, second_dry, second_dry_agent = attempt_with_extract_plan(
        second_repo,
        owner="web-dry-second",
        segments=["segment-a"],
        dry_run=True,
    )
    assert claim_prepare(
        second_repo,
        first_real,
        owner="web-real-first",
        agentscope_session_id=first_real_agent,
    ) is StepClaimOutcome.CLAIMED
    assert claim_prepare(
        second_repo,
        second_dry,
        owner="web-dry-second",
        agentscope_session_id=second_dry_agent,
    ) is StepClaimOutcome.CLAIMED


def test_running_non_locking_validation_step_does_not_block_writer(tmp_path: Path):
    repo = SqliteNavigationPlanRepository(tmp_path / "navigation.sqlite")
    task_store = SqliteNavigationTaskStore(repo.db_path)
    task = task_store.create_task_attempt(
        request="validate navigation data",
        target="20270623",
        date="20270623",
        segments=["segment-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id="web-validator",
        agentscope_session_id="web-validator-agent",
    ).task
    payload = valid_finish_plan().model_dump(mode="json")
    payload["steps"] = [
        {
            "step_id": "validate",
            "action": "validate_navigation_outputs",
            "variant": "expect_gridmap",
            "arguments": {},
            "depends_on": [],
            "failure_policy": "stop",
            "decision_refs": ["gridmap"],
        }
    ]
    validation_plan = _activate_owned(repo,
        task,
        "finish_processing",
        1,
        FinishProcessingPlanInput.model_validate(payload),
        expected_web_session_id="web-validator",
        expected_agentscope_session_id="web-validator-agent",
    )
    assert repo.claim_step(
        validation_plan.plan_id,
        "validate",
        "validate_navigation_outputs",
        expected_web_session_id="web-validator",
        expected_agentscope_session_id="web-validator-agent",
    ) is StepClaimOutcome.CLAIMED

    _candidate_task, candidate_plan, candidate_agent = attempt_with_extract_plan(
        repo, owner="web-candidate", segments=["segment-a"]
    )
    assert claim_prepare(
        repo,
        candidate_plan,
        owner="web-candidate",
        agentscope_session_id=candidate_agent,
    ) is StepClaimOutcome.CLAIMED


@pytest.mark.parametrize(
    ("task_status", "step_status"),
    [
        ("active", "pending"),
        ("waiting_user", "waiting_user"),
        ("completed", "completed"),
        ("failed", "failed"),
    ],
)
def test_non_running_attempts_never_hold_target_lock(
    tmp_path: Path,
    task_status: str,
    step_status: str,
):
    repo = SqliteNavigationPlanRepository(tmp_path / "navigation.sqlite")
    task, plan, agent = attempt_with_extract_plan(
        repo, owner="web-inactive", segments=["segment-a"]
    )
    SqliteNavigationTaskStore(repo.db_path).update_task_for_session(
        task.task_id,
        web_session_id="web-inactive",
        agentscope_session_id=agent,
        status=task_status,
    )
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = ? "
            "WHERE plan_id = ? AND step_id = 'prepare'",
            (step_status, plan.plan_id),
        )

    _candidate, candidate_plan, candidate_agent = attempt_with_extract_plan(
        repo, owner="web-candidate", segments=["segment-a"]
    )
    assert claim_prepare(
        repo,
        candidate_plan,
        owner="web-candidate",
        agentscope_session_id=candidate_agent,
    ) is StepClaimOutcome.CLAIMED


def test_two_simultaneous_overlapping_claims_have_one_busy_loser(tmp_path: Path):
    db_path = tmp_path / "navigation.sqlite"
    repo = SqliteNavigationPlanRepository(db_path)
    claims = []
    for owner in ("web-a", "web-b"):
        _task, plan, agent = attempt_with_extract_plan(
            repo, owner=owner, segments=["segment-a"]
        )
        claims.append((owner, plan.plan_id, agent))
    barrier = Barrier(2)

    def claim(claim_data):
        owner, plan_id, agent = claim_data
        barrier.wait()
        return SqliteNavigationPlanRepository(db_path).claim_step(
            plan_id,
            "prepare",
            "prepare_raw_data",
            expected_web_session_id=owner,
            expected_agentscope_session_id=agent,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, claims))

    assert sorted(outcome.value for outcome in outcomes) == [
        StepClaimOutcome.CLAIMED.value,
        StepClaimOutcome.NAVIGATION_DATA_BUSY.value,
    ]
    with sqlite3.connect(db_path) as connection:
        running = connection.execute(
            "SELECT COUNT(*) FROM navigation_task_steps WHERE status = 'running'"
        ).fetchone()[0]
    assert running == 1


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_terminal_step_releases_target_lock(tmp_path: Path, terminal_status: str):
    repo = SqliteNavigationPlanRepository(tmp_path / "navigation.sqlite")
    _task, plan, agent = attempt_with_extract_plan(
        repo, owner="web-first", segments=["segment-a"]
    )
    finalize_claimed_step(
        repo,
        plan,
        step_id="prepare",
        action="prepare_raw_data",
        owner="web-first",
        agentscope_session_id=agent,
        target_status=terminal_status,
    )

    _candidate, candidate_plan, candidate_agent = attempt_with_extract_plan(
        repo, owner="web-second", segments=["segment-a"]
    )
    assert claim_prepare(
        repo,
        candidate_plan,
        owner="web-second",
        agentscope_session_id=candidate_agent,
    ) is StepClaimOutcome.CLAIMED


def test_completed_extract_plan_leaves_attempt_active(tmp_path: Path):
    repo = SqliteNavigationPlanRepository(tmp_path / "navigation.sqlite")
    task, plan, agent = attempt_with_extract_plan(
        repo, owner="web-extract", segments=["segment-a"]
    )

    finalize_claimed_step(
        repo,
        plan,
        step_id="prepare",
        action="prepare_raw_data",
        owner="web-extract",
        agentscope_session_id=agent,
        target_status="completed",
    )
    finalize_claimed_step(
        repo,
        plan,
        step_id="sync",
        action="extract_and_sync_navigation_data",
        owner="web-extract",
        agentscope_session_id=agent,
        target_status="completed",
    )

    assert repo.get(plan.plan_id).status == "completed"
    stored = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert stored is not None
    assert stored.status.value == "active"
    assert stored.accepted_plan_phase == "extract_sync"


def test_finish_plan_completes_attempt_only_after_terminal_validation(tmp_path: Path):
    repo = SqliteNavigationPlanRepository(tmp_path / "navigation.sqlite")
    owner = "web-finish"
    agent = "web-finish-agent"
    task = SqliteNavigationTaskStore(repo.db_path).create_task_attempt(
        request="finish navigation data",
        target="20270623",
        date="20270623",
        segments=["segment-a"],
        scene_mode="out",
        dry_run=False,
        web_session_id=owner,
        agentscope_session_id=agent,
    ).task
    payload = valid_finish_plan().model_dump(mode="json")
    payload["steps"] = [
        payload["steps"][0],
        {
            "step_id": "validate",
            "action": "validate_navigation_outputs",
            "variant": "expect_gridmap",
            "arguments": {},
            "depends_on": ["confirm"],
            "failure_policy": "stop",
            "decision_refs": ["gridmap"],
        },
    ]
    plan = _activate_owned(repo,
        task,
        "finish_processing",
        1,
        FinishProcessingPlanInput.model_validate(payload),
        expected_web_session_id=owner,
        expected_agentscope_session_id=agent,
    )

    finalize_claimed_step(
        repo,
        plan,
        step_id="confirm",
        action="confirm_navigation_calibration_params",
        owner=owner,
        agentscope_session_id=agent,
        target_status="completed",
    )
    assert SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id).status.value == "active"
    assert repo.get(plan.plan_id).status == "active"

    finalize_claimed_step(
        repo,
        plan,
        step_id="validate",
        action="validate_navigation_outputs",
        owner=owner,
        agentscope_session_id=agent,
        target_status="completed",
    )
    assert repo.get(plan.plan_id).status == "completed"
    assert SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id).status.value == "completed"


def test_stale_running_writer_blocks_until_controlled_step_recovery(tmp_path: Path):
    repo = SqliteNavigationPlanRepository(tmp_path / "navigation.sqlite")
    _task, plan, agent = attempt_with_extract_plan(
        repo, owner="web-stale", segments=["segment-a"]
    )
    _candidate, candidate_plan, candidate_agent = attempt_with_extract_plan(
        repo, owner="web-candidate", segments=["segment-a"]
    )
    assert claim_prepare(
        repo, plan, owner="web-stale", agentscope_session_id=agent
    ) is StepClaimOutcome.CLAIMED
    assert claim_prepare(
        repo,
        candidate_plan,
        owner="web-candidate",
        agentscope_session_id=candidate_agent,
    ) is StepClaimOutcome.NAVIGATION_DATA_BUSY

    assert repo.recover_running_step_without_result(
        plan.plan_id,
        "prepare",
        "operator-controlled recovery",
        expected_action="prepare_raw_data",
        expected_web_session_id="web-stale",
        expected_agentscope_session_id=agent,
    )
    assert claim_prepare(
        repo,
        candidate_plan,
        owner="web-candidate",
        agentscope_session_id=candidate_agent,
    ) is StepClaimOutcome.CLAIMED


def test_repository_rejects_legacy_handoff_schema_without_mutation(tmp_path: Path):
    db_path = tmp_path / "navigation_tasks.sqlite"
    SqliteNavigationTaskStore(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE navigation_human_decision_handoffs")
        connection.execute(
            """CREATE TABLE navigation_human_decision_handoffs (
                plan_id TEXT NOT NULL, step_id TEXT NOT NULL, task_id TEXT NOT NULL,
                decision_key TEXT NOT NULL, decision_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status = 'pending'),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, step_id)
            )"""
        )
        before_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    before_bytes = db_path.read_bytes()

    with pytest.raises(NavigationStateResetRequired):
        SqliteNavigationPlanRepository(db_path)

    assert db_path.read_bytes() == before_bytes
    with sqlite3.connect(db_path) as connection:
        after_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert after_schema == before_schema


def test_repository_migrates_delivery_lease_and_fails_closed_after_expiry(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    plan = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
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
        plan.plan_id, "prepare", "decision", owner="new-owner",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    ) == ("busy", None)
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_human_decision_handoffs SET expires_at = ? WHERE plan_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), plan.plan_id),
        )
    status, token = repo.claim_human_decision_delivery(
        plan.plan_id, "prepare", "decision", owner="new-owner",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    assert status == "recovery_required" and token is None
    handoff = repo.get_human_decision_handoff(plan.plan_id, "prepare")
    assert handoff is not None
    assert handoff.status == "recovery_required"
    assert handoff.delivery_status == "recovery_required"
    assert handoff.delivery_token == "old-token"


def test_controlled_handoff_recovery_preserves_audit_and_unblocks_replacement(tmp_path: Path):
    repo, task = stores_with_task(tmp_path, web_session_id="web-owner")
    plan = _activate_owned(repo,
        task, "extract_sync", 1, valid_extract_plan(),
        expected_web_session_id="web-owner", expected_agentscope_session_id=task.agentscope_session_id,
    )
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
        plan.plan_id,
        "prepare",
        reason_code="missing_agentscope_session",
        expected_web_session_id="web-owner",
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    assert repo.mark_human_decision_recovery_required(
        plan.plan_id,
        "prepare",
        reason_code="missing_agentscope_session",
        expected_web_session_id="web-owner",
        expected_agentscope_session_id=task.agentscope_session_id,
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
    replacement = _activate_owned(repo,
        task, "extract_sync", 2, valid_extract_plan(),
        expected_web_session_id="web-owner", expected_agentscope_session_id=task.agentscope_session_id,
    )
    assert replacement.status == "active"
    assert not repo.claim_step(
        plan.plan_id, "prepare", "prepare_raw_data",
        expected_web_session_id="web-owner", expected_agentscope_session_id=task.agentscope_session_id,
    )


def test_staged_handoff_recovery_finishes_after_a_new_current_attempt(tmp_path: Path):
    repo, task = stores_with_task(tmp_path, web_session_id="web-owner")
    plan = _activate_owned(
        repo,
        task,
        "extract_sync",
        1,
        valid_extract_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            """INSERT INTO navigation_human_decision_handoffs (
                   plan_id, step_id, task_id, decision_key, decision_json,
                   status, delivery_status, created_at, updated_at
               ) VALUES (?, 'prepare', ?, 'decision', '{}',
                         'recovery_required', 'recovery_required', ?, ?)""",
            (plan.plan_id, task.task_id, timestamp, timestamp),
        )
    task_store = SqliteNavigationTaskStore(repo.db_path)
    newer = task_store.create_task_attempt(
        request="process B",
        target="20260711",
        date="20260711",
        segments=["segment-b"],
        scene_mode=None,
        dry_run=True,
        web_session_id="web-owner",
        agentscope_session_id=task.agentscope_session_id,
    )
    newer_before = task_store.get_task(newer.task.task_id)

    recovered = repo.quarantine_human_decision_handoff(
        plan.plan_id,
        "prepare",
        expected_web_session_id="web-owner",
        reason="operator requested recovery",
    )

    assert recovered["recovered"] is True
    assert repo.get_human_decision_handoff(plan.plan_id, "prepare").status == "quarantined"
    assert repo.get(plan.plan_id).status == "invalidated"
    assert repo.get_current_step(plan.plan_id)["step"]["status"] == "needs_replan"
    assert task_store.get_task(newer.task.task_id) == newer_before


def test_non_current_attempt_cannot_initiate_a_human_handoff(tmp_path: Path):
    repo, task = stores_with_task(tmp_path, web_session_id="web-owner")
    plan = _activate_owned(
        repo,
        task,
        "finish_processing",
        1,
        valid_finish_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    task_store = SqliteNavigationTaskStore(repo.db_path)
    task_store.create_task_attempt(
        request="process B",
        target="20260711",
        date="20260711",
        segments=["segment-b"],
        scene_mode=None,
        dry_run=True,
        web_session_id="web-owner",
        agentscope_session_id=task.agentscope_session_id,
    )

    with pytest.raises(PermissionError, match="session mismatch"):
        repo.stage_human_decision_handoff(
            plan.plan_id,
            "confirm",
            decision_key="decision",
            decision={"parameters": {}},
            target_status="completed",
            full_result={"ok": True},
            result_summary={"ok": True},
            expected_web_session_id="web-owner",
            expected_agentscope_session_id=task.agentscope_session_id,
        )

    assert repo.get_human_decision_handoff(plan.plan_id, "confirm") is None


@pytest.mark.parametrize("delivery_status", ["pending", "delivering", "delivered", "quarantined"])
def test_controlled_handoff_recovery_rejects_non_recovery_states(
    tmp_path: Path, delivery_status: str
):
    repo, task = stores_with_task(tmp_path, web_session_id="web-owner")
    plan = _activate_owned(repo,
        task, "extract_sync", 1, valid_extract_plan(),
        expected_web_session_id="web-owner", expected_agentscope_session_id=task.agentscope_session_id,
    )
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


def test_repository_rejects_nullable_legacy_result_ref_schema_without_mutation(
    tmp_path: Path,
):
    db_path = tmp_path / "navigation.sqlite"
    SqliteNavigationPlanRepository(db_path)
    with sqlite3.connect(db_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'navigation_step_result_outbox'"
        ).fetchone()[0]
        connection.execute("DROP TABLE navigation_step_result_outbox")
        connection.execute(table_sql.replace("result_ref TEXT NOT NULL", "result_ref TEXT"))
        before_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    before_bytes = db_path.read_bytes()

    with pytest.raises(NavigationStateResetRequired):
        SqliteNavigationPlanRepository(db_path)

    assert db_path.read_bytes() == before_bytes
    with sqlite3.connect(db_path) as connection:
        after_schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    assert after_schema == before_schema


def test_step_ledger_transitions_increment_task_state_revision(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    plan = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    after_activation = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)

    assert repo.claim_step(
        plan.plan_id, "prepare", "prepare_raw_data",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    after_claim = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert after_claim.state_revision == after_activation.state_revision + 1

    repo.stage_step_result(
        plan.plan_id,
        "prepare",
        expected_action="prepare_raw_data",
        target_status="completed",
        full_result={"message": "done", "ok": True, "tool_name": "prepare_raw_data"},
        result_summary={"message": "done", "ok": True, "tool_name": "prepare_raw_data"},
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    after_stage = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert after_stage.state_revision == after_claim.state_revision + 1
    assert repo.finalize_staged_step(
        plan.plan_id, "prepare",
        expected_action="prepare_raw_data",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    after_finalize = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert after_finalize.state_revision == after_stage.state_revision + 2

    assert repo.mark_waiting_user(
        plan.plan_id, "sync", "extract_and_sync_navigation_data",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    after_waiting = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert after_waiting.state_revision == after_finalize.state_revision + 1


def test_claim_terminalization_rejects_the_wrong_action(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    plan = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    assert repo.claim_step(
        plan.plan_id,
        "prepare",
        "prepare_raw_data",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    ) is StepClaimOutcome.CLAIMED

    with pytest.raises(PermissionError, match="session mismatch"):
        repo.stage_step_result(
            plan.plan_id,
            "prepare",
            expected_action="extract_and_sync_navigation_data",
            target_status="completed",
            full_result={"ok": True},
            result_summary={"ok": True},
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )

    assert repo.get_current_step(plan.plan_id)["step"]["status"] == "running"
    assert repo.get_staged_step_result(plan.plan_id, "prepare") is None


def test_plan_repository_can_initialize_before_task_store(tmp_path: Path):
    db_path = tmp_path / "plan-first.sqlite"

    SqliteNavigationPlanRepository(db_path)
    SqliteNavigationTaskStore(db_path)
    SqliteNavigationObservationStore(db_path)
    SqliteNavigationPlanRepository(db_path)

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "navigation_tasks",
        "navigation_task_steps",
        "navigation_plans",
        "navigation_observation_revisions",
    } <= tables


def test_owned_task_plan_writes_reject_omitted_session(tmp_path: Path):
    db_path = tmp_path / "owned.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    owned = task_store.create_task_attempt(
        request="Process navigation data", target="20260710",
        date="20260710", segments=["clip"], scene_mode=None, dry_run=False,
        web_session_id="web-owner", agentscope_session_id="as-owner",
    ).task
    repository = SqliteNavigationPlanRepository(db_path)
    plan = repository.activate(
        owned, "extract_sync", 1, valid_extract_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="as-owner",
    )
    attempt = PlanSubmissionAttempt(
        attempt_id="attempt-owned",
        task_id=owned.task_id,
        phase="extract_sync",
        planning_context_revision="context",
        candidate={},
        validation={"ok": False, "errors": [], "warnings": []},
        created_at=owned.updated_at,
    )

    with pytest.raises(PermissionError, match="session mismatch"):
        repository.record_attempt(attempt)
    with pytest.raises(PermissionError, match="session mismatch"):
        repository.activate(owned, "extract_sync", 1, valid_extract_plan())
    with pytest.raises(PermissionError, match="session mismatch"):
        repository.claim_step(plan.plan_id, "prepare", "prepare_raw_data")
    with pytest.raises(PermissionError, match="session mismatch"):
        repository.stage_step_result(
            plan.plan_id,
            "prepare",
            expected_action="prepare_raw_data",
            target_status="completed",
            full_result={"ok": True},
            result_summary={"ok": True},
        )
    with pytest.raises(PermissionError, match="session mismatch"):
        repository.stage_human_decision_handoff(
            plan.plan_id,
            "prepare",
            decision_key="decision",
            decision={"action": "confirm"},
            target_status="completed",
            full_result={"ok": True},
            result_summary={"ok": True},
        )


def test_activate_plan_and_ledger_is_atomic(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    before = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)

    record = _activate_owned(repo, task, "extract_sync", 3, valid_extract_plan())

    assert repo.get_active(task.task_id, "extract_sync").plan_id == record.plan_id
    assert [
        step.step_id for step in repo.get_execution_overview(record.plan_id).steps
    ] == ["prepare", "sync"]
    after = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert before is not None and after is not None
    assert after.accepted_plan_phase == "extract_sync"
    assert after.state_revision > before.state_revision


def test_failed_activation_does_not_supersede_active_plan(
    tmp_path: Path,
    monkeypatch,
):
    repo, task = stores_with_task(tmp_path)
    first = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    monkeypatch.setattr(
        repo,
        "_insert_ledger_rows",
        lambda *args: (_ for _ in ()).throw(sqlite3.IntegrityError()),
    )

    with pytest.raises(sqlite3.IntegrityError):
        _activate_owned(repo, task, "extract_sync", 2, valid_extract_plan())

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


def test_failed_first_activation_does_not_record_selected_phase(tmp_path: Path, monkeypatch):
    repo, task = stores_with_task(tmp_path)
    monkeypatch.setattr(
        repo,
        "_insert_ledger_rows",
        lambda *args: (_ for _ in ()).throw(sqlite3.IntegrityError()),
    )

    with pytest.raises(sqlite3.IntegrityError):
        _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())

    stored = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert stored is not None
    assert stored.accepted_plan_phase is None
    assert repo.get_active(task.task_id, "extract_sync") is None


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

    before_attempt = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert repo.record_attempt(
        attempt,
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    ) == attempt
    after_attempt = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert after_attempt.state_revision == before_attempt.state_revision + 1

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
    first = _activate_owned(repo, task, "extract_sync", 1, first_plan)
    first_json = first.plan.model_dump_json()
    after_first = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    second = _activate_owned(repo, task, "extract_sync", 2, valid_extract_plan())
    after_second = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)

    assert (first.plan_revision, second.plan_revision) == (1, 2)
    assert repo.get(first.plan_id).status == "superseded"
    assert repo.get(first.plan_id).plan.model_dump_json() == first_json
    assert repo.get(second.plan_id).status == "active"
    assert repo.get_active(task.task_id, "extract_sync").plan_id == second.plan_id
    assert after_first.state_revision > task.state_revision
    assert after_second.state_revision > after_first.state_revision


def test_cross_phase_activation_replaces_the_whole_active_plan_atomically(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    extract = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())

    finish = _activate_owned(repo, task, "finish_processing", 2, valid_finish_plan())

    assert repo.get(extract.plan_id).status == "superseded"
    assert repo.get(finish.plan_id).status == "active"
    assert repo.get_active(task.task_id, "extract_sync") is None
    assert repo.get_active_for_task(task.task_id).plan_id == finish.plan_id
    stored = SqliteNavigationTaskStore(repo.db_path).get_task(task.task_id)
    assert stored is not None
    assert stored.accepted_plan_phase == "finish_processing"


@pytest.mark.parametrize(
    ("current_status", "expected_activity"),
    [
        ("pending", "execution"),
        ("failed", "failed_recovery"),
        ("needs_replan", "failed_recovery"),
    ],
)
def test_execution_snapshot_reads_task_plan_and_ledger_in_one_state(
    tmp_path: Path,
    current_status: str,
    expected_activity: str,
):
    repo, task = stores_with_task(
        tmp_path,
        web_session_id="web-owner",
        agentscope_session_id="web-owner__agent",
    )
    plan = _activate_owned(repo,
        task,
        "extract_sync",
        1,
        valid_extract_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="web-owner__agent",
    )
    if current_status != "pending":
        with sqlite3.connect(repo.db_path) as connection:
            connection.execute(
                """UPDATE navigation_task_steps SET status = ?
                   WHERE plan_id = ? AND sequence = 0""",
                (current_status, plan.plan_id),
            )

    snapshot = repo.read_execution_snapshot(
        web_session_id="web-owner",
        agentscope_session_id="web-owner__agent",
        task_id=task.task_id,
    )

    assert snapshot is not None
    assert snapshot.task.task_id == task.task_id
    assert snapshot.active_plan.plan_id == plan.plan_id
    assert snapshot.current["step"]["status"] == current_status
    assert snapshot.overview.current_step_id == "prepare"
    assert snapshot.activity == expected_activity


def test_execution_snapshot_prefers_handoff_recovery_over_failed_recovery(tmp_path: Path):
    repo, task = stores_with_task(
        tmp_path,
        web_session_id="web-owner",
        agentscope_session_id="web-owner__agent",
    )
    plan = _activate_owned(
        repo,
        task,
        "finish_processing",
        1,
        valid_finish_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="web-owner__agent",
    )
    assert repo.mark_waiting_user(
        plan.plan_id,
        "confirm",
        "confirm_navigation_calibration_params",
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="web-owner__agent",
    )
    assert repo.mark_human_decision_recovery_required(
        plan.plan_id,
        "confirm",
        reason_code="ambiguous_delivery_state",
        request_anchor={"plan_id": plan.plan_id, "step_id": "confirm"},
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="web-owner__agent",
    )

    snapshot = repo.read_execution_snapshot(
        web_session_id="web-owner",
        agentscope_session_id="web-owner__agent",
        task_id=task.task_id,
    )

    assert snapshot is not None
    assert snapshot.activity == "recovery_required"


@pytest.mark.parametrize("in_flight_status", ["running", "waiting_user"])
def test_activation_rejects_supersede_while_active_step_is_in_flight(
    tmp_path: Path,
    in_flight_status: str,
):
    repo, task = stores_with_task(tmp_path)
    first = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    transition = (
        repo.claim_step(
            first.plan_id, "prepare", "prepare_raw_data",
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )
        if in_flight_status == "running"
        else repo.mark_waiting_user(
            first.plan_id, "prepare", "prepare_raw_data",
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )
    )
    if in_flight_status == "running":
        assert transition is StepClaimOutcome.CLAIMED
    else:
        assert transition is True

    with pytest.raises(ActivePlanExecutionConflict):
        _activate_owned(repo, task, "extract_sync", 2, valid_extract_plan())

    assert repo.get_active(task.task_id, "extract_sync").plan_id == first.plan_id
    assert repo.get_current_step(first.plan_id)["step"]["status"] == in_flight_status


def test_activation_rejects_supersede_while_result_outbox_exists(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    first = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    assert repo.claim_step(
        first.plan_id, "prepare", "prepare_raw_data",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    repo.stage_step_result(
        first.plan_id,
        "prepare",
        expected_action="prepare_raw_data",
        target_status="completed",
        full_result={"ok": True, "tool_name": "prepare_raw_data", "message": "done"},
        result_summary={"ok": True, "tool_name": "prepare_raw_data", "message": "done"},
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )

    with pytest.raises(ActivePlanExecutionConflict):
        _activate_owned(repo, task, "extract_sync", 2, valid_extract_plan())

    assert repo.get_staged_step_result(first.plan_id, "prepare") is not None


def test_invalidate_removes_plan_from_active_lookup_without_mutating_plan(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    canonical = record.plan.model_dump_json()

    invalidated = repo.invalidate(
        record.plan_id, "artifact drift",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )

    assert invalidated.status == "invalidated"
    assert invalidated.plan.model_dump_json() == canonical
    assert repo.get_active(task.task_id, "extract_sync") is None
    with sqlite3.connect(repo.db_path) as connection:
        reason = connection.execute(
            "SELECT invalidation_reason FROM navigation_plans WHERE plan_id = ?",
            (record.plan_id,),
        ).fetchone()[0]
    assert reason == "artifact drift"


def test_invalidate_requires_exact_session_for_owned_task(
    tmp_path: Path,
):
    repo, task = stores_with_task(
        tmp_path,
        web_session_id="web-owner",
        agentscope_session_id="agentscope-owner",
    )
    owned_plan = _activate_owned(repo,
        task, "extract_sync", 1, valid_extract_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="agentscope-owner",
    )

    with pytest.raises(PermissionError, match="session mismatch"):
        repo.invalidate(owned_plan.plan_id, "artifact drift")
    with pytest.raises(PermissionError, match="session mismatch"):
        repo.invalidate(
            owned_plan.plan_id,
            "artifact drift",
            expected_web_session_id="web-other",
            expected_agentscope_session_id="agentscope-owner",
        )
    assert repo.get(owned_plan.plan_id).status == "active"

    invalidated = repo.invalidate(
        owned_plan.plan_id,
        "artifact drift",
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="agentscope-owner",
    )
    assert invalidated.status == "invalidated"


@pytest.mark.parametrize("mutation", ["invalidate", "mark_needs_replan"])
def test_plan_mutations_reject_claimed_execution_in_same_transaction(
    tmp_path: Path, mutation: str
):
    repo, task = stores_with_task(tmp_path)
    record = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    assert repo.claim_step(
        record.plan_id, "prepare", "prepare_raw_data",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )

    with pytest.raises(ActivePlanExecutionConflict):
        getattr(repo, mutation)(
            record.plan_id, "artifact drift",
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )

    assert repo.get(record.plan_id).status == "active"
    assert repo.get_current_step(record.plan_id)["step"]["status"] == "running"


def test_invalidation_wins_before_claim_and_prevents_execution_claim(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())

    assert repo.invalidate(
        record.plan_id, "artifact drift",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    ).status == "invalidated"
    assert (
        repo.claim_step(
            record.plan_id, "prepare", "prepare_raw_data",
            expected_web_session_id=task.created_by_web_session_id,
            expected_agentscope_session_id=task.agentscope_session_id,
        )
        is StepClaimOutcome.NOT_CLAIMABLE
    )


def test_compact_reads_expose_execution_status_and_no_legacy_payloads(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())

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
    record = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = 'completed' "
            "WHERE plan_id = ? AND step_id = 'prepare'",
            (record.plan_id,),
        )

    current = repo.get_current_step(record.plan_id)

    assert current["step"]["step_id"] == "sync"
    assert current["decision_refs"] == ["sensor_bindings"]


def test_repository_identifies_current_and_historical_step_result_refs(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    plan = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    assert repo.claim_step(
        plan.plan_id,
        "prepare",
        "prepare_raw_data",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    ) is StepClaimOutcome.CLAIMED
    staged = repo.stage_step_result(
        plan.plan_id,
        "prepare",
        expected_action="prepare_raw_data",
        target_status="failed",
        full_result={"ok": False, "message": "failed"},
        result_summary={"ok": False, "side_effect_state": "partial_or_unknown"},
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )

    assert repo.is_step_result_ref(task.task_id, staged.result_ref) is True
    assert repo.finalize_staged_step(
        plan.plan_id,
        "prepare",
        expected_action="prepare_raw_data",
        expected_web_session_id=task.created_by_web_session_id,
        expected_agentscope_session_id=task.agentscope_session_id,
    )
    replacement = _activate_owned(
        repo,
        task,
        "extract_sync",
        2,
        valid_extract_plan(),
    )
    assert replacement.plan_id != plan.plan_id
    assert repo.get(plan.plan_id).status == "superseded"
    assert repo.is_step_result_ref(task.task_id, staged.result_ref) is True
    assert repo.is_step_result_ref(task.task_id, "ordinary-observation-ref") is False


def test_ledger_reads_reject_non_execution_status(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    record = _activate_owned(repo, task, "extract_sync", 1, valid_extract_plan())
    legacy_status = "needs_" + "reconcile"
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute(
            "UPDATE navigation_task_steps SET status = ? "
            "WHERE plan_id = ? AND step_id = 'prepare'",
            (legacy_status, record.plan_id),
        )

    with pytest.raises(ValidationError):
        repo.get_current_step(record.plan_id)


def test_compact_reads_enforce_4000_character_limit(tmp_path: Path):
    repo, task = stores_with_task(tmp_path)
    oversized_ref = "decision_" + "x" * 4100
    record = _activate_owned(repo,
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
    missing_task = NavigationTask(
        task_id="nav-missing",
        date="20270623",
        created_by_web_session_id="web-missing",
        agentscope_session_id="as-missing",
    )

    with pytest.raises(KeyError, match="nav-missing"):
        _activate_owned(repo, missing_task, "extract_sync", 1, valid_extract_plan())

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM navigation_plans").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM navigation_task_steps WHERE plan_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vla_data_juicer_agents.web.contract_models import ContractConflictError
from vla_data_juicer_agents.web.migrations import UnsupportedSchemaVersionError
from vla_data_juicer_agents.web.session_store import (
    UnsupportedLegacySessionError,
    WebSessionStore,
)


def _v1_store(tmp_path: Path) -> tuple[WebSessionStore, str]:
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    session = store.create_session("contract v1", contract_version=1)
    return store, session.id


def _binding(store: WebSessionStore, session_id: str):
    return store.create_task_binding(
        session_id,
        task_id="task-internal-1",
        task_ref="task_public_a1b2",
        navigation_session_id="as-navigation-1",
        scope={"date": "20260720"},
    )


def test_explicit_migration_is_idempotent_and_new_sessions_are_v1_only(tmp_path: Path):
    db_path = tmp_path / "sessions.sqlite"
    store = WebSessionStore(db_path)
    session = store.create_session("contract v1")

    reopened = WebSessionStore(db_path)

    assert reopened.get_session_contract_version(session.id) == 1
    with pytest.raises(ValueError, match="contract_version must be 1"):
        reopened.create_session("legacy", contract_version=0)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (1,),
            (2,),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="contract_version is immutable"):
            connection.execute(
                "UPDATE sessions SET contract_version = 0 WHERE id = ?", (session.id,)
            )


def test_unknown_newer_schema_version_is_rejected_before_base_schema_changes(tmp_path: Path):
    db_path = tmp_path / "sessions.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (3, 'future', '2026-07-20T00:00:00Z')"
        )

    with pytest.raises(UnsupportedSchemaVersionError):
        WebSessionStore(db_path)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone() is None


def test_contract_v0_rows_require_explicit_development_reset(tmp_path: Path):
    db_path = tmp_path / "sessions.sqlite"
    WebSessionStore(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER trg_sessions_contract_version_immutable")
        connection.execute(
            """
            INSERT INTO sessions (
                id, title, status, created_at, updated_at, contract_version
            ) VALUES ('legacy', 'legacy', 'active', 'now', 'now', 0)
            """
        )

    with pytest.raises(UnsupportedLegacySessionError, match="contract v0"):
        WebSessionStore(db_path)


def test_contract_migration_rolls_back_legacy_column_when_sidecar_creation_fails(
    tmp_path: Path,
):
    db_path = tmp_path / "sessions.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # This deliberately malformed pre-existing object makes the first V1
        # index fail after the legacy sessions-table alteration has run.
        connection.execute(
            "CREATE TABLE conversation_task_bindings (task_id TEXT PRIMARY KEY)"
        )

    with pytest.raises(sqlite3.OperationalError):
        WebSessionStore(db_path)

    with sqlite3.connect(db_path) as connection:
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert "contract_version" not in session_columns
        assert connection.execute("SELECT * FROM schema_migrations").fetchall() == []


def test_task_binding_focus_navigation_session_and_outbox_are_atomic_and_idempotent(
    tmp_path: Path,
):
    store, session_id = _v1_store(tmp_path)

    first = _binding(store, session_id)
    duplicate = _binding(store, session_id)

    assert first.created is True
    assert duplicate.created is False
    assert first.binding.navigation_session_id == "as-navigation-1"
    assert first.binding.scope == {"date": "20260720"}
    assert first.focus.task_id == first.binding.task_id
    assert first.navigation_session.task_id == first.binding.task_id
    assert first.outbox.kind == "navigation_start"
    assert first.outbox.payload["task_ref"] == "task_public_a1b2"
    assert store.get_task_binding_by_ref(session_id, "task_public_a1b2") == first.binding
    assert store.get_focused_task_binding(session_id) == first.binding

    with pytest.raises(ContractConflictError) as raised:
        store.create_task_binding(
            session_id,
            task_id="task-internal-2",
            task_ref="task_public_c3d4",
            navigation_session_id="as-navigation-2",
        )
    assert raised.value.code == "open_task_slot_occupied"


def test_terminal_task_releases_slot_but_keeps_focus(tmp_path: Path):
    store, session_id = _v1_store(tmp_path)
    first = _binding(store, session_id)

    closed = store.mark_task_binding_terminal(
        first.binding.task_id,
        expected_revision=0,
        status="completed",
        latest_public_update="处理完成",
    )
    second = store.create_task_binding(
        session_id,
        task_id="task-internal-2",
        task_ref="task_public_c3d4",
        navigation_session_id="as-navigation-2",
    )

    assert closed.slot_state == "closed"
    assert closed.state_revision == 1
    assert second.focus.generation == 2
    assert store.get_focused_task_binding(session_id) == second.binding


def test_response_authority_handover_allows_exactly_one_final(tmp_path: Path):
    store, session_id = _v1_store(tmp_path)
    turn = store.begin_user_turn(session_id, "处理导航数据").turn
    initial = store.get_response_authority(turn.id)
    assert initial is not None
    assert (initial.producer, initial.generation, initial.lease_state) == ("router", 1, "open")

    navigation = store.handover_response_authority(
        turn.id,
        expected_producer="router",
        expected_generation=1,
    )
    committed = store.commit_authorized_final(
        turn.id,
        producer="navigation",
        response_generation=navigation.generation,
        text="导航数据处理完成。",
    )

    assert committed.message.content == "导航数据处理完成。"
    assert [event.type for event in committed.events] == ["final", "turn_state"]
    assert store.get_response_authority(turn.id).lease_state == "closed"  # type: ignore[union-attr]
    with pytest.raises(ContractConflictError, match="already closed"):
        store.commit_authorized_final(
            turn.id,
            producer="navigation",
            response_generation=navigation.generation,
            text="重复结果",
        )
    with pytest.raises(ContractConflictError):
        store.commit_authorized_final(
            turn.id,
            producer="router",
            response_generation=1,
            text="迟到的 Router 总结",
        )


def test_v1_projected_final_uses_conversation_mapping_and_authority(tmp_path: Path):
    store, session_id = _v1_store(tmp_path)
    creation = _binding(store, session_id)
    turn = store.begin_user_turn(session_id, "处理导航数据").turn
    authority = store.handover_response_authority(
        turn.id, expected_producer="router", expected_generation=1
    )
    store.bind_conversation_agent_session_to_turn("as-navigation-1", turn.id)
    store.register_pending_reply(
        turn_id=turn.id,
        agentscope_session_id="as-navigation-1",
        agent_id="navigation-data-agent",
        source="delegation",
    )
    store.append_projected_event_batch(
        web_session_id=session_id,
        agentscope_session_id="as-navigation-1",
        entry_id="1-0",
        raw_event_type="REPLY_START",
        reply_id="reply-nav-1",
        events=[],
    )

    records = store.append_projected_event_batch(
        web_session_id=session_id,
        agentscope_session_id="as-navigation-1",
        entry_id="2-0",
        raw_event_type="REPLY_END",
        reply_id="reply-nav-1",
        events=[{"type": "final", "payload": {"text": "任务处理完成。"}}],
    )

    final = next(record for record in records if record.type == "final")
    assert final.source is None
    assert final.run_id is None
    assert final.payload["text"] == "任务处理完成。"
    assert "reply_id" not in final.payload
    closed = store.get_response_authority(turn.id)
    assert closed is not None
    assert closed.generation == authority.generation
    assert closed.lease_state == "closed"
    assert store.get_conversation_agent_session_by_agentscope_session(
        creation.navigation_session.agentscope_session_id
    ).event_cursor == "2-0"  # type: ignore[union-attr]


def test_system_controller_can_take_over_before_navigation_final(tmp_path: Path):
    store, session_id = _v1_store(tmp_path)
    turn = store.begin_user_turn(session_id, "处理导航数据").turn
    navigation = store.handover_response_authority(
        turn.id, expected_producer="router", expected_generation=1
    )

    controller = store.takeover_response_authority(
        turn.id,
        expected_producer="navigation",
        expected_generation=navigation.generation,
    )

    assert controller.producer == "system_controller"
    assert controller.generation == 3


def test_interaction_consumption_is_revision_checked_and_idempotent(tmp_path: Path):
    store, session_id = _v1_store(tmp_path)
    _binding(store, session_id)
    interaction = store.create_interaction(
        session_id,
        task_ref="task_public_a1b2",
        kind="high_risk_confirmation",
        blocking=True,
        risk="high",
        title="确认写入标定参数",
        options=[{"id": "confirm", "label": "确认"}, {"id": "reject", "label": "取消"}],
        expected_task_revision=0,
        private_payload={"reply_id": "reply-private", "tool_call_id": "call-private"},
    )

    consumed = store.consume_interaction(
        interaction.interaction_id,
        interaction_revision=1,
        expected_task_revision=0,
        idempotency_key="click-1",
        option_id="confirm",
    )
    duplicate = store.consume_interaction(
        interaction.interaction_id,
        interaction_revision=1,
        expected_task_revision=0,
        idempotency_key="click-1",
        option_id="confirm",
    )
    submission = store.create_interaction_turn(
        interaction.interaction_id, content="已确认写入标定参数"
    )
    repeated_turn = store.create_interaction_turn(
        interaction.interaction_id, content="已确认写入标定参数"
    )

    assert consumed.created is True
    assert consumed.interaction.options[0]["option_id"] == "confirm"
    assert "id" not in consumed.interaction.options[0]
    assert consumed.interaction.private_payload == {
        "reply_id": "reply-private",
        "tool_call_id": "call-private",
    }
    assert consumed.interaction.response == {"option_ids": ["confirm"]}
    assert duplicate.created is False
    assert submission.turn.origin == "interaction"
    assert repeated_turn.created is False
    assert store.get_response_authority(submission.turn.id).producer == "navigation"  # type: ignore[union-attr]


def test_interaction_background_continuation_uses_specific_safe_reply(
    tmp_path: Path,
) -> None:
    store, session_id = _v1_store(tmp_path)
    binding = _binding(store, session_id).binding
    interaction = store.create_interaction(
        session_id,
        task_ref=binding.task_ref,
        kind="confirmation",
        blocking=True,
        risk="low",
        title="确认标定参数",
        options=[{"id": "confirm", "label": "确认并继续"}],
        expected_task_revision=0,
    )
    store.consume_interaction(
        interaction.interaction_id,
        interaction_revision=1,
        expected_task_revision=0,
        idempotency_key="confirm-calibration",
        option_id="confirm",
    )
    submission = store.create_interaction_turn(
        interaction.interaction_id,
        content="已选择：确认并继续",
    )
    store.bind_conversation_agent_session_to_turn(
        binding.navigation_session_id,
        submission.turn.id,
    )
    store.append_projected_event_batch(
        web_session_id=session_id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="interaction-background-start",
        events=[],
        raw_event_type="REPLY_START",
        reply_id="interaction-background-reply",
    )
    records = store.append_projected_event_batch(
        web_session_id=session_id,
        agentscope_session_id=binding.navigation_session_id,
        entry_id="interaction-background-end",
        events=[],
        raw_event_type="REPLY_END",
        reply_id="interaction-background-reply",
    )

    assert [record.type for record in records] == ["final", "turn_state"]
    detail = store.get_session(session_id)
    assert detail is not None
    answers = [
        message.content
        for message in detail.messages
        if message.role == "assistant" and message.turn_id == submission.turn.id
    ]
    assert answers == [
        "已收到你的选择，我会按确认结果继续处理。"
        "下一次需要你操作时，DataPilot 会在这里提醒你。"
    ]
    assert "未能生成可安全展示的回复" not in answers[0]


def test_outbox_claim_retry_and_resource_lease_ownership(tmp_path: Path):
    store, session_id = _v1_store(tmp_path)
    binding = _binding(store, session_id).binding
    claimed = store.claim_outbox(worker_id="worker-1")

    assert len(claimed) == 1
    assert claimed[0].attempts == 1
    retried = store.complete_outbox(
        claimed[0].outbox_id,
        worker_id="worker-1",
        success=False,
        error="temporary",
        retry_at="2000-01-01T00:00:00.000+00:00",
    )
    assert retried.status == "pending"
    claimed_again = store.claim_outbox(worker_id="worker-2")
    done = store.complete_outbox(claimed_again[0].outbox_id, worker_id="worker-2")
    assert done.status == "completed"

    lease = store.acquire_resource_lease(
        "global:heavy_navigation_writer",
        owner_id="run-1",
        kind="heavy_writer",
        lease_seconds=60,
        task_id=binding.task_id,
    )
    with pytest.raises(ContractConflictError) as raised:
        store.acquire_resource_lease(
            "global:heavy_navigation_writer",
            owner_id="run-2",
            kind="heavy_writer",
            lease_seconds=60,
            task_id=binding.task_id,
        )
    assert raised.value.code == "resource_lease_unavailable"
    assert store.release_resource_lease(lease.lease_id, owner_id="run-1") is True

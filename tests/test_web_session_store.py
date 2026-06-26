from vla_data_juicer_agents.web.schemas import (
    CreateTurnRequest,
    SessionRecord,
    generate_session_title,
)


def test_generate_session_title_uses_first_30_chars():
    title = generate_session_title("处理 20270605 的室外导航数据，并进行 dry-run 验证")

    assert title == "处理 20270605 的室外导航数据，并进行 dry-run"


def test_turn_request_rejects_empty_message():
    try:
        CreateTurnRequest(message="   ")
    except ValueError as exc:
        assert "message must not be empty" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_session_record_serializes_status():
    record = SessionRecord(
        id="session_1",
        title="处理 20270605 的室外导航数据",
        status="active",
        created_at="2026-06-26T10:00:00+08:00",
        updated_at="2026-06-26T10:01:00+08:00",
    )

    assert record.model_dump()["status"] == "active"

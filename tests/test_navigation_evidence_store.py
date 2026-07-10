import json

import pytest

from vla_data_juicer_agents.navigation.context_budget import (
    ensure_payload_within_limit,
    serialized_chars,
)
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore


def test_evidence_read_is_task_scoped_and_paginated(tmp_path):
    store = FileNavigationEvidenceStore(tmp_path / "evidence")
    descriptor = store.write(
        "nav-1",
        1,
        "topics",
        "inspect_raw_date_tool",
        {"rows": list(range(20)), "ignored": "value"},
        "20 rows",
    )

    page = store.read("nav-1", descriptor.ref, fields=["rows"], cursor=5, limit=3)

    assert page["data"] == {"rows": [5, 6, 7]}
    assert page["next_cursor"] == 8
    with pytest.raises(PermissionError):
        store.read("nav-2", descriptor.ref)


def test_evidence_write_is_canonical_and_survives_store_restart(tmp_path):
    root = tmp_path / "evidence"
    first_store = FileNavigationEvidenceStore(root)
    descriptor = first_store.write(
        "nav-1",
        4,
        "metadata",
        "inspect_raw_date_tool",
        {"z": 1, "unicode": "导航", "a": [2, 3]},
        "metadata summary",
    )

    evidence_files = list((root / "nav-1" / "4").glob("*.json"))

    assert len(evidence_files) == 1
    assert evidence_files[0].read_text(encoding="utf-8") == '{"a":[2,3],"unicode":"导航","z":1}'
    assert descriptor.byte_size == len(evidence_files[0].read_bytes())
    assert "path" not in descriptor.model_dump()
    restarted_store = FileNavigationEvidenceStore(root)
    assert restarted_store.read("nav-1", descriptor.ref)["data"] == {
        "a": [2, 3],
        "unicode": "导航",
        "z": 1,
    }


def test_evidence_read_paginates_top_level_lists(tmp_path):
    store = FileNavigationEvidenceStore(tmp_path / "evidence")
    descriptor = store.write("nav-1", 1, "rows", "inspect", list(range(6)), "six rows")

    page = store.read("nav-1", descriptor.ref, cursor=2, limit=2)

    assert page == {"data": [2, 3], "next_cursor": 4}


def test_evidence_read_enforces_public_response_budget(tmp_path):
    store = FileNavigationEvidenceStore(tmp_path / "evidence")
    descriptor = store.write("nav-1", 1, "large", "inspect", {"value": "x" * 5_600}, "large")

    with pytest.raises(ValueError, match=r"evidence_read exceeds 5500 characters"):
        store.read("nav-1", descriptor.ref)


def test_context_budget_counts_compact_unicode_json():
    payload = {"value": "导航"}

    assert serialized_chars(payload) == len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    assert ensure_payload_within_limit(payload, max_chars=100, label="payload") is payload
    with pytest.raises(ValueError, match=r"payload exceeds 5 characters"):
        ensure_payload_within_limit(payload, max_chars=5, label="payload")

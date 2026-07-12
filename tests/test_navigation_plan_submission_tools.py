from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from vla_data_juicer_agents.navigation.catalog import (
    CAPABILITY_CATALOG_REVISION,
    list_navigation_tool_capabilities,
)
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
    CalibrationInventoryObservation,
    EvidenceWrite,
    GridmapArtifactsObservation,
    LocalizationSourcesObservation,
    RawMetadataObservation,
    RuntimeAssetsObservation,
    SensorCandidatesObservation,
    SensorRoleCandidate,
    TopicCandidatesObservation,
    TopicMeasurement,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.plan_models import PlanValidationIssue
from vla_data_juicer_agents.navigation.plan_store import SqliteNavigationPlanRepository
from vla_data_juicer_agents.navigation.plan_submission_tools import (
    build_navigation_plan_submission_tools,
)
from vla_data_juicer_agents.navigation.planning_context import (
    compute_planning_context_revision,
)
from vla_data_juicer_agents.navigation.task_state import (
    NavigationArtifactSnapshot,
    NavigationTask,
    NavigationTaskPhase,
)
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore


SUCCESS_KEYS = {
    "ok",
    "plan_id",
    "plan_revision",
    "step_count",
    "status",
    "next_action",
}
FAILURE_KEYS = {"ok", "error_type", "errors", "retry"}


def _invoke_tool(tool, arguments):
    async def _call():
        payload = tool(**arguments)
        if inspect.isawaitable(payload):
            payload = await payload
        return _decode_tool_payload(payload)

    return asyncio.run(_call())


def _decode_tool_payload(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return json.loads(payload)
    if hasattr(payload, "content"):
        return _decode_tool_payload(payload.content)
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, (list, tuple)):
        return json.loads(
            "".join(
                block.text
                for block in payload
                if hasattr(block, "text") and isinstance(block.text, str)
            )
        )
    raise TypeError(f"unsupported tool payload: {type(payload)!r}")


@dataclass
class Services:
    task: NavigationTask
    observation_store: SqliteNavigationObservationStore
    evidence_store: FileNavigationEvidenceStore
    plan_store: SqliteNavigationPlanRepository
    tools: dict
    planning_context_revision: str
    evidence_refs: dict[str, str]


def _artifact(date: str, *, sync: bool = False) -> ArtifactStateObservation:
    return ArtifactStateObservation(
        snapshot=NavigationArtifactSnapshot(
            date=date,
            segments=["20260710_120000"],
            raw_input_exists=True,
            sync_data_exists=sync,
        )
    )


def _append_fact(
    store: SqliteNavigationObservationStore,
    evidence_store: FileNavigationEvidenceStore,
    task: NavigationTask,
    kind,
    payload,
):
    return store.append(
        task.task_id,
        task.phase,
        kind,
        [payload],
        [
            EvidenceWrite(
                kind=kind,
                source_tool=f"inspect_{kind}_tool",
                payload=payload.model_dump(mode="json"),
                summary=f"Measured {kind}",
            )
        ],
        evidence_store,
    )


def build_services(tmp_path: Path, phase: str) -> Services:
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    created = task_store.create_or_update_task(
        date="20260710",
        segments=["20260710_120000"],
        scene_mode="out" if phase == "finish_processing" else None,
    )
    task = task_store.update_task(created.task_id, phase=phase)
    observation_store = SqliteNavigationObservationStore(db_path)
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    refs: dict[str, str] = {}

    if phase == "extract_sync":
        facts = [
            ("artifact_state", _artifact(task.date)),
            (
                "raw_metadata",
                RawMetadataObservation(
                    segments=["20260710_120000"],
                    topics=[
                        TopicMeasurement(topic="/camera/front/image", message_count=100),
                        TopicMeasurement(topic="/lidar/points", message_count=100),
                        TopicMeasurement(topic="/localization/odom", message_count=100),
                    ],
                ),
            ),
            (
                "sensor_candidates",
                SensorCandidatesObservation(
                    candidates=[
                        SensorRoleCandidate(
                            role="fisheye_front",
                            topic="/camera/front/image",
                            confidence=0.99,
                        ),
                        SensorRoleCandidate(
                            role="lidar", topic="/lidar/points", confidence=0.99
                        ),
                        SensorRoleCandidate(
                            role="odom", topic="/localization/odom", confidence=0.99
                        ),
                    ]
                ),
            ),
            (
                "topic_candidates",
                TopicCandidatesObservation(
                    available_topics=[
                        "/camera/front/image",
                        "/lidar/points",
                        "/localization/odom",
                    ],
                    suggested_role_names={
                        "fisheye_front": ["/camera/front/image"],
                        "lidar": ["/lidar/points"],
                        "odom": ["/localization/odom"],
                    },
                ),
            ),
        ]
    else:
        facts = [
            ("artifact_state", _artifact(task.date, sync=True)),
            (
                "gridmap_artifacts",
                GridmapArtifactsObservation(
                    existing_gridmap_paths=["/data/grid_map.pcd"],
                    pcd_sources=["/data/source.pcd"],
                    projection_ready=False,
                ),
            ),
            (
                "runtime_assets",
                RuntimeAssetsObservation(
                    pcd_gridmap_tool_available=True,
                    manual_annotation_gui_available=True,
                    projection_variants={"cjl_with_gridmap": True},
                ),
            ),
            (
                "calibration_inventory",
                CalibrationInventoryObservation(sensor_sources=["fisheye_front"]),
            ),
            (
                "localization_sources",
                LocalizationSourcesObservation(
                    available_sources=["odom"], conversion_available=True
                ),
            ),
        ]

    for kind, payload in facts:
        revision = _append_fact(
            observation_store,
            evidence_store,
            task,
            kind,
            payload,
        )
        refs[kind] = revision.evidence_refs[0]

    latest = observation_store.latest(task.task_id)
    assert latest is not None
    planning_context_revision = compute_planning_context_revision(
        task=task,
        observation_revision=latest.revision,
        capability_revision=CAPABILITY_CATALOG_REVISION,
    )
    plan_store = SqliteNavigationPlanRepository(db_path)
    tools = {
        tool.name: tool
        for tool in build_navigation_plan_submission_tools(
            task=task,
            observation_store=observation_store,
            evidence_store=evidence_store,
            plan_store=plan_store,
            capabilities=list_navigation_tool_capabilities(),
        )
    }
    return Services(
        task=task,
        observation_store=observation_store,
        evidence_store=evidence_store,
        plan_store=plan_store,
        tools=tools,
        planning_context_revision=planning_context_revision,
        evidence_refs=refs,
    )


def valid_extract_plan_payload(services: Services) -> dict:
    refs = services.evidence_refs
    return {
        "decisions": {
            "sensor_bindings": {
                "bindings": {
                    "fisheye_front": "/camera/front/image",
                    "lidar": "/lidar/points",
                    "odom": "/localization/odom",
                },
                "reason": "Observed matching sensor candidates.",
                "evidence_refs": [refs["sensor_candidates"]],
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
                "reason": "Selected only observed topics.",
                "evidence_refs": [refs["topic_candidates"]],
            },
            "time_sync": {
                "reference_sensor": "lidar",
                "method": "nearest_timestamp",
                "tolerance_ms": 50,
                "reason": "Lidar is the measured reference stream.",
                "evidence_refs": [refs["raw_metadata"]],
            },
        },
        "steps": [
            {
                "step_id": "prepare_raw",
                "action": "prepare_raw_data",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": [],
            },
            {
                "step_id": "extract_sync",
                "action": "extract_and_sync_navigation_data",
                "variant": "explicit_topic_params",
                "arguments": {"processes_num": 8},
                "depends_on": ["prepare_raw"],
                "failure_policy": "stop",
                "decision_refs": ["sensor_bindings", "topic_selection", "time_sync"],
            },
        ],
    }


def valid_finish_plan_payload(services: Services) -> dict:
    refs = services.evidence_refs
    return {
        "decisions": {
            "localization": {
                "source": "odom",
                "conversion": "odom_to_ins",
                "reason": "Odom and the converter were observed.",
                "evidence_refs": [refs["localization_sources"]],
            },
            "gridmap": {
                "source": "existing_gridmap",
                "reason": "An existing gridmap was measured.",
                "evidence_refs": [refs["gridmap_artifacts"]],
            },
            "calibration": {
                "mode": "hardcoded_with_user_confirmation",
                "selected_sensor_source": "fisheye_front",
                "requires_user_confirmation": True,
                "reason": "The measured calibration source requires confirmation.",
                "evidence_refs": [refs["calibration_inventory"]],
            },
        },
        "steps": [
            {
                "step_id": "confirm_calibration",
                "action": "confirm_navigation_calibration_params",
                "variant": "default",
                "arguments": {},
                "depends_on": [],
                "failure_policy": "stop",
                "decision_refs": ["calibration"],
            },
            {
                "step_id": "tracking",
                "action": "run_tracking",
                "variant": "default",
                "arguments": {},
                "depends_on": ["confirm_calibration"],
                "failure_policy": "stop",
                "decision_refs": ["localization"],
            },
            {
                "step_id": "prepare_gridmap",
                "action": "prepare_gridmap_for_projection",
                "variant": "copy_existing_gridmap",
                "arguments": {},
                "depends_on": ["tracking"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
            {
                "step_id": "projection",
                "action": "run_projection_and_trajectory",
                "variant": "cjl_with_gridmap",
                "arguments": {},
                "depends_on": ["prepare_gridmap"],
                "failure_policy": "stop",
                "decision_refs": ["localization", "gridmap"],
            },
            {
                "step_id": "validate_outputs",
                "action": "validate_navigation_outputs",
                "variant": "expect_gridmap",
                "arguments": {},
                "depends_on": ["projection"],
                "failure_policy": "stop",
                "decision_refs": ["gridmap"],
            },
        ],
    }


def call_submit_extract(services: Services, payload: dict, *, revision: str | None = None):
    return _invoke_tool(
        services.tools["submit_extract_sync_plan_tool"],
        {
            "planning_context_revision": revision or services.planning_context_revision,
            "plan": payload,
        },
    )


def call_submit_finish(services: Services, payload: dict, *, revision: str | None = None):
    return _invoke_tool(
        services.tools["submit_finish_processing_plan_tool"],
        {
            "planning_context_revision": revision or services.planning_context_revision,
            "plan": payload,
        },
    )


def _audit_rows(services: Services):
    with sqlite3.connect(services.plan_store.db_path) as connection:
        return connection.execute(
            "SELECT candidate_json, validation_json "
            "FROM navigation_plan_submission_attempts ORDER BY rowid"
        ).fetchall()


def _ledger_count(services: Services) -> int:
    with sqlite3.connect(services.plan_store.db_path) as connection:
        return connection.execute(
            "SELECT count(*) FROM navigation_task_steps WHERE plan_id IS NOT NULL"
        ).fetchone()[0]


def test_builder_exposes_only_active_phase_complete_typed_schema(tmp_path):
    services = build_services(tmp_path, "extract_sync")

    assert set(services.tools) == {"submit_extract_sync_plan_tool"}
    schema = services.tools["submit_extract_sync_plan_tool"].input_schema
    assert set(schema["properties"]) == {"planning_context_revision", "plan"}
    assert set(schema["required"]) == {"planning_context_revision", "plan"}
    serialized = json.dumps(schema)
    assert "ExtractSyncPlanInput" in serialized
    assert "FinishProcessingPlanInput" not in serialized
    assert "task_id" not in schema["properties"]


def test_valid_extract_submission_returns_exact_six_field_success_contract(tmp_path):
    services = build_services(tmp_path, "extract_sync")

    result = call_submit_extract(services, valid_extract_plan_payload(services))

    assert set(result) == SUCCESS_KEYS
    assert result["ok"] is True
    assert result["plan_revision"] == 1
    assert result["step_count"] == 2
    assert result["status"] == "active"
    assert result["next_action"] == "prepare_raw_data"
    assert "workflow_plan_json" not in result
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 4_000


def test_valid_finish_plan_does_not_require_nested_topic_params_copy(tmp_path):
    services = build_services(tmp_path, "finish_processing")

    result = call_submit_finish(services, valid_finish_plan_payload(services))

    assert set(result) == SUCCESS_KEYS
    assert result["ok"] is True
    assert "workflow_plan_json" not in result
    assert "topic_params" not in json.dumps(result)


def test_invalid_complete_submission_never_creates_partial_state(tmp_path):
    services = build_services(tmp_path, "finish_processing")
    payload = valid_finish_plan_payload(services)
    del payload["decisions"]["localization"]

    result = call_submit_finish(services, payload)

    assert set(result) == FAILURE_KEYS
    assert result["ok"] is False
    assert result["retry"] == "resubmit_complete_plan"
    assert "draft" not in result and "schema" not in result
    assert services.plan_store.get_active(services.task.task_id, "finish_processing") is None
    assert _ledger_count(services) == 0
    rows = _audit_rows(services)
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == payload
    assert json.loads(rows[0][1])["errors"][0]["path"] == "plan.decisions.localization"


def test_stale_context_revision_is_audited_and_cannot_activate(tmp_path):
    services = build_services(tmp_path, "extract_sync")

    result = call_submit_extract(
        services,
        valid_extract_plan_payload(services),
        revision="stale-context",
    )

    assert set(result) == FAILURE_KEYS
    assert result["error_type"] == "planning_context_mismatch"
    assert result["errors"][0]["code"] == "stale_planning_context_revision"
    assert len(_audit_rows(services)) == 1
    assert services.plan_store.get_active(services.task.task_id, "extract_sync") is None


def test_invalid_retry_requires_a_complete_replacement_and_only_retry_activates(tmp_path):
    services = build_services(tmp_path, "extract_sync")
    invalid = valid_extract_plan_payload(services)
    invalid["steps"][1]["depends_on"] = ["missing"]

    first = call_submit_extract(services, invalid)
    second = call_submit_extract(services, valid_extract_plan_payload(services))

    assert first["ok"] is False
    assert second["ok"] is True
    assert services.plan_store.get_active(services.task.task_id, "extract_sync").plan_id == second["plan_id"]
    assert _ledger_count(services) == 2
    assert len(_audit_rows(services)) == 2


def test_invalid_replacement_preserves_existing_active_plan_and_ledger(tmp_path):
    services = build_services(tmp_path, "extract_sync")
    active = call_submit_extract(services, valid_extract_plan_payload(services))
    invalid = valid_extract_plan_payload(services)
    invalid["decisions"]["topic_selection"]["topic_whitelist"] = ["/invented"]

    result = call_submit_extract(services, invalid)

    assert result["ok"] is False
    assert services.plan_store.get_active(services.task.task_id, "extract_sync").plan_id == active["plan_id"]
    assert _ledger_count(services) == 2


def test_failure_response_is_compact_and_uses_concrete_evidence_pointer(
    tmp_path,
    monkeypatch,
):
    services = build_services(tmp_path, "extract_sync")
    latest = services.observation_store.latest(services.task.task_id)
    assert latest is not None
    large_topics = [f"/measured/{index:03d}" for index in reversed(range(50))]
    large_observation = latest.model_copy(
        update={
            "payloads": [
                item.model_copy(
                    update={
                        "available_topics": [
                            *item.available_topics,
                            *large_topics,
                            *large_topics,
                        ]
                    }
                )
                if isinstance(item, TopicCandidatesObservation)
                else item
                for item in latest.payloads
            ]
        }
    )
    monkeypatch.setattr(
        services.observation_store,
        "latest",
        lambda _task_id: large_observation,
    )
    payload = valid_extract_plan_payload(services)
    payload["decisions"]["topic_selection"]["topic_whitelist"] = [
        "/invented/" + ("x" * 200) + str(index) for index in range(40)
    ]

    result = call_submit_extract(services, payload)

    assert set(result) == FAILURE_KEYS
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) <= 3_000
    assert all(word not in serialized for word in ("draft", "schema", "history", "candidate"))
    topic_issues = [
        issue for issue in result["errors"] if issue["code"] == "unobserved_topic"
    ]
    assert topic_issues
    assert all(
        issue["allowed_values"]
        == [f"evidence_ref:{services.evidence_refs['topic_candidates']}"]
        for issue in topic_issues
    )


def test_completed_markers_without_required_payloads_are_audited_and_never_activate(
    tmp_path,
    monkeypatch,
):
    services = build_services(tmp_path, "finish_processing")
    latest = services.observation_store.latest(services.task.task_id)
    assert latest is not None
    incomplete = latest.model_copy(
        update={
            "payloads": [
                payload
                for payload in latest.payloads
                if not isinstance(
                    payload,
                    (
                        CalibrationInventoryObservation,
                        LocalizationSourcesObservation,
                        RuntimeAssetsObservation,
                    ),
                )
            ]
        }
    )
    monkeypatch.setattr(
        services.observation_store,
        "latest",
        lambda _task_id: incomplete,
    )

    result = call_submit_finish(services, valid_finish_plan_payload(services))

    assert result["ok"] is False
    assert {
        issue["path"]
        for issue in result["errors"]
        if issue["code"] == "missing_required_observation_payload"
    } == {
        "observation.payloads.calibration_inventory",
        "observation.payloads.localization_sources",
        "observation.payloads.runtime_assets",
    }
    assert services.plan_store.get_active(
        services.task.task_id,
        "finish_processing",
    ) is None
    assert _ledger_count(services) == 0
    assert len(_audit_rows(services)) == 1


def test_gui_runtime_unavailable_blocks_submission_but_available_runtime_succeeds(
    tmp_path,
    monkeypatch,
):
    services = build_services(tmp_path, "finish_processing")
    payload = valid_finish_plan_payload(services)
    payload["steps"].insert(
        1,
        {
            "step_id": "initial_annotation",
            "action": "run_initial_annotation_gui",
            "variant": "human_gui",
            "arguments": {},
            "depends_on": ["confirm_calibration"],
            "failure_policy": "stop",
            "decision_refs": ["calibration"],
        },
    )
    available = services.observation_store.latest(services.task.task_id)
    assert available is not None
    unavailable = available.model_copy(
        update={
            "payloads": [
                item.model_copy(update={"manual_annotation_gui_available": False})
                if isinstance(item, RuntimeAssetsObservation)
                else item
                for item in available.payloads
            ]
        }
    )
    monkeypatch.setattr(
        services.observation_store,
        "latest",
        lambda _task_id: unavailable,
    )

    blocked = call_submit_finish(services, payload)

    assert blocked["ok"] is False
    assert PlanValidationIssue.model_validate(
        next(
            issue
            for issue in blocked["errors"]
            if issue["code"] == "runtime_action_unavailable"
        )
    ) == PlanValidationIssue(
        path="plan.steps.1.action",
        code="runtime_action_unavailable",
        message="Manual annotation GUI is unavailable in observed runtime assets",
        allowed_values=[
            f"evidence_ref:{services.evidence_refs['runtime_assets']}"
        ],
    )
    assert services.plan_store.get_active(
        services.task.task_id,
        "finish_processing",
    ) is None
    assert _ledger_count(services) == 0

    monkeypatch.setattr(
        services.observation_store,
        "latest",
        lambda _task_id: available,
    )
    accepted = call_submit_finish(services, payload)

    assert accepted["ok"] is True
    assert accepted["step_count"] == 6


def test_audit_failure_prevents_activation_and_returns_stable_internal_failure(
    tmp_path,
    monkeypatch,
):
    services = build_services(tmp_path, "extract_sync")

    def fail_audit(_attempt):
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(services.plan_store, "record_attempt", fail_audit)

    result = call_submit_extract(services, valid_extract_plan_payload(services))

    assert set(result) == FAILURE_KEYS
    assert result["error_type"] == "submission_audit_failed"
    assert services.plan_store.get_active(services.task.task_id, "extract_sync") is None
    assert _ledger_count(services) == 0


def test_activation_failure_keeps_audited_candidate_but_never_exposes_it_as_active(
    tmp_path,
    monkeypatch,
):
    services = build_services(tmp_path, "extract_sync")

    def fail_activation(*_args, **_kwargs):
        raise sqlite3.IntegrityError("activation failed")

    monkeypatch.setattr(services.plan_store, "activate", fail_activation)

    result = call_submit_extract(services, valid_extract_plan_payload(services))

    assert set(result) == FAILURE_KEYS
    assert result["error_type"] == "plan_activation_failed"
    assert len(_audit_rows(services)) == 1
    assert json.loads(_audit_rows(services)[0][1])["ok"] is True
    assert services.plan_store.get_active(services.task.task_id, "extract_sync") is None
    assert _ledger_count(services) == 0


def test_stale_planning_toolkit_cannot_audit_or_activate_after_session_rebind(tmp_path):
    services = build_services(tmp_path, "extract_sync")
    task_store = SqliteNavigationTaskStore(services.plan_store.db_path)
    bound = task_store.update_task(
        services.task.task_id,
        created_by_web_session_id="web-owner",
        latest_web_session_id="web-owner",
        agentscope_session_id="as-old",
    )
    tools = {
        tool.name: tool
        for tool in build_navigation_plan_submission_tools(
            task=bound,
            observation_store=services.observation_store,
            evidence_store=services.evidence_store,
            plan_store=services.plan_store,
            capabilities=list_navigation_tool_capabilities(),
            expected_web_session_id="web-owner",
            expected_agentscope_session_id="as-old",
        )
    }
    task_store.create_or_update_task(
        date=bound.date,
        segments=bound.segments,
        scene_mode=bound.scene_mode,
        web_session_id="web-owner",
        agentscope_session_id="as-new",
    )

    result = _invoke_tool(
        tools["submit_extract_sync_plan_tool"],
        {
            "planning_context_revision": services.planning_context_revision,
            "plan": valid_extract_plan_payload(services),
        },
    )

    assert result["ok"] is False
    assert services.plan_store.get_active(bound.task_id, "extract_sync") is None
    with sqlite3.connect(services.plan_store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM navigation_plan_submission_attempts WHERE task_id = ?",
            (bound.task_id,),
        ).fetchone()[0] == 0


def test_rebind_after_serialized_attempt_keeps_audit_but_rejects_activation(tmp_path, monkeypatch):
    services = build_services(tmp_path, "extract_sync")
    task_store = SqliteNavigationTaskStore(services.plan_store.db_path)
    bound = task_store.update_task(
        services.task.task_id,
        created_by_web_session_id="web-owner",
        latest_web_session_id="web-owner",
        agentscope_session_id="as-old",
    )
    original_record = services.plan_store.record_attempt

    def record_then_rebind(attempt, **kwargs):
        result = original_record(attempt, **kwargs)
        task_store.create_or_update_task(
            date=bound.date, segments=bound.segments, scene_mode=bound.scene_mode,
            web_session_id="web-owner", agentscope_session_id="as-new",
        )
        return result

    monkeypatch.setattr(services.plan_store, "record_attempt", record_then_rebind)
    tool = build_navigation_plan_submission_tools(
        task=bound, observation_store=services.observation_store,
        evidence_store=services.evidence_store, plan_store=services.plan_store,
        capabilities=list_navigation_tool_capabilities(),
        expected_web_session_id="web-owner", expected_agentscope_session_id="as-old",
    )[0]

    result = _invoke_tool(tool, {
        "planning_context_revision": services.planning_context_revision,
        "plan": valid_extract_plan_payload(services),
    })

    assert result["error_type"] == "plan_activation_failed"
    assert len(_audit_rows(services)) == 1
    assert services.plan_store.get_active(bound.task_id, "extract_sync") is None

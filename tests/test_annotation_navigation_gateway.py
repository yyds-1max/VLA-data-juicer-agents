from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationValidationError,
    PostprocessingSpecInput,
)
from vla_data_juicer_agents.annotation.navigation_gateway import (
    AnnotationNavigationGateway,
)
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.annotation.workflow_coordinator import (
    AnnotationWorkflowCoordinator,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.evidence_store import (
    FileNavigationEvidenceStore,
)
from vla_data_juicer_agents.navigation.plan_execution import (
    build_plan_bound_execution_tools,
    complete_annotation_workflow_step,
)
from vla_data_juicer_agents.navigation.plan_models import (
    FinishProcessingPlanInput,
    TrajectoryReviewPlanInput,
)
from vla_data_juicer_agents.navigation.plan_store import (
    SqliteNavigationPlanRepository,
)
from vla_data_juicer_agents.navigation.task_store import (
    SqliteNavigationTaskStore,
)


PROFILE_SHA = "a" * 64


class _GatewayService:
    def __init__(self, clip_data_root: Path) -> None:
        self.clip_data_root = clip_data_root
        self.exists = False
        self.created: list[object] = []
        self.linked: list[dict[str, object]] = []
        self.requested_scopes: list[list[str]] = []

    def get_processing_facts(
        self,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> dict[str, object]:
        assert dataset_date == "20270605"
        self.requested_scopes.append(list(source_clips))
        return {"exists": self.exists}

    @staticmethod
    def list_calibration_profiles(*, purpose: str) -> dict[str, object]:
        assert purpose == "processing"
        return {
            "profiles": [
                {
                    "profile_ref": "20260529_go2w",
                    "content_sha256": PROFILE_SHA,
                }
            ]
        }

    def create_job(self, request, *, idempotency_key: str) -> dict[str, object]:
        assert idempotency_key.startswith("datapilot:create_annotation_job:")
        self.exists = True
        self.created.append(request)
        return {"status": "preparing"}

    def resolve_scope_binding(
        self,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> dict[str, object]:
        assert dataset_date == "20270605"
        assert source_clips
        return {
            "job_ref": "job_" + "1" * 32,
            "job_status": "preparing",
            "job_revision": 0,
        }

    def link_navigation_task(self, **kwargs) -> dict[str, object]:
        self.linked.append(dict(kwargs))
        return {"ok": True}


class _BoundGateway(AnnotationNavigationGateway):
    def __init__(self, service: _GatewayService) -> None:
        self.service = service
        self.navigation_db_path = Path("/unused/navigation.sqlite")
        self.bound_task = SimpleNamespace(
            task_id="nav-task",
            date="20270605",
            segments=None,
        )
        calibration = SimpleNamespace(
            mode="selected_profile",
            selected_sensor_source="20260529_go2w",
        )
        self.bound_plan = SimpleNamespace(
            plan_id="plan-1",
            plan=SimpleNamespace(
                decisions=SimpleNamespace(calibration=calibration),
            ),
        )

    def _bound_finish_plan(self, **_kwargs):
        return self.bound_task, self.bound_plan


def test_gateway_resolves_all_clips_server_side_and_reuses_job(
    tmp_path: Path,
) -> None:
    clip_root = tmp_path / "clip_data" / "20270605"
    for clip in ("20260605_160904", "20260605_152930"):
        (clip_root / clip / "sync_data").mkdir(parents=True)
    (clip_root / "not-synchronized").mkdir()
    service = _GatewayService(tmp_path / "clip_data")
    gateway = _BoundGateway(service)

    first = gateway.begin_annotation_from_plan(
        navigation_task_id="nav-task",
        plan_id="plan-1",
        step_id="annotation",
    )
    second = gateway.begin_annotation_from_plan(
        navigation_task_id="nav-task",
        plan_id="plan-1",
        step_id="annotation",
    )

    expected_scope = ["20260605_152930", "20260605_160904"]
    assert service.requested_scopes == [expected_scope, expected_scope]
    assert len(service.created) == 1
    assert service.created[0].source_clips == expected_scope
    assert first["source_clip_count"] == 2
    assert second["source_clip_count"] == 2
    assert all(link["navigation_task_ref"] == "nav-task" for link in service.linked)
    assert all(link["review_ref"] is None for link in service.linked)


def test_gateway_rejects_empty_all_clips_scope(tmp_path: Path) -> None:
    (tmp_path / "clip_data" / "20270605").mkdir(parents=True)
    gateway = _BoundGateway(_GatewayService(tmp_path / "clip_data"))

    with pytest.raises(AnnotationValidationError) as exc:
        gateway.begin_annotation_from_plan(
            navigation_task_id="nav-task",
            plan_id="plan-1",
            step_id="annotation",
        )

    assert exc.value.code == "empty_annotation_scope"


class _StoreBackedPostprocessingService:
    def __init__(self, store: AnnotationStore, clip_data_root: Path) -> None:
        self.store = store
        self.clip_data_root = clip_data_root

    def resolve_scope_binding(
        self,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> dict[str, object]:
        return self.store.resolve_scope_binding(
            dataset_date=dataset_date,
            source_clips=source_clips,
        )

    def link_navigation_task(self, **kwargs) -> dict[str, object]:
        return self.store.link_navigation_task(**kwargs)

    def begin_postprocessing(
        self,
        job_ref: str,
        expected_job_revision: int,
        spec: PostprocessingSpecInput,
        *,
        idempotency_key: str,
        processing_navigation_task_ref: str | None = None,
    ) -> dict[str, object]:
        return self.store.begin_postprocessing(
            job_ref=job_ref,
            expected_job_revision=expected_job_revision,
            spec=spec.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            processing_navigation_task_ref=processing_navigation_task_ref,
        )


class _BoundPostprocessingGateway(AnnotationNavigationGateway):
    def __init__(
        self,
        service: _StoreBackedPostprocessingService,
        *,
        navigation_task_id: str,
    ) -> None:
        self.service = service
        self.navigation_db_path = Path("/unused/navigation.sqlite")
        self.bound_task = SimpleNamespace(
            task_id=navigation_task_id,
            date="20270623",
            segments=["20260623_145550"],
        )
        self.bound_plan = SimpleNamespace(plan_id="postprocessing-plan")

    def _bound_finish_plan(self, **_kwargs):
        return self.bound_task, self.bound_plan

    @staticmethod
    def _postprocessing_spec(**_kwargs) -> PostprocessingSpecInput:
        return PostprocessingSpecInput(
            localization_kind="odom",
            gridmap_decision="copy_existing_gridmap",
            trajectory_variant="cjl_0525_with_gridmap",
            plan_sha256="d" * 64,
            observations_sha256="e" * 64,
        )


def _seed_unlinked_tracked_gateway_job(
    store: AnnotationStore,
    tmp_path: Path,
) -> dict[str, object]:
    job_ref = "job_" + "a" * 32
    created = store.create_job(
        job_ref=job_ref,
        dataset_date="20270623",
        source_clips=["20260623_145550"],
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "20260529_go2w",
            "content_sha256": PROFILE_SHA,
        },
        snapshot_dir=tmp_path / "processing-calibration",
        snapshot_files=[],
        reserved_bytes=1,
        idempotency_key="seed-unlinked-tracked-job",
    )
    timestamp = "2026-07-28T00:00:00+00:00"
    with store._write() as connection:
        job_id = int(
            connection.execute(
                "SELECT id FROM annotation_jobs WHERE job_ref = ?",
                (job_ref,),
            ).fetchone()["id"]
        )
        # This fixture starts at a historical tracked fact.  The synthetic
        # create-job prepare run is outside that fact and must not masquerade
        # as an active writer.
        connection.execute(
            "DELETE FROM runtime_runs WHERE job_id = ? AND kind = 'prepare'",
            (job_id,),
        )
        connection.execute(
            """
            UPDATE annotation_jobs
            SET status = 'tracked', state_revision = 1, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, job_id),
        )
        connection.execute(
            """
            INSERT INTO annotation_segments (
                segment_ref, job_id, ordinal, source_clip, status,
                state_revision, draft_revision, submitted_revision,
                private_segment_key, private_segment_root,
                private_first_frame_path, first_frame_width,
                first_frame_height, first_frame_sha256, first_frame_etag,
                created_at, updated_at
            ) VALUES (?, ?, 1, ?, 'tracked', 1, 1, 1, ?, ?, ?,
                      1920, 1536, ?, ?, ?, ?)
            """,
            (
                "segment_" + "b" * 32,
                job_id,
                "20260623_145550",
                "private-segment",
                str(tmp_path / "tracked" / "private-segment"),
                str(tmp_path / "tracked" / "private-segment" / "first.jpg"),
                "c" * 64,
                "c" * 64,
                timestamp,
                timestamp,
            ),
        )
    return store.get_job(str(created["job_ref"]))


def test_tracked_job_allows_fresh_navigation_attempt_to_take_authority(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_unlinked_tracked_gateway_job(store, tmp_path)
    for task_ref in ("historical-navigation-attempt", "fresh-navigation-attempt"):
        assert store.link_navigation_task(
            job_ref=str(job["job_ref"]),
            review_ref=None,
            navigation_task_ref=task_ref,
            parent_navigation_task_ref=None,
            link_kind="processing",
            idempotency_key=f"link:{task_ref}",
        ) == {"linked": True, "link_kind": "processing"}

    with store._connect() as connection:
        assert connection.execute(
            """
            SELECT l.navigation_task_ref
            FROM annotation_processing_authorities a
            JOIN annotation_task_links l ON l.id = a.link_id
            """
        ).fetchone()[0] == "fresh-navigation-attempt"
        assert [
            str(row["navigation_task_ref"])
            for row in connection.execute(
                """
                SELECT navigation_task_ref
                FROM annotation_task_links
                WHERE link_kind = 'processing'
                ORDER BY id
                """
            ).fetchall()
        ] == [
            "historical-navigation-attempt",
            "fresh-navigation-attempt",
        ]


def test_existing_tracked_job_claims_processing_owner_and_delivers_completion(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job = _seed_unlinked_tracked_gateway_job(store, tmp_path)
    service = _StoreBackedPostprocessingService(
        store,
        tmp_path / "clip_data",
    )
    owner = _BoundPostprocessingGateway(
        service,
        navigation_task_id="navigation-processing-owner",
    )
    with store._connect() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM annotation_task_links
            WHERE link_kind = 'processing'
            """
        ).fetchone()[0] == 0

    started = owner.begin_postprocessing_from_plan(
        navigation_task_id="navigation-processing-owner",
        plan_id="postprocessing-plan",
        step_id="postprocessing",
    )
    replay = owner.begin_postprocessing_from_plan(
        navigation_task_id="navigation-processing-owner",
        plan_id="postprocessing-plan",
        step_id="postprocessing",
    )

    assert started["status"] == "postprocessing"
    assert replay == started
    assert store.resolve_navigation_task_binding(
        navigation_task_ref="navigation-processing-owner",
        link_kind="processing",
    )["job_ref"] == job["job_ref"]

    competing_owner = _BoundPostprocessingGateway(
        service,
        navigation_task_id="different-processing-owner",
    )
    with pytest.raises(AnnotationConflictError) as raised:
        competing_owner.begin_postprocessing_from_plan(
            navigation_task_id="different-processing-owner",
            plan_id="postprocessing-plan",
            step_id="postprocessing",
        )
    assert raised.value.code == "annotation_processing_active_attempt_conflict"

    current = store.get_job(str(job["job_ref"]))
    artifact_root = tmp_path / "postprocessing-candidate"
    completed = store.complete_postprocessing(
        job_ref=str(job["job_ref"]),
        expected_job_revision=int(current["state_revision"]),
        trajectories=[
            {
                "segment_ref": "segment_" + "b" * 32,
                "state": {},
                "private_artifact_path": str(
                    artifact_root / "trajectory.json"
                ),
                "private_compatibility_path": str(
                    artifact_root / "trajectory_0525.json"
                ),
                "artifact_sha256": "f" * 64,
                "artifact_manifest_ref": "artifact_manifest_" + "d" * 32,
            }
        ],
        idempotency_key="complete-owned-postprocessing",
    )
    assert completed["status"] == "annotated"
    assert owner.begin_postprocessing_from_plan(
        navigation_task_id="navigation-processing-owner",
        plan_id="postprocessing-plan",
        step_id="postprocessing",
    )["completed"] is True
    with store._connect() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM annotation_task_links
            WHERE link_kind = 'processing'
            """
        ).fetchone()[0] == 1

    claimed = store.claim_workflow_handoff_delivery(
        worker_id="postprocessing-handoff-worker",
    )
    assert claimed is not None
    assert claimed["kind"] == "postprocessing_completed"
    assert claimed["navigation_task_ref"] == "navigation-processing-owner"


class _LinkedReviewFactsService:
    def __init__(self) -> None:
        self.requested_scopes: list[list[str]] = []

    @staticmethod
    def resolve_navigation_task_binding(**_kwargs) -> dict[str, object]:
        return {
            "dataset_date": "20270623",
            "source_clips": ["20260623_145550"],
            "ignored_review_refs": ["review_" + "8" * 32],
            "ignored_private_path": "/private/linked-review.json",
        }

    def get_processing_facts(
        self,
        *,
        dataset_date: str,
        source_clips: list[str],
    ) -> dict[str, object]:
        assert dataset_date == "20270623"
        self.requested_scopes.append(list(source_clips))
        return {
            "exists": True,
            "job_status": "annotated",
            "segment_counts": {"annotated": 6, "skipped": 0},
            "review_counts": {"pending": 6},
            "ready_for_postprocessing": False,
            "ignored_review_ref": "review_" + "7" * 32,
            "ignored_private_path": "/private/trajectory.json",
        }


def test_linked_review_facts_use_frozen_lineage_without_exposing_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(database)
    task = task_store.create_task_attempt(
        request="Fix the linked trajectory",
        target="trajectory_review",
        date="20270623",
        # This request text projection is deliberately different from the
        # authoritative AnnotationTaskLink scope.
        segments=["20260623_untrusted_extra"],
        scene_mode=None,
        dry_run=True,
        web_session_id="web-owner",
        agentscope_session_id="navigation-owner",
        requested_outcome="trajectory_fix",
    ).task
    service = _LinkedReviewFactsService()
    gateway = AnnotationNavigationGateway(
        service=service,
        navigation_db_path=database,
    )

    facts = gateway.get_processing_facts(
        dataset_date="20270623",
        source_clips=["20260623_untrusted_extra"],
        navigation_task_id=task.task_id,
    )

    assert service.requested_scopes == [["20260623_145550"]]
    assert facts["job_status"] == "annotated"
    assert facts["segment_count"] == 6
    assert facts["ready_for_trajectory_review"] is True
    serialized = json.dumps(facts)
    assert "20260623_untrusted_extra" not in serialized
    assert "review_" + "7" * 32 not in serialized
    assert "review_" + "8" * 32 not in serialized
    assert "/private/" not in serialized


class _HandoffStore:
    def __init__(self, handoff: dict[str, object]) -> None:
        self.handoff = handoff
        self.completed: list[dict[str, object]] = []

    def claim_workflow_handoff_delivery(self, *, worker_id: str):
        assert worker_id
        claimed, self.handoff = self.handoff, {}
        return claimed or None

    def complete_workflow_handoff_delivery(self, **kwargs) -> None:
        self.completed.append(dict(kwargs))


class _HandoffService:
    def __init__(self, handoff: dict[str, object]) -> None:
        self.store = _HandoffStore(handoff)
        self.actions: list[tuple[object, ...]] = []

    def job_action(self, *args, **kwargs) -> dict[str, object]:
        self.actions.append((*args, kwargs))
        return {"ok": True}


class _WakeRuntime:
    def __init__(self) -> None:
        self.wakes: list[dict[str, str]] = []

    async def wake_navigation_task_from_workbench(self, **kwargs) -> bool:
        self.wakes.append(dict(kwargs))
        return True


def test_initial_annotation_handoff_starts_tracking_without_web_action(
    tmp_path: Path,
) -> None:
    handoff = {
        "handoff_id": 1,
        "handoff_ref": "handoff_" + "1" * 32,
        "kind": "initial_annotation_submitted",
        "job_ref": "job_" + "2" * 32,
        "job_revision": 7,
        "navigation_task_ref": "nav-task",
        "payload": {},
    }
    service = _HandoffService(handoff)
    coordinator = AnnotationWorkflowCoordinator(
        service=service,
        agentscope_runtime=_WakeRuntime(),
        navigation_workspace_root=tmp_path / "navigation",
    )

    assert asyncio.run(coordinator.process_once()) is True

    assert len(service.actions) == 1
    action, job_ref, request, kwargs = service.actions[0]
    assert action == "tracking"
    assert job_ref == handoff["job_ref"]
    assert request.expected_job_revision == 7
    assert kwargs["idempotency_key"] == (
        f"datapilot:auto_tracking:{handoff['handoff_ref']}"
    )
    assert service.store.completed[-1]["success"] is True


class _ReviewOutcomeService:
    def __init__(self) -> None:
        self.outcome = {
            "status": "returned",
            "review_count": 2,
            "counts": {
                "pending": 1,
                "in_progress": 0,
                "returned": 1,
                "approved": 0,
                "discarded": 0,
            },
            "all_terminal": False,
            "ignored_review_ref": "review_" + "1" * 32,
            "ignored_private_path": "/private/review.json",
        }

    def resolve_navigation_review_outcome(self, **_kwargs):
        return dict(self.outcome)


async def _tool_payload(tool, **arguments) -> dict[str, object]:
    response = await tool(**arguments)
    if isinstance(response, dict):
        return response
    if getattr(response, "metadata", None):
        return dict(response.metadata)
    content = getattr(response, "content", ())
    return json.loads(
        "".join(
            block.text
            for block in content
            if hasattr(block, "text") and isinstance(block.text, str)
        )
    )


def _trajectory_review_plan() -> TrajectoryReviewPlanInput:
    return TrajectoryReviewPlanInput.model_validate(
        {
            "decisions": {
                "review": {
                    "mode": "human_fix",
                    "reason": "Use the linked Web workbench.",
                    "evidence_refs": ["review-facts"],
                }
            },
            "steps": [
                {
                    "step_id": "open_fix",
                    "action": "open_trajectory_fix_workbench",
                    "variant": "durable_human_handoff",
                    "arguments": {},
                    "depends_on": [],
                    "failure_policy": "stop",
                    "decision_refs": ["review"],
                },
                {
                    "step_id": "validate_review",
                    "action": "validate_trajectory_review_outcome",
                    "variant": "approved_or_terminal",
                    "arguments": {},
                    "depends_on": ["open_fix"],
                    "failure_policy": "stop",
                    "decision_refs": ["review"],
                },
            ],
        }
    )


def test_linked_fix_plan_waits_resumes_and_completes_from_authoritative_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(database)
    created = task_store.create_task_attempt(
        request="Fix the linked trajectory",
        target="trajectory_review",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=True,
        web_session_id="web-owner",
        agentscope_session_id="navigation-owner",
        requested_outcome="trajectory_fix",
    ).task
    task = task_store.get_task(created.task_id)
    assert task is not None
    plan_store = SqliteNavigationPlanRepository(database)
    plan = plan_store.activate(
        task,
        "trajectory_review",
        1,
        _trajectory_review_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="navigation-owner",
    )
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    review_service = _ReviewOutcomeService()
    gateway = AnnotationNavigationGateway(
        service=review_service,
        navigation_db_path=database,
    )

    def tools():
        durable = task_store.get_task(task.task_id)
        assert durable is not None
        return {
            tool.name: tool
            for tool in build_plan_bound_execution_tools(
                task=durable,
                plan_store=plan_store,
                evidence_store=evidence_store,
                settings=NavigationSettings(
                    vladatasets_root=tmp_path / "datasets",
                    processing_root=tmp_path / "processing",
                ),
                dry_run=True,
                cancellation=None,
                web_session_id="web-owner",
                agentscope_session_id="navigation-owner",
                annotation_gateway=gateway,
            )
        }

    initial_tools = tools()
    assert initial_tools["open_trajectory_fix_workbench_tool"].is_read_only is False
    assert (
        initial_tools["validate_trajectory_review_outcome_tool"].is_read_only
        is False
    )

    opened = asyncio.run(
        _tool_payload(
            initial_tools["open_trajectory_fix_workbench_tool"],
            plan_id=plan.plan_id,
            step_id="open_fix",
        )
    )
    assert opened["status"] == "waiting_user"
    assert "review_11111111111111111111111111111111" not in json.dumps(opened)
    assert task_store.get_task(task.task_id).status.value == "waiting_user"

    assert complete_annotation_workflow_step(
        plan_store=plan_store,
        evidence_store=evidence_store,
        navigation_task_id=task.task_id,
        action="open_trajectory_fix_workbench",
        status="fix_revision_submitted",
    )
    assert task_store.get_task(task.task_id).status.value == "active"

    validation = asyncio.run(
        _tool_payload(
            tools()["validate_trajectory_review_outcome_tool"],
            plan_id=plan.plan_id,
            step_id="validate_review",
        )
    )
    assert validation["status"] == "waiting_user"
    assert validation["details"]["counts"]["returned"] == 1
    assert task_store.get_task(task.task_id).status.value == "waiting_user"

    # A later review handoff is an at-least-once wakeup. The already-completed
    # workbench step is not rerun, while the waiting validation becomes active.
    assert complete_annotation_workflow_step(
        plan_store=plan_store,
        evidence_store=evidence_store,
        navigation_task_id=task.task_id,
        action="open_trajectory_fix_workbench",
        status="review_completed",
    )
    assert task_store.get_task(task.task_id).status.value == "active"
    review_service.outcome = {
        "status": "completed",
        "review_count": 2,
        "counts": {
            "pending": 0,
            "in_progress": 0,
            "returned": 0,
            "approved": 1,
            "discarded": 1,
        },
        "all_terminal": True,
        "ignored_review_ref": "review_" + "1" * 32,
        "ignored_private_path": "/private/review.json",
    }
    completed = asyncio.run(
        _tool_payload(
            tools()["validate_trajectory_review_outcome_tool"],
            plan_id=plan.plan_id,
            step_id="validate_review",
        )
    )

    assert completed["status"] == "completed"
    assert task_store.get_task(task.task_id).status.value == "completed"
    outcome = task_store.get_task_outcome(task.task_id)
    assert outcome is not None
    assert outcome.completion_outcome == "trajectory_review_completed"
    serialized = json.dumps(completed)
    assert "review_11111111111111111111111111111111" not in serialized
    assert "/private/" not in serialized


class _ExplodingReviewGateway:
    @staticmethod
    def begin_trajectory_review_from_plan(**_kwargs):
        raise OSError(
            "/private/reviews/review_"
            + "9" * 32
            + "/trajectory.json is unavailable"
        )


def test_review_gateway_exception_is_not_returned_to_model(
    tmp_path: Path,
) -> None:
    database = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(database)
    created = task_store.create_task_attempt(
        request="Fix the linked trajectory",
        target="trajectory_review",
        date="20270623",
        segments=["20260623_145550"],
        scene_mode=None,
        dry_run=True,
        web_session_id="web-owner",
        agentscope_session_id="navigation-owner",
        requested_outcome="trajectory_fix",
    ).task
    task = task_store.get_task(created.task_id)
    assert task is not None
    plan_store = SqliteNavigationPlanRepository(database)
    plan = plan_store.activate(
        task,
        "trajectory_review",
        1,
        _trajectory_review_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="navigation-owner",
    )
    evidence_store = FileNavigationEvidenceStore(tmp_path / "evidence")
    tools = {
        tool.name: tool
        for tool in build_plan_bound_execution_tools(
            task=task_store.get_task(task.task_id),
            plan_store=plan_store,
            evidence_store=evidence_store,
            settings=NavigationSettings(
                vladatasets_root=tmp_path / "datasets",
                processing_root=tmp_path / "processing",
            ),
            dry_run=True,
            cancellation=None,
            web_session_id="web-owner",
            agentscope_session_id="navigation-owner",
            annotation_gateway=_ExplodingReviewGateway(),
        )
    }

    result = asyncio.run(
        _tool_payload(
            tools["open_trajectory_fix_workbench_tool"],
            plan_id=plan.plan_id,
            step_id="open_fix",
        )
    )

    assert result["ok"] is False
    assert result["error_type"] == "trajectory_review_state_unavailable"
    serialized = json.dumps(result)
    assert "/private/" not in serialized
    assert "review_" + "9" * 32 not in serialized


def _annotation_tracking_plan() -> FinishProcessingPlanInput:
    return FinishProcessingPlanInput.model_validate(
        {
            "decisions": {
                "localization": {
                    "source": "odom",
                    "conversion": "odom_to_ins",
                    "reason": "Observed odometry.",
                    "evidence_refs": ["localization-facts"],
                },
                "gridmap": {
                    "source": "existing_gridmap",
                    "reason": "Observed synchronized gridmap artifacts.",
                    "evidence_refs": ["gridmap-facts"],
                },
                "calibration": {
                    "mode": "selected_profile",
                    "selected_sensor_source": "20260529_go2w",
                    "requires_user_confirmation": False,
                    "reason": "The user selected the processing profile.",
                    "evidence_refs": ["calibration-facts"],
                },
            },
            "steps": [
                {
                    "step_id": "annotation",
                    "action": "run_annotation_tracking_workflow",
                    "variant": "durable_web_handoff",
                    "arguments": {},
                    "depends_on": [],
                    "failure_policy": "stop",
                    "decision_refs": ["localization", "calibration"],
                }
            ],
        }
    )


class _ExplodingAnnotationGateway:
    @staticmethod
    def begin_annotation_from_plan(**_kwargs):
        raise OSError(
            "/private/annotation/job_"
            + "6" * 32
            + "/first-frame.jpg is unavailable"
        )


class _RuntimeUnavailableAnnotationGateway:
    @staticmethod
    def begin_annotation_from_plan(**_kwargs):
        raise AnnotationConflictError(
            "annotation_runtime_unavailable",
            "/private/runtime/config is incomplete",
            current={
                "capabilities": {
                    "available": False,
                    "reason": {
                        "code": "processing_runtime_not_configured",
                        "message": "/private/runtime/config is incomplete",
                        "error_ref": "annotation_error_" + "7" * 32,
                    },
                },
                "job_ref": "job_" + "8" * 32,
            },
        )


def test_annotation_gateway_exception_is_not_returned_to_model(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(database)
    task = task_store.create_task_attempt(
        request="Automatically annotate the synchronized data",
        target="20270605",
        date="20270605",
        segments=["20260605_160904"],
        scene_mode=None,
        dry_run=True,
        web_session_id="web-owner",
        agentscope_session_id="navigation-owner",
        requested_outcome="postprocessing",
    ).task
    plan_store = SqliteNavigationPlanRepository(database)
    plan = plan_store.activate(
        task,
        "finish_processing",
        1,
        _annotation_tracking_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="navigation-owner",
    )
    tools = {
        tool.name: tool
        for tool in build_plan_bound_execution_tools(
            task=task_store.get_task(task.task_id),
            plan_store=plan_store,
            evidence_store=FileNavigationEvidenceStore(tmp_path / "evidence"),
            settings=NavigationSettings(
                vladatasets_root=tmp_path / "datasets",
                processing_root=tmp_path / "processing",
            ),
            dry_run=True,
            cancellation=None,
            web_session_id="web-owner",
            agentscope_session_id="navigation-owner",
            annotation_gateway=_ExplodingAnnotationGateway(),
        )
    }

    result = asyncio.run(
        _tool_payload(
            tools["run_annotation_tracking_workflow_tool"],
            plan_id=plan.plan_id,
            step_id="annotation",
        )
    )

    assert result["ok"] is False
    assert result["error_type"] == "annotation_workflow_start_failed"
    assert result["next_action"] == "operator_recovery_required"
    assert str(result["error_ref"]).startswith("annotation_error_")
    serialized = json.dumps(result)
    assert "/private/" not in serialized
    assert "job_" + "6" * 32 not in serialized
    assert any(
        result["error_ref"] in record.message
        and "exception_type=OSError" in record.message
        for record in caplog.records
    )
    assert "/private/" not in caplog.text


def test_annotation_runtime_deployment_error_is_projected_for_operator_recovery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(database)
    task = task_store.create_task_attempt(
        request="Automatically annotate the synchronized data",
        target="20270605",
        date="20270605",
        segments=["20260605_160904"],
        scene_mode=None,
        dry_run=True,
        web_session_id="web-owner",
        agentscope_session_id="navigation-owner",
        requested_outcome="postprocessing",
    ).task
    plan_store = SqliteNavigationPlanRepository(database)
    plan = plan_store.activate(
        task,
        "finish_processing",
        1,
        _annotation_tracking_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="navigation-owner",
    )
    tools = {
        tool.name: tool
        for tool in build_plan_bound_execution_tools(
            task=task_store.get_task(task.task_id),
            plan_store=plan_store,
            evidence_store=FileNavigationEvidenceStore(tmp_path / "evidence"),
            settings=NavigationSettings(
                vladatasets_root=tmp_path / "datasets",
                processing_root=tmp_path / "processing",
            ),
            dry_run=True,
            cancellation=None,
            web_session_id="web-owner",
            agentscope_session_id="navigation-owner",
            annotation_gateway=_RuntimeUnavailableAnnotationGateway(),
        )
    }

    result = asyncio.run(
        _tool_payload(
            tools["run_annotation_tracking_workflow_tool"],
            plan_id=plan.plan_id,
            step_id="annotation",
        )
    )

    assert result == {
        "ok": False,
        "error_type": "processing_runtime_not_configured",
        "message": (
            "The annotation processing runtime deployment is incomplete. "
            "An operator must complete its configuration before processing "
            "can continue."
        ),
        "next_action": "operator_recovery_required",
        "error_ref": "annotation_error_" + "7" * 32,
    }
    serialized = json.dumps(result)
    assert "/private/" not in serialized
    assert "job_" + "8" * 32 not in serialized
    assert plan.plan_id not in serialized
    assert "annotation" not in result.get("next_action", "")


def test_tracking_completion_finalizes_plan_step_and_wakes_navigation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = {
        "handoff_id": 2,
        "handoff_ref": "handoff_" + "2" * 32,
        "kind": "tracking_completed",
        "job_ref": "job_" + "3" * 32,
        "job_revision": 9,
        "navigation_task_ref": "nav-task",
        "payload": {},
    }
    service = _HandoffService(handoff)
    runtime = _WakeRuntime()
    finalized: list[dict[str, object]] = []

    def _complete(**kwargs) -> bool:
        finalized.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "vla_data_juicer_agents.annotation.workflow_coordinator."
        "complete_annotation_workflow_step",
        _complete,
    )
    coordinator = AnnotationWorkflowCoordinator(
        service=service,
        agentscope_runtime=runtime,
        navigation_workspace_root=tmp_path / "navigation",
    )

    assert asyncio.run(coordinator.process_once()) is True

    assert finalized[0]["navigation_task_id"] == "nav-task"
    assert finalized[0]["action"] == "run_annotation_tracking_workflow"
    assert finalized[0]["status"] == "tracked"
    assert runtime.wakes == [
        {
            "task_id": "nav-task",
            "reason": "initial_annotation_tracking_completed",
        }
    ]
    assert service.store.completed[-1]["success"] is True


@pytest.mark.parametrize(
    "kind,payload",
    [
        (
            "fix_revision_submitted",
            {"fix_revision_ref": "fix_revision_" + "1" * 32},
        ),
        ("review_returned", {"decision": "returned"}),
        ("review_completed", {"decision": "approved"}),
        ("review_completed", {"decision": "discarded"}),
    ],
)
def test_review_handoffs_release_workbench_wait_and_wake_linked_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    payload: dict[str, str],
) -> None:
    handoff = {
        "handoff_id": 3,
        "handoff_ref": "handoff_" + "3" * 32,
        "kind": kind,
        "job_ref": "job_" + "4" * 32,
        "job_revision": 4,
        "navigation_task_ref": "linked-fix-child",
        "payload": payload,
    }
    service = _HandoffService(handoff)
    runtime = _WakeRuntime()
    finalized: list[dict[str, object]] = []

    def _complete(**kwargs) -> bool:
        finalized.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "vla_data_juicer_agents.annotation.workflow_coordinator."
        "complete_annotation_workflow_step",
        _complete,
    )
    coordinator = AnnotationWorkflowCoordinator(
        service=service,
        agentscope_runtime=runtime,
        navigation_workspace_root=tmp_path / "navigation",
    )

    assert asyncio.run(coordinator.process_once()) is True

    assert finalized[0]["navigation_task_id"] == "linked-fix-child"
    assert finalized[0]["action"] == "open_trajectory_fix_workbench"
    assert finalized[0]["status"] == kind
    assert runtime.wakes == [
        {
            "task_id": "linked-fix-child",
            "reason": "trajectory_review_updated",
        }
    ]
    assert service.store.completed[-1]["success"] is True


def test_review_handoff_before_plan_is_delivered_after_durable_child_wakeup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = {
        "handoff_id": 4,
        "handoff_ref": "handoff_" + "4" * 32,
        "kind": "review_completed",
        "job_ref": "job_" + "6" * 32,
        "job_revision": 4,
        "navigation_task_ref": "linked-fix-child",
        "payload": {"decision": "discarded"},
    }
    service = _HandoffService(handoff)
    runtime = _WakeRuntime()
    monkeypatch.setattr(
        "vla_data_juicer_agents.annotation.workflow_coordinator."
        "complete_annotation_workflow_step",
        lambda **_kwargs: False,
    )
    coordinator = AnnotationWorkflowCoordinator(
        service=service,
        agentscope_runtime=runtime,
        navigation_workspace_root=tmp_path / "navigation",
    )

    assert asyncio.run(coordinator.process_once()) is True

    assert runtime.wakes == [
        {
            "task_id": "linked-fix-child",
            "reason": "trajectory_review_updated",
        }
    ]
    assert service.store.completed[-1]["success"] is True


def _seed_linked_reviews(store: AnnotationStore, tmp_path: Path) -> tuple[str, list[str]]:
    job_ref = "job_" + "5" * 32
    created = store.create_job(
        job_ref=job_ref,
        dataset_date="20270623",
        source_clips=["20260623_145550"],
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "20260529_go2w",
            "content_sha256": PROFILE_SHA,
        },
        snapshot_dir=tmp_path / "processing-calibration",
        snapshot_files=[],
        reserved_bytes=1,
        idempotency_key="seed-linked-review-job",
    )
    review_refs: list[str] = []
    timestamp = "2026-07-28T00:00:00+00:00"
    with store._write() as connection:
        job_id = int(
            connection.execute(
                "SELECT id FROM annotation_jobs WHERE job_ref = ?",
                (created["job_ref"],),
            ).fetchone()["id"]
        )
        connection.execute(
            """
            UPDATE annotation_jobs
            SET status = 'annotated', state_revision = 1
            WHERE id = ?
            """,
            (job_id,),
        )
        for ordinal in (1, 2):
            segment_ref = f"segment_{ordinal:032x}"
            segment = connection.execute(
                """
                INSERT INTO annotation_segments (
                    segment_ref, job_id, ordinal, source_clip, status,
                    state_revision, draft_revision, submitted_revision,
                    private_segment_key, private_segment_root,
                    private_first_frame_path, first_frame_width,
                    first_frame_height, first_frame_sha256, first_frame_etag,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'annotated', 1, 1, 1, ?, ?, ?,
                          1920, 1536, ?, ?, ?, ?)
                """,
                (
                    segment_ref,
                    job_id,
                    ordinal,
                    "20260623_145550",
                    f"private-segment-{ordinal}",
                    str(tmp_path / f"segment-{ordinal}"),
                    str(tmp_path / f"segment-{ordinal}" / "first.jpg"),
                    str(ordinal) * 64,
                    str(ordinal) * 64,
                    timestamp,
                    timestamp,
                ),
            )
            trajectory = connection.execute(
                """
                INSERT INTO trajectory_revisions (
                    revision_ref, job_id, segment_id, revision_number,
                    content_sha256, private_artifact_path,
                    private_compatibility_path, artifact_sha256,
                    private_state_json, artifact_manifest_ref, created_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    f"trajectory_revision_{ordinal:032x}",
                    job_id,
                    int(segment.lastrowid),
                    "a" * 64,
                    str(tmp_path / f"trajectory-{ordinal}"),
                    str(tmp_path / f"compatibility-{ordinal}"),
                    "b" * 64,
                    f"artifact_manifest_{ordinal:032x}",
                    timestamp,
                ),
            )
            review_ref = f"review_{ordinal:032x}"
            connection.execute(
                """
                INSERT INTO trajectory_review_tasks (
                    review_ref, trajectory_revision_id, status,
                    state_revision, created_at, updated_at
                ) VALUES (?, ?, 'pending', 0, ?, ?)
                """,
                (review_ref, int(trajectory.lastrowid), timestamp, timestamp),
            )
            review_refs.append(review_ref)
    for review_ref in review_refs:
        store.link_navigation_task(
            job_ref=job_ref,
            review_ref=review_ref,
            navigation_task_ref="linked-fix-child",
            parent_navigation_task_ref="completed-processing-parent",
            link_kind="trajectory_fix",
            idempotency_key=f"link:{review_ref}",
        )
    return job_ref, review_refs


def _mark_review_approved_and_published(
    store: AnnotationStore,
    *,
    review_ref: str,
) -> None:
    timestamp = "2026-07-28T00:01:00+00:00"
    with store._write() as connection:
        review = connection.execute(
            """
            SELECT r.id AS review_id, r.trajectory_revision_id, t.job_id
            FROM trajectory_review_tasks r
            JOIN trajectory_revisions t ON t.id = r.trajectory_revision_id
            WHERE r.review_ref = ?
            """,
            (review_ref,),
        ).fetchone()
        calibration_id = connection.execute(
            """
            INSERT INTO fix_calibration_snapshots (
                snapshot_ref, review_id, profile_ref, label,
                content_sha256, private_snapshot_dir, files_json,
                differs_from_processing, created_at
            ) VALUES (?, ?, '20260529_go2w', '20260529_go2w', ?, ?, '[]',
                      0, ?)
            """,
            (
                f"fix_calibration_{review['review_id']:032x}",
                review["review_id"],
                "c" * 64,
                f"/private/fix-calibration/{review['review_id']}",
                timestamp,
            ),
        ).lastrowid
        fix_attempt = connection.execute(
            """
            SELECT COALESCE(MAX(attempt), 0) + 1
            FROM runtime_runs
            WHERE job_id = ? AND kind = 'fix'
            """,
            (review["job_id"],),
        ).fetchone()[0]
        fix_run_id = connection.execute(
            """
            INSERT INTO runtime_runs (
                run_ref, job_id, kind, status, attempt,
                started_at, finished_at, created_at, updated_at
            ) VALUES (?, ?, 'fix', 'succeeded', ?, ?, ?, ?, ?)
            """,
            (
                f"run_fix_{review['review_id']:032x}",
                review["job_id"],
                fix_attempt,
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        fix_revision_id = connection.execute(
            """
            INSERT INTO fix_revisions (
                revision_ref, review_id, revision_number,
                calibration_snapshot_id, base_trajectory_revision_id,
                source_draft_revision, state_json, content_sha256,
                private_artifact_path, artifact_sha256,
                artifact_manifest_ref, runtime_run_id, created_at
            ) VALUES (?, ?, 1, ?, ?, 1, '{}', ?, ?, ?, ?, ?, ?)
            """,
            (
                f"fix_revision_{review['review_id']:032x}",
                review["review_id"],
                calibration_id,
                review["trajectory_revision_id"],
                "d" * 64,
                f"/private/fix/{review['review_id']}",
                "e" * 64,
                f"artifact_manifest_fix_{review['review_id']:032x}",
                fix_run_id,
                timestamp,
            ),
        ).lastrowid
        connection.execute(
            """
            UPDATE trajectory_review_tasks
            SET status = 'approved', approved_fix_revision_id = ?,
                state_revision = state_revision + 1, updated_at = ?
            WHERE id = ?
            """,
            (fix_revision_id, timestamp, review["review_id"]),
        )
        connection.execute(
            """
            INSERT INTO review_decisions (
                decision_ref, review_id, decision, fix_revision_id,
                actor_kind, deployment_instance, created_at
            ) VALUES (?, ?, 'approved', ?, 'manual_web', 'test', ?)
            """,
            (
                f"review_decision_{review['review_id']:032x}",
                review["review_id"],
                fix_revision_id,
                timestamp,
            ),
        )
        publication_attempt = connection.execute(
            """
            SELECT COALESCE(MAX(attempt), 0) + 1
            FROM runtime_runs
            WHERE job_id = ? AND kind = 'compatibility_publish'
            """,
            (review["job_id"],),
        ).fetchone()[0]
        publication_run_id = connection.execute(
            """
            INSERT INTO runtime_runs (
                run_ref, job_id, kind, status, attempt,
                started_at, finished_at, created_at, updated_at
            ) VALUES (?, ?, 'compatibility_publish', 'succeeded', ?,
                      ?, ?, ?, ?)
            """,
            (
                f"run_publication_{review['review_id']:032x}",
                review["job_id"],
                publication_attempt,
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        manifest_json = "{}"
        manifest_ref = (
            f"artifact_manifest_publication_{review['review_id']:032x}"
        )
        connection.execute(
            """
            INSERT INTO artifact_manifests (
                manifest_ref, job_id, run_id, stage, content_sha256,
                manifest_json, created_at
            ) VALUES (?, ?, ?, 'compatibility_publish', ?, ?, ?)
            """,
            (
                manifest_ref,
                review["job_id"],
                publication_run_id,
                hashlib.sha256(manifest_json.encode()).hexdigest(),
                manifest_json,
                timestamp,
            ),
        )
        publication_id = connection.execute(
            """
            INSERT INTO compatibility_publications (
                publication_ref, review_id, fix_revision_id, attempt,
                status, content_sha256, private_artifact_path,
                artifact_manifest_ref, created_at
            ) VALUES (?, ?, ?, 1, 'succeeded', ?, ?, ?, ?)
            """,
            (
                f"publication_{review['review_id']:032x}",
                review["review_id"],
                fix_revision_id,
                "d" * 64,
                f"/private/published/{review['review_id']}.json",
                manifest_ref,
                timestamp,
            ),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO runtime_run_publication_links (
                run_id, publication_id, review_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                publication_run_id,
                publication_id,
                review["review_id"],
                timestamp,
            ),
        )


class _StoreReviewService:
    def __init__(self, store: AnnotationStore) -> None:
        self.store = store

    def resolve_navigation_review_outcome(self, **kwargs):
        return self.store.resolve_navigation_review_outcome(**kwargs)


def test_review_event_end_to_end_resumes_linked_child_and_validates_all_reviews(
    tmp_path: Path,
) -> None:
    navigation_root = tmp_path / "navigation"
    navigation_root.mkdir()
    database = navigation_root / "navigation-tasks.sqlite"
    task_store = SqliteNavigationTaskStore(database)
    task = task_store.create_task_attempt(
        task_id="linked-fix-child",
        request="Fix all linked trajectories",
        target="trajectory_review",
        date="20270623",
        segments=None,
        scene_mode=None,
        dry_run=True,
        web_session_id="web-owner",
        agentscope_session_id="navigation-owner",
        requested_outcome="trajectory_fix",
    ).task
    plan_store = SqliteNavigationPlanRepository(database)
    plan = plan_store.activate(
        task,
        "trajectory_review",
        1,
        _trajectory_review_plan(),
        expected_web_session_id="web-owner",
        expected_agentscope_session_id="navigation-owner",
    )
    evidence_store = FileNavigationEvidenceStore(
        navigation_root / "navigation-evidence"
    )
    annotation_store = AnnotationStore(tmp_path / "annotation.sqlite")
    job_ref, review_refs = _seed_linked_reviews(annotation_store, tmp_path)
    service = _StoreReviewService(annotation_store)
    gateway = AnnotationNavigationGateway(
        service=service,
        navigation_db_path=database,
    )

    def tools():
        durable = task_store.get_task(task.task_id)
        assert durable is not None
        return {
            tool.name: tool
            for tool in build_plan_bound_execution_tools(
                task=durable,
                plan_store=plan_store,
                evidence_store=evidence_store,
                settings=NavigationSettings(
                    vladatasets_root=tmp_path / "datasets",
                    processing_root=tmp_path / "processing",
                ),
                dry_run=True,
                cancellation=None,
                web_session_id="web-owner",
                agentscope_session_id="navigation-owner",
                annotation_gateway=gateway,
            )
        }

    opened = asyncio.run(
        _tool_payload(
            tools()["open_trajectory_fix_workbench_tool"],
            plan_id=plan.plan_id,
            step_id="open_fix",
        )
    )
    assert opened["status"] == "waiting_user"

    with annotation_store._write() as connection:
        connection.execute(
            """
            UPDATE trajectory_review_tasks
            SET status = 'returned', state_revision = state_revision + 1
            WHERE review_ref = ?
            """,
            (review_refs[0],),
        )
    returned_handoff = annotation_store.create_workflow_handoff(
        job_ref=job_ref,
        review_ref=review_refs[0],
        kind="review_returned",
        payload={"decision": "returned"},
        idempotency_key="e2e-review-returned",
    )
    assert annotation_store.create_workflow_handoff(
        job_ref=job_ref,
        review_ref=review_refs[0],
        kind="review_returned",
        payload={"decision": "returned"},
        idempotency_key="e2e-review-returned",
    ) == returned_handoff

    runtime = _WakeRuntime()
    coordinator = AnnotationWorkflowCoordinator(
        service=service,
        agentscope_runtime=runtime,
        navigation_workspace_root=navigation_root,
    )
    assert asyncio.run(coordinator.process_once()) is True
    assert runtime.wakes[-1] == {
        "task_id": task.task_id,
        "reason": "trajectory_review_updated",
    }
    assert task_store.get_task(task.task_id).status.value == "active"

    waiting = asyncio.run(
        _tool_payload(
            tools()["validate_trajectory_review_outcome_tool"],
            plan_id=plan.plan_id,
            step_id="validate_review",
        )
    )
    assert waiting["status"] == "waiting_user"
    assert waiting["details"]["review_count"] == 2
    assert waiting["details"]["counts"]["returned"] == 1

    _mark_review_approved_and_published(
        annotation_store,
        review_ref=review_refs[0],
    )
    with annotation_store._write() as connection:
        connection.execute(
            """
            UPDATE trajectory_review_tasks
            SET status = 'discarded', state_revision = state_revision + 1
            WHERE review_ref = ?
            """,
            (review_refs[1],),
        )
    annotation_store.create_workflow_handoff(
        job_ref=job_ref,
        review_ref=review_refs[0],
        kind="review_completed",
        payload={"decision": "approved"},
        idempotency_key="e2e-review-completed",
    )
    assert asyncio.run(coordinator.process_once()) is True
    assert task_store.get_task(task.task_id).status.value == "active"

    completed = asyncio.run(
        _tool_payload(
            tools()["validate_trajectory_review_outcome_tool"],
            plan_id=plan.plan_id,
            step_id="validate_review",
        )
    )
    assert completed["status"] == "completed"
    assert annotation_store.resolve_navigation_review_outcome(
        navigation_task_ref=task.task_id
    )["counts"] == {
        "pending": 0,
        "in_progress": 0,
        "returned": 0,
        "approved": 1,
        "discarded": 1,
    }
    assert task_store.get_task(task.task_id).status.value == "completed"
    outcome = task_store.get_task_outcome(task.task_id)
    assert outcome is not None
    assert outcome.completion_outcome == "trajectory_review_completed"


def test_store_aggregates_multiple_linked_reviews_and_retries_handoffs(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    job_ref, review_refs = _seed_linked_reviews(store, tmp_path)

    initial = store.resolve_navigation_review_outcome(
        navigation_task_ref="linked-fix-child"
    )
    assert initial == {
        "status": "pending",
        "review_count": 2,
        "counts": {
            "pending": 2,
            "in_progress": 0,
            "returned": 0,
            "approved": 0,
            "discarded": 0,
        },
        "all_terminal": False,
    }

    with store._write() as connection:
        connection.execute(
            """
            UPDATE trajectory_review_tasks
            SET status = 'returned', state_revision = state_revision + 1
            WHERE review_ref = ?
            """,
            (review_refs[0],),
        )
    _mark_review_approved_and_published(
        store,
        review_ref=review_refs[1],
    )
    returned = store.resolve_navigation_review_outcome(
        navigation_task_ref="linked-fix-child"
    )
    assert returned["status"] == "returned"
    assert returned["counts"]["returned"] == 1
    assert returned["counts"]["approved"] == 1
    assert returned["all_terminal"] is False

    handoff = store.create_workflow_handoff(
        job_ref=job_ref,
        review_ref=review_refs[0],
        kind="review_returned",
        payload={"decision": "returned"},
        idempotency_key="review-returned-handoff",
    )
    first_claim = store.claim_workflow_handoff_delivery(worker_id="worker-a")
    assert first_claim is not None
    assert first_claim["handoff_ref"] == handoff["handoff_ref"]
    assert first_claim["navigation_task_ref"] == "linked-fix-child"
    assert first_claim["attempt"] == 1
    store.complete_workflow_handoff_delivery(
        handoff_id=first_claim["handoff_id"],
        worker_id="worker-a",
        success=False,
        error="simulated_crash",
    )
    retry = store.claim_workflow_handoff_delivery(worker_id="worker-b")
    assert retry is not None
    assert retry["handoff_id"] == first_claim["handoff_id"]
    assert retry["attempt"] == 2
    store.complete_workflow_handoff_delivery(
        handoff_id=retry["handoff_id"],
        worker_id="worker-b",
        success=True,
    )
    assert store.claim_workflow_handoff_delivery(worker_id="worker-c") is None

    with store._write() as connection:
        connection.execute(
            """
            UPDATE trajectory_review_tasks
            SET status = 'discarded', state_revision = state_revision + 1
            WHERE review_ref = ?
            """,
            (review_refs[0],),
        )
    terminal = store.resolve_navigation_review_outcome(
        navigation_task_ref="linked-fix-child"
    )
    assert terminal["status"] == "completed"
    assert terminal["counts"]["approved"] == 1
    assert terminal["counts"]["discarded"] == 1
    assert terminal["all_terminal"] is True

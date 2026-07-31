from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vla_data_juicer_agents.annotation.application import (
    AnnotationApplicationService,
)
from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationNotFoundError,
    AnnotationValidationError,
    CreateAnnotationJobRequest,
    PostprocessingSpecInput,
)
from vla_data_juicer_agents.navigation.annotation_gateway import (
    NavigationAnnotationGateway,
)
from vla_data_juicer_agents.navigation.observation_store import (
    SqliteNavigationObservationStore,
)
from vla_data_juicer_agents.navigation.observation_models import (
    CalibrationInventoryObservation,
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


_SAFE_CLIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AnnotationNavigationGateway(NavigationAnnotationGateway):
    """Plan-bound, private adapter into the Annotation Application Service.

    The adapter owns every Annotation ref and filesystem lookup. Navigation
    tools pass only their durable task/Plan/step identity and receive bounded
    business state; no ref, path, script name, or command reaches the model.
    """

    def __init__(
        self,
        *,
        service: AnnotationApplicationService,
        navigation_db_path: Path,
    ) -> None:
        self.service = service
        self.navigation_db_path = Path(navigation_db_path)
        self.task_store = SqliteNavigationTaskStore(
            self.navigation_db_path,
            initialize=False,
        )
        self.plan_store = SqliteNavigationPlanRepository(
            self.navigation_db_path,
            initialize=False,
        )
        self.observation_store = SqliteNavigationObservationStore(
            self.navigation_db_path,
            initialize=False,
        )

    def get_processing_facts(
        self,
        *,
        dataset_date: str,
        source_clips: Sequence[str] | None,
        navigation_task_id: str | None = None,
    ) -> Mapping[str, Any]:
        clips: list[str]
        task = (
            self.task_store.get_task(navigation_task_id)
            if navigation_task_id is not None
            else None
        )
        if navigation_task_id is not None and task is None:
            raise AnnotationValidationError(
                "invalid_navigation_task_binding",
                "The Annotation facts request is not bound to a durable task.",
            )
        if task is not None and task.target == "trajectory_review":
            binding = self.service.resolve_navigation_task_binding(
                navigation_task_ref=navigation_task_id,
                link_kind="trajectory_fix",
            )
            if task.date != dataset_date or str(binding["dataset_date"]) != dataset_date:
                raise AnnotationValidationError(
                    "annotation_scope_mismatch",
                    "The linked Annotation scope does not match this task.",
                )
            clips = [str(value) for value in binding["source_clips"]]
            if not clips:
                raise AnnotationValidationError(
                    "empty_annotation_scope",
                    "The linked Annotation scope is empty.",
                )
        else:
            clips = self._resolve_source_clips(
                dataset_date=dataset_date,
                requested=source_clips,
            )
        raw = self.service.get_processing_facts(
            dataset_date=dataset_date,
            source_clips=clips,
        )
        if not raw.get("exists"):
            return {
                "kind": "annotation_job_facts",
                "job_status": "missing",
                "segment_count": 0,
                "tracked_count": 0,
                "skipped_count": 0,
                "annotated_count": 0,
                "ready_for_postprocessing": False,
                "ready_for_trajectory_review": False,
                "processing_calibration_snapshot_available": False,
                "reviews": {},
            }
        counts = dict(raw.get("segment_counts") or {})
        reviews = dict(raw.get("review_counts") or {})
        status = str(raw.get("job_status") or "missing")
        return {
            "kind": "annotation_job_facts",
            "job_status": status,
            "segment_count": sum(int(value or 0) for value in counts.values()),
            "tracked_count": int(counts.get("tracked", 0) or 0),
            "skipped_count": int(counts.get("skipped", 0) or 0),
            "annotated_count": int(counts.get("annotated", 0) or 0),
            "ready_for_postprocessing": bool(
                raw.get("ready_for_postprocessing")
            ),
            "ready_for_trajectory_review": (
                status == "annotated"
                and sum(int(value or 0) for value in reviews.values()) > 0
            ),
            "processing_calibration_snapshot_available": True,
            "reviews": reviews,
        }

    def begin_annotation_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        task, plan = self._bound_finish_plan(
            navigation_task_id=navigation_task_id,
            plan_id=plan_id,
            step_id=step_id,
            expected_action="run_annotation_tracking_workflow",
        )
        clips = self._resolve_source_clips(
            dataset_date=task.date,
            requested=task.segments,
        )
        facts = self.service.get_processing_facts(
            dataset_date=task.date,
            source_clips=clips,
        )
        if facts.get("exists"):
            binding = self.service.resolve_scope_binding(
                dataset_date=task.date,
                source_clips=clips,
            )
            job_status = str(binding["job_status"])
        else:
            profile = self._processing_profile(plan)
            created = self.service.create_job(
                CreateAnnotationJobRequest(
                    dataset_date=task.date,
                    source_clips=clips,
                    calibration_profile_ref=profile["profile_ref"],
                    calibration_content_sha256=profile["content_sha256"],
                ),
                idempotency_key=(
                    f"datapilot:create_annotation_job:{navigation_task_id}:{plan_id}"
                ),
            )
            binding = self.service.resolve_scope_binding(
                dataset_date=task.date,
                source_clips=clips,
            )
            job_status = str(created["status"])
        self.service.link_navigation_task(
            job_ref=str(binding["job_ref"]),
            review_ref=None,
            navigation_task_ref=navigation_task_id,
            parent_navigation_task_ref=None,
            link_kind="processing",
            idempotency_key=(
                f"datapilot:link_processing:{navigation_task_id}"
            ),
        )
        if job_status in {"failed", "cancelled"}:
            raise AnnotationConflictError(
                "annotation_job_not_resumable",
                "The bound annotation workflow cannot be resumed.",
            )
        return {
            "ok": True,
            "workflow": "initial_annotation_and_tracking",
            "status": job_status,
            "source_clip_count": len(clips),
            "waiting_for_workbench": job_status
            in {
                "preparing",
                "waiting_initial_annotation",
            },
            "waiting_for_runtime": job_status == "tracking",
            "completed": job_status in {"tracked", "postprocessing", "annotated"},
        }

    def get_processing_calibration_options(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
    ) -> Sequence[Mapping[str, str]]:
        task = self.task_store.get_task(navigation_task_id)
        plan = self.plan_store.get(plan_id)
        if task is None or plan is None or plan.task_id != task.task_id:
            raise AnnotationValidationError(
                "invalid_navigation_task_binding",
                "The calibration inventory is not bound to this task.",
            )
        observation = self.observation_store.get(
            navigation_task_id,
            plan.observation_revision,
        )
        observed_sources = {
            source
            for payload in (observation.payloads if observation is not None else [])
            if isinstance(payload, CalibrationInventoryObservation)
            for source in payload.sensor_sources
        }
        selected = str(
            getattr(
                getattr(plan.plan.decisions, "calibration", None),
                "selected_sensor_source",
                "",
            )
            or ""
        )
        options: list[Mapping[str, str]] = []
        for profile in self.service.list_calibration_profiles(
            purpose="processing",
        )["profiles"]:
            profile_ref = str(profile["profile_ref"])
            matches = sorted(
                source
                for source in observed_sources
                if source == profile_ref or profile_ref in Path(source).parts
            )
            if len(matches) != 1:
                continue
            options.append(
                {
                    "profile_ref": profile_ref,
                    "label": str(profile["label"]),
                    "selected_sensor_source": matches[0],
                    "selected": "true" if matches[0] == selected else "false",
                }
            )
        if not options:
            raise AnnotationValidationError(
                "processing_calibration_unavailable",
                "No audited processing calibration is available for this Plan.",
            )
        return options

    def begin_postprocessing_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        task, plan = self._bound_finish_plan(
            navigation_task_id=navigation_task_id,
            plan_id=plan_id,
            step_id=step_id,
            expected_action="run_annotation_postprocessing_workflow",
        )
        clips = self._resolve_source_clips(
            dataset_date=task.date,
            requested=task.segments,
        )
        binding = self.service.resolve_scope_binding(
            dataset_date=task.date,
            source_clips=clips,
        )
        status = str(binding["job_status"])
        if status not in {"tracked", "postprocessing", "annotated"}:
            raise AnnotationConflictError(
                "annotation_job_not_ready_for_postprocessing",
                "The bound annotation workflow is not ready for postprocessing.",
            )
        self.service.link_navigation_task(
            job_ref=str(binding["job_ref"]),
            review_ref=None,
            navigation_task_ref=navigation_task_id,
            parent_navigation_task_ref=None,
            link_kind="processing",
            idempotency_key=(
                f"datapilot:link_processing:{navigation_task_id}"
            ),
        )
        if status == "annotated":
            return {
                "ok": True,
                "workflow": "postprocessing",
                "status": status,
                "source_clip_count": len(clips),
                "waiting_for_runtime": False,
                "completed": True,
            }
        if status == "tracked":
            spec = self._postprocessing_spec(
                navigation_task_id=navigation_task_id,
                plan=plan,
            )
            started = self.service.begin_postprocessing(
                str(binding["job_ref"]),
                int(binding["job_revision"]),
                spec,
                idempotency_key=(
                    f"datapilot:begin_postprocessing:{navigation_task_id}:{plan_id}"
                ),
                processing_navigation_task_ref=navigation_task_id,
            )
            status = str(started["status"])
        return {
            "ok": True,
            "workflow": "postprocessing",
            "status": status,
            "source_clip_count": len(clips),
            "waiting_for_runtime": status == "postprocessing",
            "completed": status == "annotated",
        }

    def begin_linked_fix(
        self,
        *,
        parent_navigation_task_id: str,
        child_navigation_task_id: str,
    ) -> Mapping[str, Any]:
        binding = self.service.resolve_navigation_task_binding(
            navigation_task_ref=parent_navigation_task_id,
            link_kind="processing",
        )
        if str(binding["job_status"]) != "annotated":
            raise AnnotationConflictError(
                "trajectory_review_not_ready",
                "The linked annotation workflow is not ready for trajectory review.",
            )
        review_refs = list(binding.get("review_refs") or [])
        if not review_refs:
            raise AnnotationNotFoundError("trajectory review scope not found")
        for review_ref in review_refs:
            self.service.link_navigation_task(
                job_ref=str(binding["job_ref"]),
                review_ref=str(review_ref),
                navigation_task_ref=child_navigation_task_id,
                parent_navigation_task_ref=parent_navigation_task_id,
                link_kind="trajectory_fix",
                idempotency_key=(
                    "datapilot:link_trajectory_fix:"
                    f"{child_navigation_task_id}:{review_ref}"
                ),
            )
        return {
            "ok": True,
            "workflow": "trajectory_fix",
            "status": "pending",
            "source_clip_count": len(binding["source_clips"]),
            "review_count": len(review_refs),
        }

    def begin_trajectory_review_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        self._bound_review_plan(
            navigation_task_id=navigation_task_id,
            plan_id=plan_id,
            step_id=step_id,
            expected_action="open_trajectory_fix_workbench",
        )
        return self._review_outcome(navigation_task_id)

    def get_trajectory_review_outcome_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        self._bound_review_plan(
            navigation_task_id=navigation_task_id,
            plan_id=plan_id,
            step_id=step_id,
            expected_action="validate_trajectory_review_outcome",
        )
        return self._review_outcome(navigation_task_id)

    def _review_outcome(self, navigation_task_id: str) -> dict[str, Any]:
        raw = self.service.resolve_navigation_review_outcome(
            navigation_task_ref=navigation_task_id,
        )
        raw_counts = dict(raw.get("counts") or {})
        counts = {
            status: int(raw_counts.get(status, 0) or 0)
            for status in (
                "pending",
                "in_progress",
                "returned",
                "approved",
                "discarded",
            )
        }
        review_count = int(raw.get("review_count", 0) or 0)
        if review_count < 1 or sum(counts.values()) != review_count:
            raise RuntimeError("invalid trajectory review aggregate")
        return {
            "ok": True,
            "workflow": "trajectory_review",
            "status": str(raw.get("status") or "pending"),
            "review_count": review_count,
            "counts": counts,
            "completed": bool(raw.get("all_terminal")),
        }

    def _bound_finish_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
        expected_action: str,
    ):
        task = self.task_store.get_task(navigation_task_id)
        plan = self.plan_store.get(plan_id)
        if (
            task is None
            or plan is None
            or plan.task_id != navigation_task_id
            or plan.phase != "finish_processing"
            or plan.status != "active"
            or not isinstance(plan.plan, FinishProcessingPlanInput)
        ):
            raise AnnotationValidationError(
                "invalid_navigation_plan_binding",
                "The annotation request is not bound to an active finish Plan.",
            )
        step = next(
            (candidate for candidate in plan.plan.steps if candidate.step_id == step_id),
            None,
        )
        if step is None or step.action != expected_action:
            raise AnnotationValidationError(
                "invalid_navigation_step_binding",
                "The annotation request is not bound to the expected Plan step.",
            )
        return task, plan

    def _bound_review_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
        expected_action: str,
    ):
        task = self.task_store.get_task(navigation_task_id)
        plan = self.plan_store.get(plan_id)
        if (
            task is None
            or plan is None
            or plan.task_id != navigation_task_id
            or task.target != "trajectory_review"
            or plan.phase != "trajectory_review"
            or plan.status != "active"
            or not isinstance(plan.plan, TrajectoryReviewPlanInput)
        ):
            raise AnnotationValidationError(
                "invalid_navigation_plan_binding",
                "The review request is not bound to an active trajectory-review Plan.",
            )
        step = next(
            (candidate for candidate in plan.plan.steps if candidate.step_id == step_id),
            None,
        )
        if step is None or step.action != expected_action:
            raise AnnotationValidationError(
                "invalid_navigation_step_binding",
                "The review request is not bound to the expected Plan step.",
            )
        return task, plan

    def _resolve_source_clips(
        self,
        *,
        dataset_date: str,
        requested: Sequence[str] | None,
    ) -> list[str]:
        if requested:
            clips = [str(value).strip() for value in requested]
            if any(not _SAFE_CLIP_RE.fullmatch(value) for value in clips):
                raise AnnotationValidationError(
                    "invalid_annotation_scope",
                    "The selected source clip scope is invalid.",
                )
            if len(clips) != len(set(clips)):
                raise AnnotationValidationError(
                    "invalid_annotation_scope",
                    "The selected source clip scope contains duplicates.",
                )
        else:
            date_root = self.service.clip_data_root / dataset_date
            try:
                date_stat = date_root.lstat()
            except OSError as exc:
                raise AnnotationValidationError(
                    "annotation_scope_unavailable",
                    "The synchronized dataset date is unavailable.",
                ) from exc
            if stat.S_ISLNK(date_stat.st_mode) or not stat.S_ISDIR(date_stat.st_mode):
                raise AnnotationValidationError(
                    "annotation_scope_unavailable",
                    "The synchronized dataset date is unavailable.",
                )
            clips = []
            for candidate in sorted(date_root.iterdir(), key=lambda path: path.name):
                if not _SAFE_CLIP_RE.fullmatch(candidate.name):
                    continue
                try:
                    candidate_stat = candidate.lstat()
                    sync_stat = (candidate / "sync_data").lstat()
                except OSError:
                    continue
                if (
                    stat.S_ISDIR(candidate_stat.st_mode)
                    and not stat.S_ISLNK(candidate_stat.st_mode)
                    and stat.S_ISDIR(sync_stat.st_mode)
                    and not stat.S_ISLNK(sync_stat.st_mode)
                ):
                    clips.append(candidate.name)
        if not clips:
            raise AnnotationValidationError(
                "empty_annotation_scope",
                "No synchronized source clips are available for this task.",
            )
        return clips

    def _processing_profile(self, plan: Any) -> dict[str, str]:
        decision = plan.plan.decisions.calibration
        if decision.mode == "annotation_snapshot":
            raise AnnotationValidationError(
                "annotation_snapshot_unavailable",
                "A missing annotation job cannot use an annotation snapshot.",
            )
        selected = self._confirmed_processing_calibration(plan)
        profiles = self.service.list_calibration_profiles(
            purpose="processing",
        )["profiles"]
        matches = [
            profile
            for profile in profiles
            if profile["profile_ref"] == selected
            or profile["profile_ref"] in Path(selected).parts
        ]
        if len(matches) != 1:
            raise AnnotationValidationError(
                "processing_calibration_not_selected",
                "The accepted Plan does not identify one processing calibration profile.",
            )
        return dict(matches[0])

    def _confirmed_processing_calibration(self, plan: Any) -> str:
        selected = str(plan.plan.decisions.calibration.selected_sensor_source or "")
        confirmation_step = next(
            (
                step
                for step in getattr(plan.plan, "steps", [])
                if step.action == "confirm_navigation_calibration_params"
            ),
            None,
        )
        if confirmation_step is None:
            return selected
        handoff = self.plan_store.get_human_decision_handoff(
            plan.plan_id,
            confirmation_step.step_id,
        )
        if handoff is None or handoff.decision.get("action") != "confirm":
            return selected
        confirmed = handoff.decision.get("selected_sensor_source")
        return confirmed if isinstance(confirmed, str) and confirmed else selected

    def _postprocessing_spec(
        self,
        *,
        navigation_task_id: str,
        plan: Any,
    ) -> PostprocessingSpecInput:
        decisions = plan.plan.decisions
        trajectory_variant = {
            "odom": "cjl_0525_with_gridmap",
            "ins": "cjl_with_gridmap",
        }[decisions.localization.source]
        observation = self.observation_store.get(
            navigation_task_id,
            plan.observation_revision,
        )
        if observation is None:
            raise AnnotationValidationError(
                "navigation_observation_unavailable",
                "The accepted Plan observation snapshot is unavailable.",
            )
        return PostprocessingSpecInput(
            localization_kind=decisions.localization.source,
            gridmap_decision={
                "existing_gridmap": "copy_existing_gridmap",
                "generated_from_pcd": "generate_from_pcd",
                "projection_ready": "skip_if_projection_ready",
            }[decisions.gridmap.source],
            trajectory_variant=trajectory_variant,
            plan_sha256=_canonical_sha256(plan.plan.model_dump(mode="json")),
            observations_sha256=_canonical_sha256(
                observation.model_dump(mode="json")
            ),
        )

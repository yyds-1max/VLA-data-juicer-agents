from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.annotation.application import (
    AnnotationApplicationService,
)
from vla_data_juicer_agents.annotation.models import (
    ExpectedJobRevisionRequest,
)
from vla_data_juicer_agents.navigation.evidence_store import (
    FileNavigationEvidenceStore,
)
from vla_data_juicer_agents.navigation.plan_execution import (
    complete_annotation_workflow_step,
    fail_annotation_workflow_step,
)
from vla_data_juicer_agents.navigation.plan_store import (
    SqliteNavigationPlanRepository,
)


logger = logging.getLogger(__name__)


class AnnotationWorkflowCoordinator:
    """Deliver immutable Annotation handoffs into durable Navigation state."""

    def __init__(
        self,
        *,
        service: AnnotationApplicationService,
        agentscope_runtime: Any,
        navigation_workspace_root: Path,
        poll_interval: float = 0.25,
    ) -> None:
        self.service = service
        self.agentscope_runtime = agentscope_runtime
        self.plan_store = SqliteNavigationPlanRepository(
            Path(navigation_workspace_root) / "navigation-tasks.sqlite",
            initialize=False,
        )
        self.evidence_store = FileNavigationEvidenceStore(
            Path(navigation_workspace_root) / "navigation-evidence"
        )
        self.poll_interval = poll_interval
        self.worker_id = f"annotation-handoff-{uuid4().hex}"
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            processed = False
            try:
                processed = await self.process_once()
                recover = getattr(
                    self.agentscope_runtime,
                    "recover_explicit_linked_fix_tasks_once",
                    None,
                )
                if callable(recover):
                    processed = bool(await recover()) or processed
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Annotation workflow coordinator iteration failed")
            if not processed:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.poll_interval,
                    )

    async def stop(self) -> None:
        self._stop.set()

    async def process_once(self) -> bool:
        handoff = await asyncio.to_thread(
            self.service.store.claim_workflow_handoff_delivery,
            worker_id=self.worker_id,
        )
        if handoff is None:
            return False
        try:
            kind = str(handoff["kind"])
            if kind == "initial_annotation_submitted":
                await asyncio.to_thread(
                    self.service.job_action,
                    "tracking",
                    str(handoff["job_ref"]),
                    ExpectedJobRevisionRequest(
                        expected_job_revision=int(handoff["job_revision"]),
                    ),
                    idempotency_key=(
                        f"datapilot:auto_tracking:{handoff['handoff_ref']}"
                    ),
                )
                publish_milestone = getattr(
                    self.agentscope_runtime,
                    "publish_navigation_workflow_milestone",
                    None,
                )
                if callable(publish_milestone):
                    await publish_milestone(
                        task_id=str(handoff["navigation_task_ref"]),
                        milestone_code="tracking_started",
                        origin_key=(
                            "annotation_workbench_milestone:"
                            f"{handoff['handoff_ref']}:tracking_started"
                        ),
                    )
            elif kind == "tracking_completed":
                await self._complete_and_wake(
                    handoff,
                    action="run_annotation_tracking_workflow",
                    status="tracked",
                )
            elif kind == "postprocessing_completed":
                await self._complete_and_wake(
                    handoff,
                    action="run_annotation_postprocessing_workflow",
                    status="annotated",
                )
            elif kind == "postprocessing_failed":
                await self._fail_and_wake(handoff)
            elif kind in {
                "fix_revision_submitted",
                "review_returned",
                "review_completed",
            }:
                await self._complete_and_wake(
                    handoff,
                    action="open_trajectory_fix_workbench",
                    status=kind,
                )
            else:
                raise RuntimeError(f"unsupported workflow handoff: {kind}")
        except Exception as exc:
            await asyncio.to_thread(
                self.service.store.complete_workflow_handoff_delivery,
                handoff_id=int(handoff["handoff_id"]),
                worker_id=self.worker_id,
                success=False,
                error=type(exc).__name__,
            )
            raise
        await asyncio.to_thread(
            self.service.store.complete_workflow_handoff_delivery,
            handoff_id=int(handoff["handoff_id"]),
            worker_id=self.worker_id,
            success=True,
        )
        return True

    async def _complete_and_wake(
        self,
        handoff: dict[str, Any],
        *,
        action: str,
        status: str,
    ) -> None:
        task_id = str(handoff["navigation_task_ref"])
        completed = await asyncio.to_thread(
            complete_annotation_workflow_step,
            plan_store=self.plan_store,
            evidence_store=self.evidence_store,
            navigation_task_id=task_id,
            action=action,
            status=status,
        )
        if not completed and action != "open_trajectory_fix_workbench":
            raise RuntimeError("Navigation workflow step could not be completed")
        wake = getattr(
            self.agentscope_runtime,
            "wake_navigation_task_from_workbench",
            None,
        )
        if not callable(wake):
            raise RuntimeError("Navigation workbench wakeup is unavailable")
        await wake(
            task_id=task_id,
            reason=(
                "initial_annotation_tracking_completed"
                if action == "run_annotation_tracking_workflow"
                else "postprocessing_completed"
                if action == "run_annotation_postprocessing_workflow"
                else "trajectory_review_updated"
            ),
            dispatch_idempotency_key=(
                "annotation_workbench_dispatch:"
                f"{handoff['handoff_ref']}:{task_id}:{handoff['kind']}"
            ),
        )

    async def _fail_and_wake(self, handoff: dict[str, Any]) -> None:
        task_id = str(handoff["navigation_task_ref"])
        payload = dict(handoff.get("payload") or {})
        failed = await asyncio.to_thread(
            fail_annotation_workflow_step,
            plan_store=self.plan_store,
            evidence_store=self.evidence_store,
            navigation_task_id=task_id,
            action="run_annotation_postprocessing_workflow",
            failure_code=str(
                payload.get("failure_code") or "annotation_runtime_failed"
            ),
            failure_ref=str(
                payload.get("error_ref") or "annotation_error_unavailable"
            ),
            retryable=bool(payload.get("retryable")),
        )
        if not failed:
            raise RuntimeError("Navigation workflow failure could not be recorded")
        wake = getattr(
            self.agentscope_runtime,
            "wake_navigation_task_from_workbench",
            None,
        )
        if not callable(wake):
            raise RuntimeError("Navigation workbench wakeup is unavailable")
        await wake(
            task_id=task_id,
            reason="postprocessing_failed",
            dispatch_idempotency_key=(
                "annotation_workbench_dispatch:"
                f"{handoff['handoff_ref']}:{task_id}:{handoff['kind']}"
            ),
        )

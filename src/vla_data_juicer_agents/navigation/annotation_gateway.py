from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


class AnnotationGatewayUnavailable(RuntimeError):
    """Raised when a plan requires Annotation Application Service facts or work."""


@runtime_checkable
class NavigationAnnotationGateway(Protocol):
    """Safe boundary from Navigation orchestration into Annotation.

    Implementations own all Annotation identifiers and filesystem bindings.  The
    Navigation model receives only bounded business facts and public outcomes.
    """

    def get_processing_facts(
        self,
        *,
        dataset_date: str,
        source_clips: Sequence[str] | None,
        navigation_task_id: str | None = None,
    ) -> Mapping[str, Any]:
        ...

    def get_processing_calibration_options(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
    ) -> Sequence[Mapping[str, str]]:
        """Return audited processing profiles and their private observed sources."""
        ...

    def begin_annotation_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        ...

    def begin_postprocessing_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        ...

    def begin_linked_fix(
        self,
        *,
        parent_navigation_task_id: str,
        child_navigation_task_id: str,
    ) -> Mapping[str, Any]:
        ...

    def begin_trajectory_review_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        ...

    def get_trajectory_review_outcome_from_plan(
        self,
        *,
        navigation_task_id: str,
        plan_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        ...

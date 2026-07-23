from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import Response

from vla_data_juicer_agents.annotation.application import AnnotationApplicationService
from vla_data_juicer_agents.annotation.models import (
    AnnotationConflictError,
    AnnotationNotFoundError,
    AnnotationValidationError,
    CreateAnnotationJobRequest,
    DraftRequest,
    ExpectedJobRevisionRequest,
    SegmentRevisionRequest,
    SkipRequest,
    SubmitRequest,
)


IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


def create_annotation_router(
    service: AnnotationApplicationService,
) -> APIRouter:
    router = APIRouter(prefix="/api/annotation", tags=["annotation"])

    @router.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return service.capabilities()

    @router.get("/calibration-profiles")
    def calibration_profiles(
        domain: str = Query(default="navigation"),
        purpose: str = Query(default="processing"),
    ) -> dict[str, Any]:
        if domain != "navigation" or purpose != "processing":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "unsupported_calibration_inventory",
                    "message": "Only navigation processing calibration is available in M1.",
                },
            )
        return service.list_calibration_profiles()

    @router.post("/jobs", status_code=201)
    def create_job(
        request: CreateAnnotationJobRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.create_job,
            request,
            idempotency_key=idempotency_key,
        )

    @router.get("/jobs")
    def list_jobs() -> dict[str, Any]:
        return service.list_jobs()

    @router.get("/jobs/{job_ref}")
    def get_job(job_ref: str) -> dict[str, Any]:
        return _translate(service.get_job, job_ref)

    @router.post("/jobs/{job_ref}/tracking")
    def start_tracking(
        job_ref: str,
        request: ExpectedJobRevisionRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.job_action,
            "tracking",
            job_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/jobs/{job_ref}/complete-no-processable-targets")
    def complete_no_processable_targets(
        job_ref: str,
        request: ExpectedJobRevisionRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.job_action,
            "complete_no_processable_targets",
            job_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/jobs/{job_ref}/cancel")
    def cancel_job(
        job_ref: str,
        request: ExpectedJobRevisionRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.job_action,
            "cancel",
            job_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/jobs/{job_ref}/retry")
    def retry_job(
        job_ref: str,
        request: ExpectedJobRevisionRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.job_action,
            "retry",
            job_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.get("/jobs/{job_ref}/segments/{segment_ref}")
    def get_segment(job_ref: str, segment_ref: str) -> dict[str, Any]:
        return _translate(service.get_segment, job_ref, segment_ref)

    @router.get("/jobs/{job_ref}/segments/{segment_ref}/first-frame")
    def first_frame(job_ref: str, segment_ref: str) -> Response:
        content, etag, media_type = _translate(
            service.resolve_first_frame,
            job_ref,
            segment_ref,
        )
        normalized_etag = etag if etag.startswith('"') else f'"{etag}"'
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "ETag": normalized_etag,
                "Cache-Control": "private, no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.put("/jobs/{job_ref}/segments/{segment_ref}/draft")
    def save_draft(
        job_ref: str,
        segment_ref: str,
        request: DraftRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.save_draft,
            job_ref,
            segment_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/jobs/{job_ref}/segments/{segment_ref}/submit")
    def submit_segment(
        job_ref: str,
        segment_ref: str,
        request: SubmitRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.submit,
            job_ref,
            segment_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/jobs/{job_ref}/segments/{segment_ref}/reopen")
    def reopen_segment(
        job_ref: str,
        segment_ref: str,
        request: SegmentRevisionRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.segment_action,
            "reopen",
            job_ref,
            segment_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/jobs/{job_ref}/segments/{segment_ref}/skip")
    def skip_segment(
        job_ref: str,
        segment_ref: str,
        request: SkipRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.segment_action,
            "skip",
            job_ref,
            segment_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/jobs/{job_ref}/segments/{segment_ref}/unskip")
    def unskip_segment(
        job_ref: str,
        segment_ref: str,
        request: SegmentRevisionRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.segment_action,
            "unskip",
            job_ref,
            segment_ref,
            request,
            idempotency_key=idempotency_key,
        )

    return router


def _translate(callable_: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return callable_(*args, **kwargs)
    except AnnotationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "annotation_not_found", "message": "Annotation resource not found."},
        ) from exc
    except AnnotationConflictError as exc:
        detail: dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if exc.current is not None:
            detail["current"] = exc.current
        raise HTTPException(status_code=409, detail=detail) from exc
    except AnnotationValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

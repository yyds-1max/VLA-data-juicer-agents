from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from vla_data_juicer_agents.annotation.application import AnnotationApplicationService
from vla_data_juicer_agents.annotation.models import (
    ApplyFixCommandRequest,
    AnnotationConflictError,
    AnnotationNotFoundError,
    AnnotationValidationError,
    ApproveReviewRequest,
    CreateFixRevisionRequest,
    CreateFixSessionRequest,
    CreateAnnotationJobRequest,
    DiscardReviewRequest,
    DraftRequest,
    ExpectedJobRevisionRequest,
    RetryPublicationRequest,
    ReturnReviewRequest,
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
        if domain != "navigation" or purpose not in {"processing", "fix"}:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "unsupported_calibration_inventory",
                    "message": "The requested calibration inventory is unavailable.",
                },
            )
        return _translate(service.list_calibration_profiles, purpose=purpose)

    @router.get("/events/cursor")
    def public_event_cursor() -> dict[str, int]:
        return service.public_event_cursor()

    @router.get("/events")
    async def public_events(
        request: Request,
        after_seq: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(
            default=None,
            alias="Last-Event-ID",
            max_length=32,
        ),
    ) -> StreamingResponse:
        cursor = after_seq
        if last_event_id is not None:
            try:
                resumed_cursor = int(last_event_id, 10)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative integer",
                ) from exc
            if resumed_cursor < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative integer",
                )
            cursor = max(cursor, resumed_cursor)

        async def stream():
            nonlocal cursor
            loop = asyncio.get_running_loop()
            heartbeat_at = loop.time() + 15.0
            yield "retry: 1000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                events = await asyncio.to_thread(
                    service.list_public_events_after,
                    after_seq=cursor,
                )
                for event in events:
                    cursor = int(event["seq"])
                    payload = json.dumps(
                        event,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    yield (
                        f"id: {cursor}\n"
                        "event: annotation\n"
                        f"data: {payload}\n\n"
                    )
                now = loop.time()
                if now >= heartbeat_at:
                    yield ": keepalive\n\n"
                    heartbeat_at = now + 15.0
                if not events:
                    await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

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

    @router.get("/reviews")
    def list_reviews(
        status: str | None = Query(default=None),
        dataset_date: str | None = Query(
            default=None,
            pattern=r"^[0-9]{8}$",
        ),
        source_clip: str | None = Query(default=None, min_length=1, max_length=200),
    ) -> dict[str, Any]:
        return _translate(
            service.list_reviews,
            status=status,
            dataset_date=dataset_date,
            source_clip=source_clip,
        )

    @router.get("/reviews/{review_ref}")
    def get_review(review_ref: str) -> dict[str, Any]:
        return _translate(service.get_review, review_ref)

    @router.get("/reviews/{review_ref}/evidence/trajectory")
    def get_review_trajectory_evidence(
        review_ref: str,
    ) -> dict[str, Any]:
        return _translate(
            service.get_review_trajectory_evidence,
            review_ref,
        )

    @router.get(
        "/reviews/{review_ref}/evidence/frames/{frame_index}/{kind}"
    )
    def get_review_evidence_file(
        review_ref: str,
        frame_index: int,
        kind: str,
    ) -> Response:
        if frame_index < 0 or kind not in {
            "camera",
            "gridmap",
            "projection",
        }:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "annotation_not_found",
                    "message": "Annotation evidence not found.",
                },
            )
        content, etag, media_type = _translate(
            service.resolve_review_evidence_file,
            review_ref,
            frame_index=frame_index,
            kind=kind,
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "ETag": f'"{etag}"',
                "Cache-Control": "private, no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/reviews/{review_ref}/fix-sessions", status_code=201)
    def create_fix_session(
        review_ref: str,
        request: CreateFixSessionRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.create_fix_session,
            review_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/reviews/{review_ref}/fix-commands")
    def apply_fix_command(
        review_ref: str,
        request: ApplyFixCommandRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.apply_fix_command,
            review_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/reviews/{review_ref}/fix-revisions", status_code=201)
    def create_fix_revision(
        review_ref: str,
        request: CreateFixRevisionRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.create_fix_revision,
            review_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/reviews/{review_ref}/approve")
    def approve_review(
        review_ref: str,
        request: ApproveReviewRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.approve_review,
            review_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/reviews/{review_ref}/return")
    def return_review(
        review_ref: str,
        request: ReturnReviewRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.return_review,
            review_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/reviews/{review_ref}/discard")
    def discard_review(
        review_ref: str,
        request: DiscardReviewRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.discard_review,
            review_ref,
            request,
            idempotency_key=idempotency_key,
        )

    @router.post("/reviews/{review_ref}/retry-publication")
    def retry_publication(
        review_ref: str,
        request: RetryPublicationRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _translate(
            service.retry_publication,
            review_ref,
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

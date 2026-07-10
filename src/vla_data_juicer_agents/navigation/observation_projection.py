from __future__ import annotations

from typing import Any

from vla_data_juicer_agents.navigation.context_budget import ensure_payload_within_limit
from vla_data_juicer_agents.navigation.observation_models import ObservationPayload


DEFAULT_OBSERVATION_PROJECTION_MAX_CHARS = 2_000
DEFAULT_PREVIEW_ITEMS = 5
PREVIEW_STRING_CHARS = 160


def preview_string(value: str) -> str:
    if len(value) <= PREVIEW_STRING_CHARS:
        return value
    return value[: PREVIEW_STRING_CHARS - 1] + "…"


def compact_observation_payload(
    payload: ObservationPayload,
    *,
    preview_items: int = DEFAULT_PREVIEW_ITEMS,
    max_chars: int = DEFAULT_OBSERVATION_PROJECTION_MAX_CHARS,
) -> dict[str, Any]:
    kind = payload.kind
    if kind == "raw_metadata":
        projection = {
            "kind": kind,
            "segment_count": len(payload.segments),
            "topic_count": len(payload.topics),
            "total_message_count": sum(topic.message_count for topic in payload.topics),
            "topics_preview": [
                {
                    "topic": preview_string(topic.topic),
                    "message_type": (
                        preview_string(topic.message_type)
                        if topic.message_type is not None
                        else None
                    ),
                    "message_count": topic.message_count,
                }
                for topic in payload.topics[:preview_items]
            ],
        }
    elif kind == "sensor_candidates":
        role_counts: dict[str, int] = {}
        for candidate in payload.candidates:
            role_counts[candidate.role] = role_counts.get(candidate.role, 0) + 1
        projection = {
            "kind": kind,
            "candidate_count": len(payload.candidates),
            "role_counts": dict(sorted(role_counts.items())),
            "candidates_preview": [
                {
                    "role": candidate.role,
                    "topic": preview_string(candidate.topic),
                    "message_type": (
                        preview_string(candidate.message_type)
                        if candidate.message_type is not None
                        else None
                    ),
                    "confidence": candidate.confidence,
                }
                for candidate in payload.candidates[:preview_items]
            ],
        }
    elif kind == "topic_candidates":
        projection = {
            "kind": kind,
            "available_topic_count": len(payload.available_topics),
            "suggested_role_counts": {
                role: len(topics)
                for role, topics in sorted(payload.suggested_role_names.items())
            },
            "available_topics_preview": [
                preview_string(topic)
                for topic in payload.available_topics[:preview_items]
            ],
        }
    elif kind == "artifact_state":
        snapshot = payload.snapshot
        projection = {
            "kind": kind,
            "segment_count": len(snapshot.segments or []),
            "raw_input_exists": snapshot.raw_input_exists,
            "raw_temp_exists": snapshot.raw_temp_exists,
            "sync_data_exists": snapshot.sync_data_exists,
            "synced_segment_count": sum(snapshot.sync_data_by_segment.values()),
            "finish_temp_samples_exists": snapshot.finish_temp_samples_exists,
            "final_outputs_exist": snapshot.final_outputs_exist,
            "final_grid_map_exists": snapshot.final_grid_map_exists,
            "sync_image_sample_count": len(snapshot.sync_image_samples),
        }
    elif kind == "gridmap_artifacts":
        projection = {
            "kind": kind,
            "existing_gridmap_count": len(payload.existing_gridmap_paths),
            "pcd_source_count": len(payload.pcd_sources),
            "projection_ready": payload.projection_ready,
            "existing_gridmap_preview": [
                preview_string(path)
                for path in payload.existing_gridmap_paths[:preview_items]
            ],
            "pcd_sources_preview": [
                preview_string(path)
                for path in payload.pcd_sources[:preview_items]
            ],
        }
    elif kind == "runtime_assets":
        available_variants = sorted(
            variant
            for variant, available in payload.projection_variants.items()
            if available
        )
        projection = {
            "kind": kind,
            "pcd_gridmap_tool_available": payload.pcd_gridmap_tool_available,
            "manual_annotation_gui_available": payload.manual_annotation_gui_available,
            "projection_variant_count": len(payload.projection_variants),
            "available_projection_variant_count": len(available_variants),
            "available_projection_variants_preview": [
                preview_string(variant)
                for variant in available_variants[:preview_items]
            ],
        }
    elif kind == "calibration_inventory":
        projection = {
            "kind": kind,
            "sensor_source_count": len(payload.sensor_sources),
            "sensor_sources_preview": [
                preview_string(source)
                for source in payload.sensor_sources[:preview_items]
            ],
        }
    elif kind == "localization_sources":
        projection = {
            "kind": kind,
            "available_source_count": len(payload.available_sources),
            "available_sources_preview": list(payload.available_sources),
            "conversion_available": payload.conversion_available,
        }
    elif kind == "user_guidance":
        text = preview_string(payload.text)
        projection = {
            "kind": kind,
            "guidance_revision": payload.guidance_revision,
            "text": text,
            "text_truncated": text != payload.text,
        }
    else:
        raise ValueError(f"unsupported compact observation kind: {kind}")
    return ensure_payload_within_limit(
        projection,
        max_chars=max_chars,
        label=f"{kind}_observation_projection",
    )

import pytest
from pydantic import TypeAdapter, ValidationError

from vla_data_juicer_agents.navigation.observation_models import (
    ArtifactStateObservation,
    EvidenceDescriptor,
    NavigationObservationRevision,
    ObservationPayload,
    RawMetadataObservation,
    RuntimeAssetsObservation,
)
from vla_data_juicer_agents.navigation.task_state import NavigationArtifactSnapshot


def _snapshot() -> NavigationArtifactSnapshot:
    return NavigationArtifactSnapshot(date="20260710", segments=["20260710_120000"])


def test_observation_payloads_forbid_policy_fields():
    with pytest.raises(ValidationError):
        ArtifactStateObservation.model_validate(
            {
                "kind": "artifact_state",
                "snapshot": _snapshot(),
                "localization_policy": {"source": "odom"},
            }
        )


def test_unavailable_resource_still_completes_observation_kind():
    revision = NavigationObservationRevision(
        task_id="nav-1",
        revision=1,
        phase="finish_processing",
        completed_kinds=["runtime_assets"],
        payloads=[
            RuntimeAssetsObservation(
                pcd_gridmap_tool_available=False,
                manual_annotation_gui_available=False,
                projection_variants={},
            )
        ],
    )

    assert revision.completed_kinds == ["runtime_assets"]


def test_observation_payload_uses_kind_discriminator():
    payload = TypeAdapter(ObservationPayload).validate_python(
        {
            "kind": "raw_metadata",
            "segments": ["20260710_120000"],
            "topics": [
                {
                    "topic": "/lidar/points",
                    "message_type": "sensor_msgs/msg/PointCloud2",
                    "message_count": 42,
                }
            ],
        }
    )

    assert isinstance(payload, RawMetadataObservation)
    assert payload.topics[0].message_count == 42


def test_nested_observation_models_forbid_extra_fields():
    with pytest.raises(ValidationError):
        RawMetadataObservation.model_validate(
            {
                "segments": ["20260710_120000"],
                "topics": [{"topic": "/lidar/points", "policy": "prefer"}],
            }
        )


def test_evidence_descriptor_does_not_expose_filesystem_path():
    schema_text = str(EvidenceDescriptor.model_json_schema())

    assert "path" not in schema_text

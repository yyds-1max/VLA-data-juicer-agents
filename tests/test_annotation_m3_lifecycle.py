from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from vla_data_juicer_agents.annotation import operator_cli
from vla_data_juicer_agents.annotation.models import AnnotationConflictError
from vla_data_juicer_agents.annotation.store import AnnotationStore
from vla_data_juicer_agents.annotation.store import _annotation_unit_lifecycle
from vla_data_juicer_agents.annotation.store import _review_unit_lifecycle


def _trajectory(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asset(
    path: Path,
    *,
    ordinal: int,
    total: int,
    dataset_date: str = "20260623",
    source_clip: str = "20260623_145550",
) -> dict[str, object]:
    return {
        "dataset_date": dataset_date,
        "source_clip": source_clip,
        "segment_ordinal": ordinal,
        "segment_total": total,
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "private_artifact_path": str(path),
    }


@pytest.mark.parametrize(
    ("segment_status", "expected"),
    [
        ("pending_initial_annotation", "waiting_initial_annotation"),
        ("tracking", "processing"),
        ("postprocessing_failed", "failed"),
        ("skipped", "annotated"),
        ("annotated", "annotated"),
    ],
)
def test_native_annotation_unit_lifecycle_mapping(
    segment_status: str,
    expected: str,
) -> None:
    assert _annotation_unit_lifecycle(
        job_status=(
            "waiting_initial_annotation"
            if segment_status == "pending_initial_annotation"
            else "annotated"
            if segment_status in {"annotated", "postprocessing_failed"}
            else "tracking"
        ),
        completion_outcome=None,
        segment_status=segment_status,
    ) == expected


@pytest.mark.parametrize(
    ("segment_status", "review_status", "expected"),
    [
        ("tracking", None, None),
        ("skipped", None, "discarded"),
        ("annotated", None, "pending"),
        ("annotated", "pending", "pending"),
        ("annotated", "in_progress", "in_progress"),
        ("annotated", "returned", "returned"),
        ("annotated", "discarded", "discarded"),
        ("annotated", "approved", "verified"),
    ],
)
def test_native_review_unit_lifecycle_mapping(
    segment_status: str,
    review_status: str | None,
    expected: str | None,
) -> None:
    assert _review_unit_lifecycle(
        segment_status=segment_status,
        completion_outcome=None,
        review_status=review_status,
    ) == expected


def test_historical_verified_import_is_idempotent_and_projects_no_private_path(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    first_path = tmp_path / "finish" / "segment-1_trajectory_fix_five.json"
    second_path = tmp_path / "finish" / "segment-2_trajectory_fix_five.json"
    first_sha = _trajectory(first_path, {"frame": {"master": {"x": 1}}})
    _trajectory(second_path, {"frame": {"master": {"x": 2}}})
    assets = [
        _asset(first_path, ordinal=1, total=2),
        _asset(second_path, ordinal=2, total=2),
    ]

    first_import = store.import_historical_verified_assets(
        manifest_sha256="a" * 64,
        assets=assets[:1],
    )
    partial_scope = store.asset_lifecycle_snapshot()["scopes"][0]
    assert partial_scope["annotation"]["status"] == "processing"
    assert partial_scope["annotation"]["counts"]["annotated"] == 1
    assert partial_scope["annotation"]["counts"]["not_started"] == 1
    assert partial_scope["review"]["status"] == "partial"
    assert partial_scope["review"]["counts"]["total"] == 2
    assert partial_scope["review"]["counts"]["pending"] == 1
    assert partial_scope["review"]["counts"]["verified"] == 1

    created = store.import_historical_verified_assets(
        manifest_sha256="a" * 64,
        assets=assets,
    )
    replay = store.import_historical_verified_assets(
        manifest_sha256="a" * 64,
        assets=assets,
    )

    assert first_import["imported"] == 1
    assert created["imported"] == 1
    assert created["existing"] == 1
    assert replay["imported"] == 0
    assert replay["existing"] == 2
    assert replay["asset_refs"] == created["asset_refs"]
    snapshot = store.asset_lifecycle_snapshot()
    assert snapshot["releases"] == []
    assert len(snapshot["scopes"]) == 1
    scope = snapshot["scopes"][0]
    assert scope["dataset_date"] == "20260623"
    assert scope["source_clip"] == "20260623_145550"
    assert scope["annotation"]["status"] == "annotated"
    assert scope["annotation"]["counts"]["annotated"] == 2
    assert scope["annotation"]["historical_asset_ref"] == created["asset_refs"][0]
    assert scope["review"]["status"] == "verified"
    assert scope["review"]["counts"]["verified"] == 2
    assert scope["review"]["publishable_verified_unit_count"] == 2
    assert scope["review"]["source"] == "historical_import"
    public_asset = store.get_historical_verified_asset(created["asset_refs"][0])
    assert public_asset["content_sha256"] == first_sha
    assert public_asset["segment_ordinal"] == 1
    assert str(tmp_path) not in json.dumps(public_asset)


def test_historical_verified_import_refuses_inconsistent_or_native_scope(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    first_path = tmp_path / "finish" / "segment-1_trajectory_fix_five.json"
    second_path = tmp_path / "finish" / "segment-2_trajectory_fix_five.json"
    native_path = tmp_path / "finish" / "native_trajectory_fix_five.json"
    _trajectory(first_path, {"frame": 1})
    _trajectory(second_path, {"frame": 2})
    _trajectory(native_path, {"frame": 3})
    store.import_historical_verified_assets(
        manifest_sha256="a" * 64,
        assets=[_asset(first_path, ordinal=1, total=2)],
    )

    with pytest.raises(AnnotationConflictError, match="authority"):
        store.import_historical_verified_assets(
            manifest_sha256="b" * 64,
            assets=[_asset(second_path, ordinal=2, total=3)],
        )

    store.create_job(
        job_ref="job_" + "1" * 32,
        dataset_date="20270605",
        source_clips=["20260605_160904"],
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "20260529_go2w",
            "content_sha256": "c" * 64,
        },
        snapshot_dir=tmp_path / "calibration",
        snapshot_files=[],
        reserved_bytes=1,
        idempotency_key="native-job",
    )
    with pytest.raises(AnnotationConflictError, match="authority"):
        store.import_historical_verified_assets(
            manifest_sha256="d" * 64,
            assets=[
                _asset(
                    native_path,
                    ordinal=1,
                    total=1,
                    dataset_date="20270605",
                    source_clip="20260605_160904",
                )
            ],
        )


def test_historical_verified_rows_are_immutable(tmp_path: Path) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    artifact = tmp_path / "finish" / "segment_trajectory_fix_five.json"
    _trajectory(artifact, {"frame": 1})
    store.import_historical_verified_assets(
        manifest_sha256="a" * 64,
        assets=[_asset(artifact, ordinal=1, total=1)],
    )

    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE historical_verified_assets SET segment_total = 2",
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM historical_verified_assets")


def test_history_manifest_validation_is_explicit_and_hash_bound(
    tmp_path: Path,
) -> None:
    finish_root = tmp_path / "finish_data"
    artifact = finish_root / "20260623" / "clip" / "segment_trajectory_fix_five.json"
    artifact_sha = _trajectory(artifact, {"frame": {"pass": False}})
    manifest = tmp_path / "history-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "dataset_date": "20260623",
                        "source_clip": "clip",
                        "segment_ordinal": 1,
                        "segment_total": 1,
                        "relative_path": str(artifact.relative_to(finish_root)),
                        "sha256": artifact_sha,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    manifest_sha, assets = operator_cli._historical_import_manifest(
        finish_data_root=finish_root,
        manifest_path=manifest,
    )
    assert manifest_sha == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert assets[0]["private_artifact_path"] == str(artifact)

    artifact.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(AnnotationConflictError) as exc_info:
        operator_cli._historical_import_manifest(
            finish_data_root=finish_root,
            manifest_path=manifest,
        )
    assert exc_info.value.code == "historical_asset_hash_mismatch"


def test_history_manifest_rejects_path_escape_symlink_and_duplicate_ordinal(
    tmp_path: Path,
) -> None:
    finish_root = tmp_path / "finish_data"
    artifact = finish_root / "20260623" / "segment_trajectory_fix_five.json"
    artifact_sha = _trajectory(artifact, {"frame": {"pass": False}})
    outside = tmp_path / "outside_trajectory_fix_five.json"
    outside_sha = _trajectory(outside, {"frame": {"pass": True}})
    manifest = tmp_path / "history-manifest.json"

    def write_manifest(assets: list[dict[str, object]]) -> None:
        manifest.write_text(json.dumps({"assets": assets}), encoding="utf-8")

    base = {
        "dataset_date": "20260623",
        "source_clip": "clip",
        "segment_ordinal": 1,
        "segment_total": 1,
        "relative_path": str(artifact.relative_to(finish_root)),
        "sha256": artifact_sha,
    }
    escaped = {
        **base,
        "relative_path": "../outside_trajectory_fix_five.json",
        "sha256": outside_sha,
    }
    write_manifest([escaped])
    with pytest.raises(operator_cli._OperatorScopeError, match="escapes"):
        operator_cli._historical_import_manifest(
            finish_data_root=finish_root,
            manifest_path=manifest,
        )

    alias = finish_root / "linked_trajectory_fix_five.json"
    alias.symlink_to(artifact)
    write_manifest([{**base, "relative_path": alias.name}])
    with pytest.raises(operator_cli._OperatorScopeError, match="unsafe"):
        operator_cli._historical_import_manifest(
            finish_data_root=finish_root,
            manifest_path=manifest,
        )

    duplicate = {**base, "relative_path": str(artifact.relative_to(finish_root))}
    write_manifest([base, duplicate])
    with pytest.raises(operator_cli._OperatorCLIUsageError, match="inconsistent"):
        operator_cli._historical_import_manifest(
            finish_data_root=finish_root,
            manifest_path=manifest,
        )


def test_native_annotation_authority_supersedes_historical_projection(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    artifact = tmp_path / "finish" / "segment_trajectory_fix_five.json"
    _trajectory(artifact, {"frame": 1})
    store.import_historical_verified_assets(
        manifest_sha256="a" * 64,
        assets=[
            _asset(
                artifact,
                ordinal=1,
                total=1,
                dataset_date="20270605",
                source_clip="20260605_160904",
            )
        ],
    )

    created = store.create_job(
        job_ref="job_" + "2" * 32,
        dataset_date="20270605",
        source_clips=["20260605_160904"],
        calibration={
            "profile_ref": "20260529_go2w",
            "label": "20260529_go2w",
            "content_sha256": "c" * 64,
        },
        snapshot_dir=tmp_path / "calibration",
        snapshot_files=[],
        reserved_bytes=1,
        idempotency_key="native-overrides-history",
    )

    scope = store.asset_lifecycle_snapshot()["scopes"][0]
    assert scope["annotation"]["source"] == "native"
    assert scope["annotation"]["job_ref"] == created["job_ref"]
    assert scope["annotation"]["status"] == "processing"
    assert scope["annotation"]["historical_asset_ref"] is None
    assert scope["review"] is None


def test_dataset_release_is_scope_bound_idempotent_and_immutable(
    tmp_path: Path,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    artifacts = [
        tmp_path / "finish" / f"segment-{ordinal}_trajectory_fix_five.json"
        for ordinal in (1, 2)
    ]
    for ordinal, artifact in enumerate(artifacts, start=1):
        _trajectory(artifact, {"frame": ordinal})
    store.import_historical_verified_assets(
        manifest_sha256="a" * 64,
        assets=[
            _asset(artifact, ordinal=ordinal, total=2)
            for ordinal, artifact in enumerate(artifacts, start=1)
        ],
    )
    managed_clips = [
        {
            "source_clip": "20260623_145550",
            "status": "synced",
            "duration_ns": 12_345,
        }
    ]

    candidate = store.dataset_release_candidate(
        dataset_date="20260623",
        managed_clips=managed_clips,
    )

    assert candidate["status"] == "ready"
    assert candidate["verified_unit_count"] == 2
    assert candidate["discarded_unit_count"] == 0
    assert candidate["note"] is None
    with pytest.raises(AnnotationConflictError) as stale:
        store.create_dataset_release(
            dataset_date="20260623",
            managed_clips=managed_clips,
            expected_scope_manifest_sha256="b" * 64,
            note=None,
            idempotency_key="release-stale-scope",
        )
    assert stale.value.code == "release_scope_changed"

    released = store.create_dataset_release(
        dataset_date="20260623",
        managed_clips=managed_clips,
        expected_scope_manifest_sha256=candidate["scope_manifest_sha256"],
        note="  ",
        idempotency_key="release-date",
    )
    replay = store.create_dataset_release(
        dataset_date="20260623",
        managed_clips=managed_clips,
        expected_scope_manifest_sha256=candidate["scope_manifest_sha256"],
        note=None,
        idempotency_key="release-date",
    )

    assert replay == released
    assert released["status"] == "released"
    assert released["note"] is None
    assert store.asset_lifecycle_snapshot()["releases"] == [released]
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE dataset_releases SET note = 'changed'",
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM dataset_releases")


def test_dataset_release_requires_verified_publishable_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AnnotationStore(tmp_path / "annotation.sqlite")
    monkeypatch.setattr(
        store,
        "asset_lifecycle_snapshot",
        lambda: {
            "scopes": [
                {
                    "dataset_date": "20260623",
                    "source_clip": "clip",
                    "annotation": {
                        "status": "annotated",
                        "counts": {"total": 2},
                    },
                    "review": {
                        "status": "discarded",
                        "counts": {"total": 2, "verified": 0, "discarded": 2},
                        "publishable_verified_unit_count": 0,
                    },
                }
            ],
            "releases": [],
        },
    )

    candidate = store.dataset_release_candidate(
        dataset_date="20260623",
        managed_clips=[
            {"source_clip": "clip", "status": "synced", "duration_ns": 1}
        ],
    )

    assert candidate["status"] == "not_ready"
    assert candidate["verified_unit_count"] == 0
    assert candidate["discarded_unit_count"] == 2

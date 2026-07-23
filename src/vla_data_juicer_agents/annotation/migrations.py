from __future__ import annotations

import sqlite3


LATEST_ANNOTATION_SCHEMA_VERSION = 3


class UnsupportedAnnotationSchemaVersionError(RuntimeError):
    """Raised before a newer annotation database can be mutated."""


def prepare_annotation_migration_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS annotation_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    versions = [
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM annotation_schema_migrations ORDER BY version"
        ).fetchall()
    ]
    if versions and versions[-1] > LATEST_ANNOTATION_SCHEMA_VERSION:
        raise UnsupportedAnnotationSchemaVersionError(
            "annotation database schema version "
            f"{versions[-1]} is newer than supported version "
            f"{LATEST_ANNOTATION_SCHEMA_VERSION}"
        )
    expected = list(range(1, (versions[-1] if versions else 0) + 1))
    if versions != expected:
        raise RuntimeError(
            f"annotation database has a non-contiguous migration ledger: {versions}"
        )


def apply_annotation_migrations(
    connection: sqlite3.Connection,
    *,
    applied_at: str,
) -> None:
    applied = {
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM annotation_schema_migrations"
        ).fetchall()
    }
    for version, name, migration in _MIGRATIONS:
        if version in applied:
            continue
        try:
            # Each migration opens its own IMMEDIATE transaction.  The schema
            # and its ledger row are committed together, so a failed ledger
            # write can never leave an unversioned partial schema behind.
            migration(connection)
            connection.execute(
                """
                INSERT INTO annotation_schema_migrations (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, name, applied_at),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _migration_001_annotation_m1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        BEGIN IMMEDIATE;

        CREATE TABLE annotation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_ref TEXT NOT NULL UNIQUE,
            dataset_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'preparing', 'waiting_initial_annotation', 'tracking',
                    'tracked', 'failed', 'cancelled'
                )
            ),
            completion_outcome TEXT,
            state_revision INTEGER NOT NULL DEFAULT 0,
            reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (
                cancel_requested IN (0, 1)
            ),
            cancel_requested_at TEXT,
            calibration_snapshot_id INTEGER,
            staging_root TEXT,
            failure_code TEXT,
            failure_message TEXT,
            failure_ref TEXT,
            private_failure_detail TEXT,
            failure_retryable INTEGER NOT NULL DEFAULT 0 CHECK (failure_retryable IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (calibration_snapshot_id) REFERENCES calibration_snapshots(id)
        );

        CREATE TABLE calibration_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL UNIQUE,
            profile_ref TEXT NOT NULL,
            label TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            private_snapshot_dir TEXT NOT NULL,
            files_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id)
        );

        CREATE TABLE annotation_job_source_clips (
            job_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            source_clip TEXT NOT NULL,
            PRIMARY KEY (job_id, source_clip),
            UNIQUE (job_id, ordinal),
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id)
        );

        CREATE TABLE annotation_source_leases (
            dataset_date TEXT NOT NULL,
            source_clip TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            acquired_at TEXT NOT NULL,
            PRIMARY KEY (dataset_date, source_clip),
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id)
        );

        CREATE TABLE annotation_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            source_clip TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending_initial_annotation', 'draft', 'submitted',
                    'skipped', 'tracking', 'tracked'
                )
            ),
            state_revision INTEGER NOT NULL DEFAULT 0,
            draft_revision INTEGER NOT NULL DEFAULT 0,
            submitted_revision INTEGER,
            private_segment_key TEXT NOT NULL,
            private_segment_root TEXT NOT NULL,
            private_first_frame_path TEXT NOT NULL,
            first_frame_width INTEGER NOT NULL,
            first_frame_height INTEGER NOT NULL,
            first_frame_sha256 TEXT NOT NULL,
            first_frame_etag TEXT NOT NULL,
            skip_reason_code TEXT,
            skip_note TEXT,
            skip_restore_status TEXT CHECK (
                skip_restore_status IS NULL OR skip_restore_status IN (
                    'pending_initial_annotation', 'draft', 'submitted'
                )
            ),
            skip_restore_submitted_revision INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (job_id, ordinal),
            UNIQUE (job_id, private_segment_key),
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id)
        );

        CREATE TABLE initial_annotation_drafts (
            segment_id INTEGER PRIMARY KEY,
            draft_revision INTEGER NOT NULL,
            targets_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (segment_id) REFERENCES annotation_segments(id)
        );

        CREATE TABLE initial_annotation_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_ref TEXT NOT NULL UNIQUE,
            segment_id INTEGER NOT NULL,
            revision_number INTEGER NOT NULL,
            targets_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (segment_id, revision_number),
            FOREIGN KEY (segment_id) REFERENCES annotation_segments(id)
        );

        CREATE TABLE runtime_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('prepare', 'tracking')),
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
            ),
            attempt INTEGER NOT NULL,
            worker_id TEXT,
            started_at TEXT,
            finished_at TEXT,
            failure_code TEXT,
            failure_message TEXT,
            failure_ref TEXT,
            private_failure_detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (job_id, kind, attempt),
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id)
        );

        CREATE TABLE runtime_run_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            safe_step_code TEXT NOT NULL,
            status TEXT NOT NULL,
            artifact_sha256 TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (run_id, ordinal),
            FOREIGN KEY (run_id) REFERENCES runtime_runs(id)
        );

        CREATE TABLE runtime_leases (
            lease_key TEXT PRIMARY KEY,
            run_id INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runtime_runs(id)
        );

        CREATE TABLE artifact_manifests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manifest_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            stage TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id),
            FOREIGN KEY (run_id) REFERENCES runtime_runs(id)
        );

        CREATE TABLE tracking_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkpoint_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            target_ref TEXT NOT NULL,
            revision_sha256 TEXT NOT NULL,
            identity TEXT NOT NULL,
            private_output_dir TEXT NOT NULL,
            private_points_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (job_id, segment_id, target_ref, revision_sha256),
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id),
            FOREIGN KEY (run_id) REFERENCES runtime_runs(id),
            FOREIGN KEY (segment_id) REFERENCES annotation_segments(id)
        );

        CREATE TABLE annotation_segment_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_ref TEXT NOT NULL UNIQUE,
            segment_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            safe_payload_json TEXT NOT NULL,
            actor_kind TEXT NOT NULL CHECK (actor_kind = 'manual_web'),
            deployment_instance TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (segment_id) REFERENCES annotation_segments(id)
        );

        CREATE TABLE annotation_mutation_receipts (
            idempotency_key TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL,
            actor_kind TEXT NOT NULL CHECK (actor_kind = 'manual_web'),
            deployment_instance TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE annotation_operator_actions (
            idempotency_key TEXT PRIMARY KEY,
            action_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK (
                action IN ('confirm_recovery', 'abandon_recovery')
            ),
            confirmation TEXT NOT NULL CHECK (
                confirmation = 'old_process_group_absent'
            ),
            operator_reference TEXT NOT NULL,
            deployment_instance TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id)
        );

        CREATE INDEX idx_annotation_jobs_updated
        ON annotation_jobs (updated_at DESC);
        CREATE INDEX idx_annotation_segments_job
        ON annotation_segments (job_id, ordinal);
        CREATE INDEX idx_runtime_runs_claim
        ON runtime_runs (status, created_at);

        CREATE TRIGGER initial_annotation_revisions_no_update
        BEFORE UPDATE ON initial_annotation_revisions
        BEGIN
            SELECT RAISE(ABORT, 'initial annotation revisions are immutable');
        END;
        CREATE TRIGGER initial_annotation_revisions_no_delete
        BEFORE DELETE ON initial_annotation_revisions
        BEGIN
            SELECT RAISE(ABORT, 'initial annotation revisions are immutable');
        END;
        CREATE TRIGGER calibration_snapshots_no_update
        BEFORE UPDATE ON calibration_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'calibration snapshots are immutable');
        END;
        CREATE TRIGGER calibration_snapshots_no_delete
        BEFORE DELETE ON calibration_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'calibration snapshots are immutable');
        END;
        CREATE TRIGGER artifact_manifests_no_update
        BEFORE UPDATE ON artifact_manifests
        BEGIN
            SELECT RAISE(ABORT, 'artifact manifests are immutable');
        END;
        CREATE TRIGGER artifact_manifests_no_delete
        BEFORE DELETE ON artifact_manifests
        BEGIN
            SELECT RAISE(ABORT, 'artifact manifests are immutable');
        END;
        CREATE TRIGGER tracking_checkpoints_no_update
        BEFORE UPDATE ON tracking_checkpoints
        BEGIN
            SELECT RAISE(ABORT, 'tracking checkpoints are immutable');
        END;
        CREATE TRIGGER tracking_checkpoints_no_delete
        BEFORE DELETE ON tracking_checkpoints
        BEGIN
            SELECT RAISE(ABORT, 'tracking checkpoints are immutable');
        END;
        CREATE TRIGGER annotation_segment_actions_no_update
        BEFORE UPDATE ON annotation_segment_actions
        BEGIN
            SELECT RAISE(ABORT, 'annotation segment actions are immutable');
        END;
        CREATE TRIGGER annotation_segment_actions_no_delete
        BEFORE DELETE ON annotation_segment_actions
        BEGIN
            SELECT RAISE(ABORT, 'annotation segment actions are immutable');
        END;
        CREATE TRIGGER annotation_mutation_receipts_no_update
        BEFORE UPDATE ON annotation_mutation_receipts
        BEGIN
            SELECT RAISE(ABORT, 'annotation mutation receipts are immutable');
        END;
        CREATE TRIGGER annotation_mutation_receipts_no_delete
        BEFORE DELETE ON annotation_mutation_receipts
        BEGIN
            SELECT RAISE(ABORT, 'annotation mutation receipts are immutable');
        END;
        CREATE TRIGGER annotation_operator_actions_no_update
        BEFORE UPDATE ON annotation_operator_actions
        BEGIN
            SELECT RAISE(ABORT, 'annotation operator actions are immutable');
        END;
        CREATE TRIGGER annotation_operator_actions_no_delete
        BEFORE DELETE ON annotation_operator_actions
        BEGIN
            SELECT RAISE(ABORT, 'annotation operator actions are immutable');
        END;
        """
    )


def _migration_002_runtime_step_evidence(
    connection: sqlite3.Connection,
) -> None:
    connection.executescript(
        """
        BEGIN IMMEDIATE;

        ALTER TABLE runtime_run_steps ADD COLUMN return_code INTEGER;
        ALTER TABLE runtime_run_steps ADD COLUMN diagnostic_ref TEXT;
        """
    )


def _migration_003_global_writer_quarantine_audit(
    connection: sqlite3.Connection,
) -> None:
    connection.executescript(
        """
        BEGIN IMMEDIATE;

        CREATE TABLE writer_quarantine_actions (
            idempotency_key TEXT PRIMARY KEY,
            action_ref TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL CHECK (action = 'clear_global_quarantine'),
            confirmation TEXT NOT NULL CHECK (
                confirmation =
                    'all_navigation_annotation_writer_process_groups_absent'
            ),
            operator_reference TEXT NOT NULL,
            deployment_instance TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            expected_marker_state_sha256 TEXT NOT NULL,
            expected_marker_entries_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE writer_quarantine_action_completions (
            action_ref TEXT PRIMARY KEY,
            marker_was_present INTEGER NOT NULL CHECK (
                marker_was_present IN (0, 1)
            ),
            response_json TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            FOREIGN KEY (action_ref) REFERENCES writer_quarantine_actions(action_ref)
        );

        CREATE TABLE writer_quarantine_action_recoveries (
            action_ref TEXT NOT NULL,
            failure_ref TEXT NOT NULL,
            PRIMARY KEY (action_ref, failure_ref),
            FOREIGN KEY (action_ref) REFERENCES writer_quarantine_actions(action_ref)
        );

        ALTER TABLE annotation_operator_actions
        ADD COLUMN global_quarantine_action_ref TEXT;

        ALTER TABLE runtime_leases
        ADD COLUMN owner_epoch TEXT;

        CREATE TRIGGER writer_quarantine_actions_no_update
        BEFORE UPDATE ON writer_quarantine_actions
        BEGIN
            SELECT RAISE(ABORT, 'writer quarantine actions are immutable');
        END;
        CREATE TRIGGER writer_quarantine_actions_no_delete
        BEFORE DELETE ON writer_quarantine_actions
        BEGIN
            SELECT RAISE(ABORT, 'writer quarantine actions are immutable');
        END;
        CREATE TRIGGER writer_quarantine_action_completions_no_update
        BEFORE UPDATE ON writer_quarantine_action_completions
        BEGIN
            SELECT RAISE(
                ABORT,
                'writer quarantine action completions are immutable'
            );
        END;
        CREATE TRIGGER writer_quarantine_action_completions_no_delete
        BEFORE DELETE ON writer_quarantine_action_completions
        BEGIN
            SELECT RAISE(
                ABORT,
                'writer quarantine action completions are immutable'
            );
        END;
        CREATE TRIGGER writer_quarantine_action_recoveries_no_update
        BEFORE UPDATE ON writer_quarantine_action_recoveries
        BEGIN
            SELECT RAISE(
                ABORT,
                'writer quarantine recovery bindings are immutable'
            );
        END;
        CREATE TRIGGER writer_quarantine_action_recoveries_no_delete
        BEFORE DELETE ON writer_quarantine_action_recoveries
        BEGIN
            SELECT RAISE(
                ABORT,
                'writer quarantine recovery bindings are immutable'
            );
        END;
        """
    )


_MIGRATIONS = (
    (1, "annotation_m1", _migration_001_annotation_m1),
    (2, "runtime_step_evidence", _migration_002_runtime_step_evidence),
    (
        3,
        "global_writer_quarantine_audit",
        _migration_003_global_writer_quarantine_audit,
    ),
)

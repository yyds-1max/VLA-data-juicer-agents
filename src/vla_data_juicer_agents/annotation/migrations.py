from __future__ import annotations

import sqlite3


LATEST_ANNOTATION_SCHEMA_VERSION = 5


class UnsupportedAnnotationSchemaVersionError(RuntimeError):
    """Raised before a newer annotation database can be mutated."""


class AnnotationOfflineMigrationRequiredError(RuntimeError):
    """Raised when an existing Store needs an explicit offline migration."""


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
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.rollback()
            connection.execute("PRAGMA foreign_keys = ON")
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


def _migration_004_annotation_m2_domain(
    connection: sqlite3.Connection,
) -> None:
    # SQLite cannot widen a CHECK constraint in place.  Foreign-key
    # enforcement is disabled only for this transaction while the three M1
    # state-bearing tables are rebuilt with identical keys and data.  The
    # caller commits the schema and ledger row together, then restores foreign
    # keys.  foreign_key_check below makes a broken copy fail before commit.
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA legacy_alter_table = ON")
    connection.executescript(
        """
        BEGIN IMMEDIATE;

        ALTER TABLE annotation_jobs RENAME TO annotation_jobs_m1;
        CREATE TABLE annotation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_ref TEXT NOT NULL UNIQUE,
            dataset_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'preparing', 'waiting_initial_annotation', 'tracking',
                    'tracked', 'postprocessing', 'annotated', 'failed',
                    'cancelled'
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
            failure_retryable INTEGER NOT NULL DEFAULT 0 CHECK (
                failure_retryable IN (0, 1)
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (calibration_snapshot_id) REFERENCES calibration_snapshots(id)
        );
        INSERT INTO annotation_jobs
        SELECT * FROM annotation_jobs_m1;

        ALTER TABLE annotation_segments RENAME TO annotation_segments_m1;
        CREATE TABLE annotation_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            source_clip TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending_initial_annotation', 'draft', 'submitted',
                    'skipped', 'tracking', 'tracked', 'postprocessing',
                    'annotated', 'postprocessing_failed'
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
        INSERT INTO annotation_segments
        SELECT * FROM annotation_segments_m1;

        ALTER TABLE runtime_runs RENAME TO runtime_runs_m1;
        CREATE TABLE runtime_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (
                kind IN (
                    'prepare', 'tracking', 'postprocessing', 'fix',
                    'compatibility_publish'
                )
            ),
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
        INSERT INTO runtime_runs
        SELECT * FROM runtime_runs_m1;

        DROP TABLE runtime_runs_m1;
        DROP TABLE annotation_segments_m1;
        DROP TABLE annotation_jobs_m1;

        ALTER TABLE annotation_mutation_receipts
        RENAME TO annotation_mutation_receipts_m1;
        CREATE TABLE annotation_mutation_receipts (
            idempotency_key TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL,
            actor_kind TEXT NOT NULL CHECK (
                actor_kind IN ('manual_web', 'datapilot', 'system_worker')
            ),
            deployment_instance TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO annotation_mutation_receipts
        SELECT * FROM annotation_mutation_receipts_m1;
        DROP TABLE annotation_mutation_receipts_m1;

        CREATE INDEX idx_annotation_jobs_updated
        ON annotation_jobs (updated_at DESC);
        CREATE INDEX idx_annotation_segments_job
        ON annotation_segments (job_id, ordinal);
        CREATE INDEX idx_runtime_runs_claim
        ON runtime_runs (status, created_at);

        CREATE TABLE postprocessing_specs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spec_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL UNIQUE,
            localization_kind TEXT NOT NULL CHECK (
                localization_kind IN ('odom', 'ins')
            ),
            gridmap_decision TEXT NOT NULL CHECK (
                gridmap_decision IN (
                    'copy_existing_gridmap', 'generate_from_pcd',
                    'skip_if_projection_ready'
                )
            ),
            trajectory_variant TEXT NOT NULL CHECK (
                trajectory_variant IN (
                    'cjl_with_gridmap', 'cjl_0525_with_gridmap'
                )
            ),
            plan_sha256 TEXT NOT NULL,
            observations_sha256 TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id)
        );

        CREATE TABLE trajectory_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            revision_number INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL,
            private_artifact_path TEXT NOT NULL,
            private_compatibility_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            private_state_json TEXT NOT NULL,
            artifact_manifest_ref TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (segment_id, revision_number),
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id),
            FOREIGN KEY (segment_id) REFERENCES annotation_segments(id)
        );

        CREATE TABLE trajectory_review_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_ref TEXT NOT NULL UNIQUE,
            trajectory_revision_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'in_progress', 'returned', 'approved',
                    'discarded'
                )
            ),
            state_revision INTEGER NOT NULL DEFAULT 0,
            active_fix_draft_id INTEGER,
            approved_fix_revision_id INTEGER,
            fix_failure_code TEXT,
            fix_failure_message TEXT,
            fix_failure_ref TEXT,
            fix_failure_retryable INTEGER NOT NULL DEFAULT 0 CHECK (
                fix_failure_retryable IN (0, 1)
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (trajectory_revision_id) REFERENCES trajectory_revisions(id),
            FOREIGN KEY (active_fix_draft_id) REFERENCES fix_drafts(id),
            FOREIGN KEY (approved_fix_revision_id) REFERENCES fix_revisions(id)
        );

        CREATE TABLE fix_calibration_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_ref TEXT NOT NULL UNIQUE,
            review_id INTEGER NOT NULL,
            profile_ref TEXT NOT NULL,
            label TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            private_snapshot_dir TEXT NOT NULL,
            files_json TEXT NOT NULL,
            differs_from_processing INTEGER NOT NULL CHECK (
                differs_from_processing IN (0, 1)
            ),
            difference_reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id)
        );

        CREATE TABLE fix_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_ref TEXT NOT NULL UNIQUE,
            review_id INTEGER NOT NULL UNIQUE,
            calibration_snapshot_id INTEGER NOT NULL,
            base_trajectory_revision_id INTEGER NOT NULL,
            draft_revision INTEGER NOT NULL,
            original_state_json TEXT NOT NULL,
            state_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id),
            FOREIGN KEY (calibration_snapshot_id) REFERENCES fix_calibration_snapshots(id),
            FOREIGN KEY (base_trajectory_revision_id) REFERENCES trajectory_revisions(id)
        );

        CREATE TABLE runtime_run_review_links (
            run_id INTEGER PRIMARY KEY,
            review_id INTEGER NOT NULL,
            fix_draft_id INTEGER NOT NULL,
            source_draft_revision INTEGER NOT NULL,
            planned_revision_ref TEXT NOT NULL UNIQUE,
            planned_revision_number INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runtime_runs(id),
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id),
            FOREIGN KEY (fix_draft_id) REFERENCES fix_drafts(id)
        );

        CREATE TABLE fix_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_ref TEXT NOT NULL UNIQUE,
            review_id INTEGER NOT NULL,
            revision_number INTEGER NOT NULL,
            calibration_snapshot_id INTEGER NOT NULL,
            base_trajectory_revision_id INTEGER NOT NULL,
            source_draft_revision INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            private_artifact_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            artifact_manifest_ref TEXT NOT NULL,
            runtime_run_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE (review_id, revision_number),
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id),
            FOREIGN KEY (calibration_snapshot_id) REFERENCES fix_calibration_snapshots(id),
            FOREIGN KEY (base_trajectory_revision_id) REFERENCES trajectory_revisions(id),
            FOREIGN KEY (runtime_run_id) REFERENCES runtime_runs(id)
        );

        CREATE TABLE fix_command_actions (
            action_ref TEXT PRIMARY KEY,
            review_id INTEGER NOT NULL,
            draft_revision INTEGER NOT NULL,
            command_json TEXT NOT NULL,
            result_sha256 TEXT NOT NULL,
            actor_kind TEXT NOT NULL CHECK (
                actor_kind IN ('manual_web', 'datapilot', 'system_worker')
            ),
            deployment_instance TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id)
        );

        CREATE TABLE review_decisions (
            decision_ref TEXT PRIMARY KEY,
            review_id INTEGER NOT NULL,
            decision TEXT NOT NULL CHECK (
                decision IN ('approved', 'returned', 'discarded')
            ),
            fix_revision_id INTEGER,
            reason TEXT,
            actor_kind TEXT NOT NULL CHECK (actor_kind = 'manual_web'),
            deployment_instance TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id),
            FOREIGN KEY (fix_revision_id) REFERENCES fix_revisions(id)
        );

        CREATE TABLE compatibility_publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_ref TEXT NOT NULL UNIQUE,
            review_id INTEGER NOT NULL,
            fix_revision_id INTEGER NOT NULL,
            attempt INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'succeeded', 'failed')
            ),
            content_sha256 TEXT,
            private_artifact_path TEXT,
            artifact_manifest_ref TEXT,
            failure_code TEXT,
            failure_ref TEXT,
            created_at TEXT NOT NULL,
            CHECK (
                (
                    status IN ('queued', 'running')
                    AND content_sha256 IS NULL
                    AND private_artifact_path IS NULL
                    AND artifact_manifest_ref IS NULL
                    AND failure_code IS NULL
                    AND failure_ref IS NULL
                )
                OR (
                    status = 'succeeded'
                    AND content_sha256 IS NOT NULL
                    AND private_artifact_path IS NOT NULL
                    AND artifact_manifest_ref IS NOT NULL
                    AND failure_code IS NULL
                    AND failure_ref IS NULL
                )
                OR (
                    status = 'failed'
                    AND content_sha256 IS NULL
                    AND private_artifact_path IS NULL
                    AND artifact_manifest_ref IS NULL
                    AND failure_code IS NOT NULL
                    AND failure_ref IS NOT NULL
                )
            ),
            UNIQUE (review_id, attempt),
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id),
            FOREIGN KEY (fix_revision_id) REFERENCES fix_revisions(id),
            FOREIGN KEY (artifact_manifest_ref) REFERENCES artifact_manifests(manifest_ref)
        );

        CREATE TABLE runtime_run_publication_links (
            run_id INTEGER PRIMARY KEY,
            publication_id INTEGER NOT NULL UNIQUE,
            review_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runtime_runs(id),
            FOREIGN KEY (publication_id) REFERENCES compatibility_publications(id),
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id)
        );

        CREATE TABLE workflow_handoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handoff_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            review_id INTEGER,
            kind TEXT NOT NULL CHECK (
                kind IN (
                    'initial_annotation_ready', 'initial_annotation_submitted',
                    'tracking_completed', 'postprocessing_completed',
                    'fix_ready', 'fix_revision_submitted',
                    'review_returned', 'review_completed'
                )
            ),
            payload_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id),
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id)
        );

        CREATE TABLE workflow_handoff_deliveries (
            handoff_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL CHECK (
                status IN ('running', 'delivered', 'retry')
            ),
            worker_id TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            lease_expires_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (handoff_id) REFERENCES workflow_handoffs(id)
        );

        CREATE TABLE annotation_task_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_ref TEXT NOT NULL UNIQUE,
            job_id INTEGER NOT NULL,
            review_id INTEGER,
            navigation_task_ref TEXT NOT NULL,
            parent_navigation_task_ref TEXT,
            link_kind TEXT NOT NULL CHECK (
                link_kind IN ('processing', 'trajectory_fix')
            ),
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES annotation_jobs(id),
            FOREIGN KEY (review_id) REFERENCES trajectory_review_tasks(id)
        );

        CREATE INDEX idx_trajectory_reviews_status
        ON trajectory_review_tasks (status, updated_at DESC);
        CREATE INDEX idx_trajectory_revisions_job
        ON trajectory_revisions (job_id, segment_id);
        CREATE INDEX idx_workflow_handoffs_job
        ON workflow_handoffs (job_id, created_at);
        CREATE INDEX idx_workflow_handoff_deliveries_status
        ON workflow_handoff_deliveries (status, lease_expires_at);
        CREATE INDEX idx_runtime_run_review_links_review
        ON runtime_run_review_links (review_id, run_id);
        CREATE INDEX idx_runtime_run_publication_links_review
        ON runtime_run_publication_links (review_id, run_id);
        CREATE UNIQUE INDEX idx_annotation_task_links_processing
        ON annotation_task_links (navigation_task_ref, link_kind)
        WHERE link_kind = 'processing';
        CREATE UNIQUE INDEX idx_annotation_task_links_fix_review
        ON annotation_task_links (navigation_task_ref, review_id, link_kind)
        WHERE link_kind = 'trajectory_fix';

        CREATE TRIGGER postprocessing_specs_no_update
        BEFORE UPDATE ON postprocessing_specs BEGIN
            SELECT RAISE(ABORT, 'postprocessing specs are immutable');
        END;
        CREATE TRIGGER postprocessing_specs_no_delete
        BEFORE DELETE ON postprocessing_specs BEGIN
            SELECT RAISE(ABORT, 'postprocessing specs are immutable');
        END;
        CREATE TRIGGER trajectory_revisions_no_update
        BEFORE UPDATE ON trajectory_revisions BEGIN
            SELECT RAISE(ABORT, 'trajectory revisions are immutable');
        END;
        CREATE TRIGGER trajectory_revisions_no_delete
        BEFORE DELETE ON trajectory_revisions BEGIN
            SELECT RAISE(ABORT, 'trajectory revisions are immutable');
        END;
        CREATE TRIGGER fix_calibration_snapshots_no_update
        BEFORE UPDATE ON fix_calibration_snapshots BEGIN
            SELECT RAISE(ABORT, 'fix calibration snapshots are immutable');
        END;
        CREATE TRIGGER fix_calibration_snapshots_no_delete
        BEFORE DELETE ON fix_calibration_snapshots BEGIN
            SELECT RAISE(ABORT, 'fix calibration snapshots are immutable');
        END;
        CREATE TRIGGER fix_revisions_no_update
        BEFORE UPDATE ON fix_revisions BEGIN
            SELECT RAISE(ABORT, 'fix revisions are immutable');
        END;
        CREATE TRIGGER fix_revisions_no_delete
        BEFORE DELETE ON fix_revisions BEGIN
            SELECT RAISE(ABORT, 'fix revisions are immutable');
        END;
        CREATE TRIGGER runtime_run_review_links_no_update
        BEFORE UPDATE ON runtime_run_review_links BEGIN
            SELECT RAISE(ABORT, 'runtime review links are immutable');
        END;
        CREATE TRIGGER runtime_run_review_links_no_delete
        BEFORE DELETE ON runtime_run_review_links BEGIN
            SELECT RAISE(ABORT, 'runtime review links are immutable');
        END;
        CREATE TRIGGER fix_command_actions_no_update
        BEFORE UPDATE ON fix_command_actions BEGIN
            SELECT RAISE(ABORT, 'fix command actions are immutable');
        END;
        CREATE TRIGGER fix_command_actions_no_delete
        BEFORE DELETE ON fix_command_actions BEGIN
            SELECT RAISE(ABORT, 'fix command actions are immutable');
        END;
        CREATE TRIGGER review_decisions_no_update
        BEFORE UPDATE ON review_decisions BEGIN
            SELECT RAISE(ABORT, 'review decisions are immutable');
        END;
        CREATE TRIGGER review_decisions_no_delete
        BEFORE DELETE ON review_decisions BEGIN
            SELECT RAISE(ABORT, 'review decisions are immutable');
        END;
        CREATE TRIGGER compatibility_publications_guard_update
        BEFORE UPDATE ON compatibility_publications
        WHEN
            OLD.publication_ref IS NOT NEW.publication_ref
            OR OLD.review_id IS NOT NEW.review_id
            OR OLD.fix_revision_id IS NOT NEW.fix_revision_id
            OR OLD.attempt IS NOT NEW.attempt
            OR OLD.created_at IS NOT NEW.created_at
            OR NOT (
                (OLD.status = 'queued' AND NEW.status = 'running'
                 AND NEW.content_sha256 IS NULL
                 AND NEW.private_artifact_path IS NULL
                 AND NEW.artifact_manifest_ref IS NULL
                 AND NEW.failure_code IS NULL
                 AND NEW.failure_ref IS NULL)
                OR
                (OLD.status = 'running' AND NEW.status = 'succeeded'
                 AND NEW.content_sha256 IS NOT NULL
                 AND NEW.private_artifact_path IS NOT NULL
                 AND NEW.artifact_manifest_ref IS NOT NULL
                 AND NEW.failure_code IS NULL
                 AND NEW.failure_ref IS NULL)
                OR
                (OLD.status = 'running' AND NEW.status = 'failed'
                 AND NEW.content_sha256 IS NULL
                 AND NEW.private_artifact_path IS NULL
                 AND NEW.artifact_manifest_ref IS NULL
                 AND NEW.failure_code IS NOT NULL
                 AND NEW.failure_ref IS NOT NULL)
            )
        BEGIN
            SELECT RAISE(ABORT, 'invalid compatibility publication transition');
        END;
        CREATE TRIGGER compatibility_publications_no_delete
        BEFORE DELETE ON compatibility_publications BEGIN
            SELECT RAISE(ABORT, 'compatibility publications are immutable');
        END;
        CREATE TRIGGER runtime_run_publication_links_no_update
        BEFORE UPDATE ON runtime_run_publication_links BEGIN
            SELECT RAISE(ABORT, 'runtime publication links are immutable');
        END;
        CREATE TRIGGER runtime_run_publication_links_no_delete
        BEFORE DELETE ON runtime_run_publication_links BEGIN
            SELECT RAISE(ABORT, 'runtime publication links are immutable');
        END;
        CREATE TRIGGER workflow_handoffs_no_update
        BEFORE UPDATE ON workflow_handoffs BEGIN
            SELECT RAISE(ABORT, 'workflow handoffs are immutable');
        END;
        CREATE TRIGGER workflow_handoffs_no_delete
        BEFORE DELETE ON workflow_handoffs BEGIN
            SELECT RAISE(ABORT, 'workflow handoffs are immutable');
        END;
        CREATE TRIGGER annotation_task_links_no_update
        BEFORE UPDATE ON annotation_task_links BEGIN
            SELECT RAISE(ABORT, 'annotation task links are immutable');
        END;
        CREATE TRIGGER annotation_task_links_no_delete
        BEFORE DELETE ON annotation_task_links BEGIN
            SELECT RAISE(ABORT, 'annotation task links are immutable');
        END;
        CREATE TRIGGER annotation_mutation_receipts_no_update
        BEFORE UPDATE ON annotation_mutation_receipts BEGIN
            SELECT RAISE(ABORT, 'annotation mutation receipts are immutable');
        END;
        CREATE TRIGGER annotation_mutation_receipts_no_delete
        BEFORE DELETE ON annotation_mutation_receipts BEGIN
            SELECT RAISE(ABORT, 'annotation mutation receipts are immutable');
        END;

        PRAGMA legacy_alter_table = OFF;
        """
    )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("annotation M2 migration failed foreign-key validation")


def _migration_005_processing_owner_and_safety_marker(
    connection: sqlite3.Connection,
) -> None:
    duplicate_processing_owners = connection.execute(
        """
        SELECT job_id
        FROM annotation_task_links
        WHERE link_kind = 'processing'
        GROUP BY job_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate_processing_owners is not None:
        raise RuntimeError(
            "annotation M2 migration found multiple processing owners for one job"
        )
    connection.executescript(
        """
        BEGIN IMMEDIATE;

        CREATE UNIQUE INDEX idx_annotation_task_links_processing_job
        ON annotation_task_links (job_id)
        WHERE link_kind = 'processing';

        CREATE TABLE annotation_migration_safety (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending_integrity_check', 'verified')
            ),
            verified_at TEXT,
            CHECK (
                (
                    status = 'pending_integrity_check'
                    AND verified_at IS NULL
                )
                OR (
                    status = 'verified'
                    AND verified_at IS NOT NULL
                )
            )
        );
        INSERT INTO annotation_migration_safety (
            singleton, schema_version, status, verified_at
        ) VALUES (
            1, 5, 'pending_integrity_check', NULL
        );
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
    (4, "annotation_m2_domain", _migration_004_annotation_m2_domain),
    (
        5,
        "processing_owner_and_safety_marker",
        _migration_005_processing_owner_and_safety_marker,
    ),
)

from __future__ import annotations

import sqlite3


LATEST_TRAINING_SCHEMA_VERSION = 13


def apply_training_migrations(connection: sqlite3.Connection, *, applied_at: str) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS training_schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"""
    )
    versions = [int(row[0]) for row in connection.execute(
        "SELECT version FROM training_schema_migrations ORDER BY version"
    )]
    if versions and versions[-1] > LATEST_TRAINING_SCHEMA_VERSION:
        raise RuntimeError("training database schema is newer than this application")
    if versions != list(range(1, (versions[-1] if versions else 0) + 1)):
        raise RuntimeError("training database has a non-contiguous migration ledger")
    if 1 not in versions:
        connection.executescript(_MIGRATION_001)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(1,?,?)",
            ("training_platform_m1", applied_at),
        )
        connection.commit()
        versions.append(1)
    if 2 not in versions:
        connection.executescript(_MIGRATION_002)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(2,?,?)",
            ("training_nodes_m2", applied_at),
        )
        connection.commit()
        versions.append(2)
    if 3 not in versions:
        connection.executescript(_MIGRATION_003)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(3,?,?)",
            ("training_node_deployment_m3", applied_at),
        )
        connection.commit()
        versions.append(3)
    if 4 not in versions:
        connection.executescript(_MIGRATION_004)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(4,?,?)",
            ("model_families_m4", applied_at),
        )
        connection.commit()
        versions.append(4)
    if 5 not in versions:
        connection.executescript(_MIGRATION_005)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(5,?,?)",
            ("model_worker_verification_m5", applied_at),
        )
        connection.commit()
        versions.append(5)
    if 6 not in versions:
        connection.executescript(_MIGRATION_006)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(6,?,?)",
            ("training_node_revision_split_m6", applied_at),
        )
        connection.commit()
        versions.append(6)
    if 7 not in versions:
        connection.executescript(_MIGRATION_007)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(7,?,?)",
            ("training_workflows_m7", applied_at),
        )
        connection.commit()
        versions.append(7)
    if 8 not in versions:
        connection.executescript(_MIGRATION_008)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(8,?,?)",
            ("training_node_deletion_history_m8", applied_at),
        )
        connection.commit()
        versions.append(8)
    if 9 not in versions:
        connection.executescript(_MIGRATION_009)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(9,?,?)",
            ("training_datasets_m9", applied_at),
        )
        connection.commit()
        versions.append(9)
    if 10 not in versions:
        connection.executescript(_MIGRATION_010)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(10,?,?)",
            ("training_node_command_claim_tokens_m10", applied_at),
        )
        connection.commit()
        versions.append(10)
    if 11 not in versions:
        connection.executescript(_MIGRATION_011)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(11,?,?)",
            ("dataset_transfer_pause_cancel_m11", applied_at),
        )
        connection.commit()
        versions.append(11)
    if 12 not in versions:
        connection.executescript(_MIGRATION_012)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(12,?,?)",
            ("real_training_execution_m12", applied_at),
        )
        connection.commit()
        versions.append(12)
    if 13 not in versions:
        connection.executescript(_MIGRATION_013)
        connection.execute(
            "INSERT INTO training_schema_migrations(version,name,applied_at) VALUES(13,?,?)",
            ("model_version_library_m13", applied_at),
        )
        connection.commit()


_MIGRATION_001 = """
BEGIN IMMEDIATE;
CREATE TABLE registered_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_ref TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','verified','disabled')) DEFAULT 'draft',
  current_revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE model_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_ref TEXT NOT NULL UNIQUE,
  model_id INTEGER NOT NULL,
  revision_number INTEGER NOT NULL,
  working_directory TEXT NOT NULL,
  entrypoint TEXT NOT NULL,
  fixed_argv_json TEXT NOT NULL,
  output_template TEXT NOT NULL,
  parameter_definitions_json TEXT NOT NULL,
  launch_template_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(model_id, revision_number),
  FOREIGN KEY(model_id) REFERENCES registered_models(id)
);
CREATE TABLE training_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_ref TEXT NOT NULL UNIQUE,
  model_id INTEGER NOT NULL,
  model_revision_id INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK(mode='simulation'),
  server_ref TEXT NOT NULL,
  gpu_uuids_json TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  run_spec_json TEXT NOT NULL,
  command_preview TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','preparing','running','stop_requested','succeeded','failed','cancelled','lost')),
  state_revision INTEGER NOT NULL DEFAULT 0,
  seed INTEGER NOT NULL,
  total_steps INTEGER NOT NULL,
  current_step INTEGER NOT NULL DEFAULT 0,
  owner_id TEXT,
  owner_epoch INTEGER NOT NULL DEFAULT 0,
  lease_expires_at TEXT,
  heartbeat_at TEXT,
  failure_code TEXT,
  failure_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(model_id) REFERENCES registered_models(id),
  FOREIGN KEY(model_revision_id) REFERENCES model_revisions(id)
);
CREATE INDEX idx_training_runs_status_created ON training_runs(status, id);
CREATE TABLE gpu_leases (
  gpu_uuid TEXT PRIMARY KEY,
  run_id INTEGER NOT NULL,
  acquired_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES training_runs(id)
);
CREATE TABLE port_leases (
  server_ref TEXT NOT NULL,
  master_port INTEGER NOT NULL,
  run_id INTEGER NOT NULL UNIQUE,
  acquired_at TEXT NOT NULL,
  PRIMARY KEY(server_ref, master_port),
  FOREIGN KEY(run_id) REFERENCES training_runs(id)
);
CREATE TABLE run_logs (
  run_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, seq),
  FOREIGN KEY(run_id) REFERENCES training_runs(id)
);
CREATE TABLE metric_samples (
  run_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  step INTEGER NOT NULL,
  total_steps INTEGER NOT NULL,
  epoch REAL NOT NULL,
  loss REAL NOT NULL,
  learning_rate REAL NOT NULL,
  grad_norm REAL NOT NULL,
  elapsed_seconds REAL NOT NULL,
  gpu_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, seq),
  FOREIGN KEY(run_id) REFERENCES training_runs(id)
);
CREATE TABLE training_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  run_ref TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE training_idempotency (
  scope TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  response_ref TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(scope,idempotency_key)
);
COMMIT;
"""


_MIGRATION_002 = """
BEGIN IMMEDIATE;
CREATE TABLE training_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_ref TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL,
  ssh_port INTEGER NOT NULL CHECK(ssh_port BETWEEN 1 AND 65535),
  ssh_username TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'pending_enrollment','online','degraded','offline','repair_required','disabled'
  )) DEFAULT 'pending_enrollment',
  state_revision INTEGER NOT NULL DEFAULT 1,
  enrolled_at TEXT,
  last_heartbeat_at TEXT,
  worker_instance_id TEXT,
  worker_version TEXT,
  protocol_version INTEGER,
  worker_token_sha256 TEXT,
  health_message TEXT,
  capabilities_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_training_nodes_status ON training_nodes(status, id);
CREATE TABLE training_node_enrollment_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_ref TEXT NOT NULL UNIQUE,
  node_id INTEGER NOT NULL,
  token_sha256 TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  consumed_at TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE CASCADE
);
CREATE INDEX idx_training_node_enrollment_tokens_node
  ON training_node_enrollment_tokens(node_id, id);
CREATE TABLE training_node_resource_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id INTEGER NOT NULL,
  captured_at TEXT NOT NULL,
  resources_json TEXT NOT NULL,
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE CASCADE
);
CREATE INDEX idx_training_node_resource_snapshots_node
  ON training_node_resource_snapshots(node_id, id DESC);
COMMIT;
"""


_MIGRATION_003 = """
BEGIN IMMEDIATE;
ALTER TABLE training_nodes ADD COLUMN host_key_algorithm TEXT;
ALTER TABLE training_nodes ADD COLUMN host_public_key TEXT;
ALTER TABLE training_nodes ADD COLUMN host_key_fingerprint TEXT;
ALTER TABLE training_nodes ADD COLUMN deployment_status TEXT NOT NULL DEFAULT 'not_started'
  CHECK(deployment_status IN ('not_started','deploying','succeeded','failed'));
ALTER TABLE training_nodes ADD COLUMN deployment_message TEXT;
ALTER TABLE training_nodes ADD COLUMN deployment_started_at TEXT;
ALTER TABLE training_nodes ADD COLUMN deployment_finished_at TEXT;
ALTER TABLE training_nodes ADD COLUMN installed_worker_version TEXT;
COMMIT;
"""


_MIGRATION_004 = """
BEGIN IMMEDIATE;
CREATE TABLE model_families (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  family_ref TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
ALTER TABLE registered_models ADD COLUMN family_id INTEGER REFERENCES model_families(id);
ALTER TABLE registered_models ADD COLUMN version_number INTEGER;
ALTER TABLE registered_models ADD COLUMN based_on_model_id INTEGER REFERENCES registered_models(id);
ALTER TABLE registered_models ADD COLUMN version_description TEXT;
ALTER TABLE registered_models ADD COLUMN configuration_locked_at TEXT;
INSERT INTO model_families(family_ref,name,created_at,updated_at)
SELECT 'family_' || model_ref,name,created_at,updated_at
FROM registered_models ORDER BY id;
UPDATE registered_models
SET family_id=(
      SELECT family.id FROM model_families AS family
      WHERE family.family_ref='family_' || registered_models.model_ref
    ),
    version_number=1,
    version_description=NULLIF(description,''),
    configuration_locked_at=CASE
      WHEN EXISTS(
        SELECT 1 FROM training_runs AS run
        WHERE run.model_id=registered_models.id
      ) THEN updated_at
      ELSE NULL
    END;
CREATE UNIQUE INDEX uq_registered_models_family_version
  ON registered_models(family_id,version_number);
CREATE INDEX idx_registered_models_family
  ON registered_models(family_id,version_number DESC);
COMMIT;
"""


_MIGRATION_005 = """
BEGIN IMMEDIATE;
CREATE TABLE model_verification_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  verification_ref TEXT NOT NULL UNIQUE,
  model_id INTEGER NOT NULL,
  model_revision_id INTEGER NOT NULL,
  node_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
  request_json TEXT NOT NULL,
  result_json TEXT,
  worker_instance_id TEXT,
  lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(model_id) REFERENCES registered_models(id),
  FOREIGN KEY(model_revision_id) REFERENCES model_revisions(id),
  FOREIGN KEY(node_id) REFERENCES training_nodes(id)
);
CREATE INDEX idx_model_verification_node_status
  ON model_verification_requests(node_id,status,id);
CREATE INDEX idx_model_verification_model
  ON model_verification_requests(model_id,id DESC);
COMMIT;
"""


_MIGRATION_006 = """
BEGIN IMMEDIATE;
ALTER TABLE training_nodes ADD COLUMN heartbeat_revision INTEGER NOT NULL DEFAULT 0;
UPDATE training_nodes
SET heartbeat_revision=CASE WHEN last_heartbeat_at IS NULL THEN 0 ELSE 1 END;
COMMIT;
"""


_MIGRATION_007 = """
BEGIN IMMEDIATE;
DELETE FROM gpu_leases;
DELETE FROM port_leases;
DELETE FROM run_logs;
DELETE FROM metric_samples;
DELETE FROM training_events;
DELETE FROM training_idempotency WHERE scope IN ('create_run','stop_run');
DELETE FROM model_verification_requests;
DELETE FROM training_runs;

ALTER TABLE model_families ADD COLUMN current_model_id INTEGER REFERENCES registered_models(id);
UPDATE model_families
SET current_model_id=(
  SELECT model.id FROM registered_models AS model
  WHERE model.family_id=model_families.id
  ORDER BY model.version_number DESC,model.id DESC LIMIT 1
);
UPDATE registered_models SET based_on_model_id=NULL;
DELETE FROM model_revisions
WHERE model_id NOT IN (
  SELECT current_model_id FROM model_families WHERE current_model_id IS NOT NULL
);
DELETE FROM registered_models
WHERE id NOT IN (
  SELECT current_model_id FROM model_families WHERE current_model_id IS NOT NULL
);
DELETE FROM model_revisions
WHERE id NOT IN (
  SELECT revision.id
  FROM registered_models AS model
  JOIN model_revisions AS revision
    ON revision.model_id=model.id
   AND revision.revision_number=model.current_revision
);
UPDATE model_revisions SET revision_number=1;
UPDATE registered_models
SET status='draft',configuration_locked_at=NULL,based_on_model_id=NULL,
    current_revision=1,version_number=1,version_description=NULL;

CREATE TABLE model_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_ref TEXT NOT NULL UNIQUE,
  family_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL UNIQUE,
  version_number INTEGER NOT NULL,
  version_date TEXT NOT NULL,
  version_label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(family_id,version_number),
  FOREIGN KEY(family_id) REFERENCES model_families(id),
  FOREIGN KEY(run_id) REFERENCES training_runs(id)
);
CREATE INDEX idx_model_versions_family ON model_versions(family_id,version_number DESC);

CREATE TABLE training_stages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stage_ref TEXT NOT NULL UNIQUE,
  run_id INTEGER NOT NULL,
  stage_number INTEGER NOT NULL,
  stage_name TEXT NOT NULL,
  stage_input_source TEXT NOT NULL CHECK(stage_input_source IN ('manual','previous_stage_output')),
  parameters_json TEXT NOT NULL,
  run_spec_json TEXT NOT NULL,
  command_preview TEXT NOT NULL,
  output_directory TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','preparing','running','succeeded','failed','cancelled','skipped','lost')),
  current_step INTEGER NOT NULL DEFAULT 0,
  total_steps INTEGER NOT NULL,
  failure_code TEXT,
  failure_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(run_id,stage_number),
  FOREIGN KEY(run_id) REFERENCES training_runs(id)
);
CREATE INDEX idx_training_stages_run ON training_stages(run_id,stage_number);

ALTER TABLE run_logs ADD COLUMN stage_id INTEGER REFERENCES training_stages(id);
ALTER TABLE metric_samples ADD COLUMN stage_id INTEGER REFERENCES training_stages(id);

CREATE TABLE training_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_ref TEXT NOT NULL UNIQUE,
  version_id INTEGER NOT NULL,
  stage_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('stage_output','version_model')),
  path TEXT NOT NULL,
  simulated INTEGER NOT NULL CHECK(simulated IN (0,1)),
  created_at TEXT NOT NULL,
  FOREIGN KEY(version_id) REFERENCES model_versions(id),
  FOREIGN KEY(stage_id) REFERENCES training_stages(id)
);
CREATE INDEX idx_training_artifacts_version ON training_artifacts(version_id,id);
COMMIT;
"""


_MIGRATION_008 = """
BEGIN IMMEDIATE;
ALTER TABLE model_verification_requests
  RENAME TO model_verification_requests_m7;
CREATE TABLE model_verification_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  verification_ref TEXT NOT NULL UNIQUE,
  model_id INTEGER NOT NULL,
  model_revision_id INTEGER NOT NULL,
  node_id INTEGER,
  node_ref_snapshot TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
  request_json TEXT NOT NULL,
  result_json TEXT,
  worker_instance_id TEXT,
  lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(model_id) REFERENCES registered_models(id),
  FOREIGN KEY(model_revision_id) REFERENCES model_revisions(id),
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE SET NULL
);
INSERT INTO model_verification_requests(
  id,verification_ref,model_id,model_revision_id,node_id,node_ref_snapshot,
  status,request_json,result_json,worker_instance_id,lease_expires_at,
  created_at,started_at,finished_at,updated_at
)
SELECT
  verification.id,verification.verification_ref,verification.model_id,
  verification.model_revision_id,verification.node_id,node.node_ref,
  verification.status,verification.request_json,verification.result_json,
  verification.worker_instance_id,verification.lease_expires_at,
  verification.created_at,verification.started_at,verification.finished_at,
  verification.updated_at
FROM model_verification_requests_m7 AS verification
JOIN training_nodes AS node ON node.id=verification.node_id;
DROP TABLE model_verification_requests_m7;
CREATE INDEX idx_model_verification_node_status
  ON model_verification_requests(node_id,status,id);
CREATE INDEX idx_model_verification_model
  ON model_verification_requests(model_id,id DESC);
COMMIT;
"""


_MIGRATION_009 = """
BEGIN IMMEDIATE;

ALTER TABLE model_revisions ADD COLUMN data_access_mode TEXT NOT NULL
  DEFAULT 'self_managed'
  CHECK(data_access_mode IN ('datapilot_managed','self_managed'));
ALTER TABLE model_versions ADD COLUMN description TEXT;

CREATE TABLE dataset_source_manifests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  manifest_ref TEXT NOT NULL UNIQUE,
  release_ref TEXT NOT NULL UNIQUE,
  domain TEXT NOT NULL,
  dataset_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('preparing','building','ready','failed')),
  source_root TEXT,
  inventory_json TEXT,
  inventory_sha256 TEXT,
  file_count INTEGER NOT NULL DEFAULT 0 CHECK(file_count >= 0),
  total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes >= 0),
  error_code TEXT,
  error_message TEXT,
  preparation_lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_dataset_source_manifests_date
  ON dataset_source_manifests(dataset_date, id);

CREATE TABLE dataset_source_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  manifest_id INTEGER NOT NULL,
  file_ref TEXT NOT NULL UNIQUE,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  relative_path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
  sha256 TEXT NOT NULL,
  FOREIGN KEY(manifest_id) REFERENCES dataset_source_manifests(id) ON DELETE CASCADE,
  UNIQUE(manifest_id,ordinal),
  UNIQUE(manifest_id,relative_path)
);
CREATE INDEX idx_dataset_source_files_page
  ON dataset_source_files(manifest_id,ordinal);

CREATE TABLE training_node_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command_ref TEXT NOT NULL UNIQUE,
  node_id INTEGER,
  node_ref_snapshot TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN (
    'list_directories','transfer_dataset','cancel_dataset_transfer',
    'remove_dataset_replica'
  )),
  status TEXT NOT NULL CHECK(status IN (
    'queued','running','succeeded','failed','cancelled'
  )),
  request_json TEXT NOT NULL,
  result_json TEXT,
  worker_instance_id TEXT,
  lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE SET NULL
);
CREATE INDEX idx_training_node_commands_claim
  ON training_node_commands(node_id,status,id);

CREATE TABLE dataset_transfers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  transfer_ref TEXT NOT NULL UNIQUE,
  node_id INTEGER,
  node_ref_snapshot TEXT NOT NULL,
  source_manifest_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'preparing','queued','running','cancel_requested','succeeded','failed','cancelled'
  )),
  target_parent_directory TEXT NOT NULL,
  final_directory TEXT NOT NULL,
  bytes_transferred INTEGER NOT NULL DEFAULT 0 CHECK(bytes_transferred >= 0),
  files_completed INTEGER NOT NULL DEFAULT 0 CHECK(files_completed >= 0),
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE SET NULL,
  FOREIGN KEY(source_manifest_id) REFERENCES dataset_source_manifests(id)
);
CREATE INDEX idx_dataset_transfers_node_status
  ON dataset_transfers(node_id,status,id DESC);

CREATE TABLE dataset_replicas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  replica_ref TEXT NOT NULL UNIQUE,
  node_id INTEGER,
  node_ref_snapshot TEXT NOT NULL,
  source_manifest_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ready','removing','failed')),
  local_root TEXT NOT NULL,
  inventory_sha256 TEXT NOT NULL,
  file_count INTEGER NOT NULL CHECK(file_count >= 0),
  total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE SET NULL,
  FOREIGN KEY(source_manifest_id) REFERENCES dataset_source_manifests(id),
  UNIQUE(node_id,source_manifest_id)
);
CREATE INDEX idx_dataset_replicas_node_status
  ON dataset_replicas(node_id,status,id DESC);

CREATE TABLE dataset_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_ref TEXT NOT NULL UNIQUE,
  run_id INTEGER NOT NULL UNIQUE,
  manifest_json TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES training_runs(id)
);

COMMIT;
"""


_MIGRATION_010 = """
BEGIN IMMEDIATE;
ALTER TABLE training_node_commands ADD COLUMN claim_token_sha256 TEXT;
COMMIT;
"""


_MIGRATION_011 = """
BEGIN IMMEDIATE;
ALTER TABLE dataset_transfers RENAME TO dataset_transfers_m10;
CREATE TABLE dataset_transfers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  transfer_ref TEXT NOT NULL UNIQUE,
  node_id INTEGER,
  node_ref_snapshot TEXT NOT NULL,
  source_manifest_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'preparing','queued','running','pause_requested','paused',
    'cancel_requested','succeeded','failed','cancelled'
  )),
  target_parent_directory TEXT NOT NULL,
  final_directory TEXT NOT NULL,
  bytes_transferred INTEGER NOT NULL DEFAULT 0 CHECK(bytes_transferred >= 0),
  files_completed INTEGER NOT NULL DEFAULT 0 CHECK(files_completed >= 0),
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE SET NULL,
  FOREIGN KEY(source_manifest_id) REFERENCES dataset_source_manifests(id)
);
INSERT INTO dataset_transfers(
  id,transfer_ref,node_id,node_ref_snapshot,source_manifest_id,status,
  target_parent_directory,final_directory,bytes_transferred,files_completed,
  error_code,error_message,created_at,updated_at,started_at,finished_at
)
SELECT
  id,transfer_ref,node_id,node_ref_snapshot,source_manifest_id,
  CASE status
    WHEN 'cancel_requested' THEN 'pause_requested'
    WHEN 'cancelled' THEN 'paused'
    ELSE status
  END,
  target_parent_directory,final_directory,bytes_transferred,files_completed,
  error_code,error_message,created_at,updated_at,started_at,finished_at
FROM dataset_transfers_m10;
DROP TABLE dataset_transfers_m10;
CREATE INDEX idx_dataset_transfers_node_status
  ON dataset_transfers(node_id,status,id DESC);
COMMIT;
"""


_MIGRATION_012 = """
BEGIN IMMEDIATE;

-- ``mode`` is retained as the v1 compatibility column whose original CHECK
-- only admitted simulation.  ``execution_mode`` is the authoritative field
-- from v12 onward and lets existing databases migrate without rebuilding the
-- heavily referenced run table.
ALTER TABLE training_runs ADD COLUMN execution_mode TEXT NOT NULL
  DEFAULT 'simulation' CHECK(execution_mode IN ('simulation','real'));
ALTER TABLE training_runs ADD COLUMN execution_control_status TEXT
  CHECK(execution_control_status IN ('connected','unreachable','unresolved'));
ALTER TABLE training_runs ADD COLUMN execution_worker_instance_id TEXT;
ALTER TABLE training_runs ADD COLUMN execution_owner_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE training_runs ADD COLUMN execution_update_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE training_runs ADD COLUMN execution_last_heartbeat_at TEXT;

ALTER TABLE metric_samples ADD COLUMN metric_payload_json TEXT;

CREATE TABLE training_execution_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_ref TEXT NOT NULL UNIQUE,
  run_id INTEGER NOT NULL,
  stage_id INTEGER,
  node_ref_snapshot TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('start_training_stage','stop_training_run')),
  status TEXT NOT NULL CHECK(status IN (
    'queued','running','succeeded','failed','cancelled'
  )),
  request_json TEXT NOT NULL,
  result_json TEXT,
  worker_instance_id TEXT,
  claim_token_sha256 TEXT,
  lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(run_id) REFERENCES training_runs(id),
  FOREIGN KEY(stage_id) REFERENCES training_stages(id)
);
CREATE INDEX idx_training_execution_actions_claim
  ON training_execution_actions(node_ref_snapshot,status,kind,id);
CREATE INDEX idx_training_execution_actions_run
  ON training_execution_actions(run_id,id);

CREATE TABLE training_checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  checkpoint_ref TEXT NOT NULL UNIQUE,
  version_id INTEGER NOT NULL,
  stage_id INTEGER NOT NULL,
  relative_path TEXT NOT NULL,
  step INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(stage_id,relative_path),
  FOREIGN KEY(version_id) REFERENCES model_versions(id),
  FOREIGN KEY(stage_id) REFERENCES training_stages(id)
);
CREATE INDEX idx_training_checkpoints_version
  ON training_checkpoints(version_id,id);

CREATE TABLE training_run_log_storage (
  run_id INTEGER PRIMARY KEY,
  stored_lines INTEGER NOT NULL DEFAULT 0 CHECK(stored_lines >= 0),
  stored_bytes INTEGER NOT NULL DEFAULT 0 CHECK(stored_bytes >= 0),
  truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0,1)),
  updated_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES training_runs(id) ON DELETE CASCADE
);

COMMIT;
"""


_MIGRATION_013 = """
BEGIN IMMEDIATE;

-- SQLite cannot extend a CHECK constraint in place. Rebuild the generic
-- Worker command table while preserving queued/running commands and claims.
DROP INDEX idx_training_node_commands_claim;
ALTER TABLE training_node_commands RENAME TO training_node_commands_m12;
CREATE TABLE training_node_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command_ref TEXT NOT NULL UNIQUE,
  node_id INTEGER,
  node_ref_snapshot TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN (
    'list_directories','transfer_dataset','cancel_dataset_transfer',
    'remove_dataset_replica','inspect_training_artifact'
  )),
  status TEXT NOT NULL CHECK(status IN (
    'queued','running','succeeded','failed','cancelled'
  )),
  request_json TEXT NOT NULL,
  result_json TEXT,
  worker_instance_id TEXT,
  lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  claim_token_sha256 TEXT,
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE SET NULL
);
INSERT INTO training_node_commands(
  id,command_ref,node_id,node_ref_snapshot,kind,status,request_json,result_json,
  worker_instance_id,lease_expires_at,created_at,started_at,finished_at,
  updated_at,claim_token_sha256
)
SELECT id,command_ref,node_id,node_ref_snapshot,kind,status,request_json,result_json,
  worker_instance_id,lease_expires_at,created_at,started_at,finished_at,
  updated_at,claim_token_sha256
FROM training_node_commands_m12;
DROP TABLE training_node_commands_m12;
CREATE INDEX idx_training_node_commands_claim
  ON training_node_commands(node_id,status,id);

CREATE TABLE training_artifact_inspections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inspection_ref TEXT NOT NULL UNIQUE,
  artifact_id INTEGER NOT NULL,
  node_id INTEGER,
  command_ref TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN (
    'queued','running','succeeded','failed'
  )),
  availability TEXT NOT NULL CHECK(availability IN (
    'unchecked','available','missing','unreadable','unsafe','check_failed'
  )),
  file_count INTEGER CHECK(file_count IS NULL OR file_count >= 0),
  total_bytes INTEGER CHECK(total_bytes IS NULL OR total_bytes >= 0),
  error_code TEXT,
  error_message TEXT,
  requested_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(artifact_id) REFERENCES training_artifacts(id) ON DELETE CASCADE,
  FOREIGN KEY(node_id) REFERENCES training_nodes(id) ON DELETE SET NULL,
  FOREIGN KEY(command_ref) REFERENCES training_node_commands(command_ref)
);
CREATE INDEX idx_training_artifact_inspections_artifact
  ON training_artifact_inspections(artifact_id,id DESC);
CREATE UNIQUE INDEX idx_training_artifact_inspections_active
  ON training_artifact_inspections(artifact_id)
  WHERE status IN ('queued','running');

CREATE INDEX idx_metric_samples_run_stage_seq
  ON metric_samples(run_id,stage_id,seq DESC);

COMMIT;
"""

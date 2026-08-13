from __future__ import annotations

import sqlite3


LATEST_TRAINING_SCHEMA_VERSION = 4


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

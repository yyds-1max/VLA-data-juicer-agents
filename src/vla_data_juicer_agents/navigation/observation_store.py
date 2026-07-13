from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from pydantic import TypeAdapter

from vla_data_juicer_agents.navigation.observation_models import (
    EvidenceDescriptor,
    EvidenceWrite,
    NavigationObservationRevision,
    ObservationKind,
    ObservationPayload,
    UserGuidanceObservation,
)
from vla_data_juicer_agents.navigation.task_state import utc_now
from vla_data_juicer_agents.navigation.schema import initialize_navigation_schema
from vla_data_juicer_agents.navigation.task_store import (
    NavigationTaskStateRevisionError,
    authorize_navigation_task_write,
)


_OBSERVATION_PAYLOAD_ADAPTER = TypeAdapter(ObservationPayload)


class ObservationRollbackCleanupError(RuntimeError):
    def __init__(
        self,
        original_error: Exception,
        cleanup_errors: list[Exception],
    ) -> None:
        self.original_error = original_error
        self.cleanup_errors = tuple(cleanup_errors)
        super().__init__(
            "observation append failed and evidence rollback cleanup failed: "
            f"{len(cleanup_errors)} cleanup error(s)"
        )


class NavigationEvidenceWriter(Protocol):
    def write(
        self,
        task_id: str,
        observation_revision: int,
        kind: str,
        source_tool: str,
        payload: dict[str, Any] | list[Any],
        summary: str,
    ) -> EvidenceDescriptor: ...

    def delete(self, task_id: str, ref: str) -> None: ...


class SqliteNavigationObservationStore:
    def __init__(self, db_path: str | Path, *, initialize: bool = True) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_schema(self) -> None:
        initialize_navigation_schema(self.db_path)

    def append(
        self,
        task_id: str,
        completed_kind: ObservationKind,
        payloads: list[ObservationPayload],
        evidence_writes: list[EvidenceWrite],
        evidence_store: NavigationEvidenceWriter,
        *,
        expected_web_session_id: str,
        expected_agentscope_session_id: str,
    ) -> NavigationObservationRevision:
        incoming_payloads = [
            _OBSERVATION_PAYLOAD_ADAPTER.validate_python(payload) for payload in payloads
        ]
        writes = [EvidenceWrite.model_validate(write) for write in evidence_writes]
        connection = self._connect()
        written_descriptors: list[EvidenceDescriptor] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            authorize_navigation_task_write(
                connection,
                task_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
            row = connection.execute(
                """
                SELECT revision_json
                FROM navigation_observation_revisions
                WHERE task_id = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            previous = (
                NavigationObservationRevision.model_validate_json(row["revision_json"])
                if row is not None
                else None
            )
            revision_number = 1 if previous is None else previous.revision + 1
            completed_kinds = list(previous.completed_kinds) if previous is not None else []
            if completed_kind not in completed_kinds:
                completed_kinds.append(completed_kind)
            accumulated_payloads = self._merge_payloads(previous, incoming_payloads)
            revision = NavigationObservationRevision(
                task_id=task_id,
                revision=revision_number,
                completed_kinds=completed_kinds,
                payloads=accumulated_payloads,
            )

            for write in writes:
                descriptor = evidence_store.write(
                    task_id,
                    revision_number,
                    write.kind,
                    write.source_tool,
                    write.payload,
                    write.summary,
                )
                written_descriptors.append(descriptor)
            revision = NavigationObservationRevision.model_validate(
                {
                    **revision.model_dump(mode="json"),
                    "evidence_refs": [descriptor.ref for descriptor in written_descriptors],
                }
            )
            connection.execute(
                """
                INSERT INTO navigation_observation_revisions (
                    task_id, revision, revision_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    task_id,
                    revision_number,
                    self._canonical_json(revision.model_dump(mode="json")),
                    revision.created_at,
                ),
            )
            for descriptor in written_descriptors:
                connection.execute(
                    """
                    INSERT INTO navigation_evidence (
                        ref, task_id, observation_revision, kind, summary,
                        byte_size, source_tool, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor.ref,
                        descriptor.task_id,
                        descriptor.observation_revision,
                        descriptor.kind,
                        descriptor.summary,
                        descriptor.byte_size,
                        descriptor.source_tool,
                        descriptor.created_at,
                    ),
                )
            connection.commit()
            return revision
        except Exception as append_error:
            connection.rollback()
            cleanup_errors: list[Exception] = []
            for descriptor in reversed(written_descriptors):
                try:
                    evidence_store.delete(task_id, descriptor.ref)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ObservationRollbackCleanupError(
                    append_error,
                    cleanup_errors,
                ) from append_error
            raise
        finally:
            connection.close()

    def append_user_guidance(
        self,
        task_id: str,
        *,
        text: str,
        scene_mode: str | None,
        expected_state_revision: int,
        evidence_store: NavigationEvidenceWriter,
        expected_web_session_id: str,
        expected_agentscope_session_id: str,
    ) -> tuple[int, NavigationObservationRevision]:
        """Advance guidance state and its evidence-bearing observation atomically."""
        connection = self._connect()
        written_descriptors: list[EvidenceDescriptor] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            authorize_navigation_task_write(
                connection,
                task_id,
                expected_web_session_id=expected_web_session_id,
                expected_agentscope_session_id=expected_agentscope_session_id,
            )
            task_row = connection.execute(
                """SELECT state_revision, guidance_revision
                   FROM navigation_tasks WHERE task_id = ?""",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(task_id)
            if int(task_row["state_revision"]) != expected_state_revision:
                raise NavigationTaskStateRevisionError(
                    "navigation task state revision changed"
                )
            guidance_revision = int(task_row["guidance_revision"]) + 1
            cursor = connection.execute(
                """UPDATE navigation_tasks
                   SET guidance_revision = ?,
                       scene_mode = COALESCE(?, scene_mode),
                       updated_at = ?, state_revision = state_revision + 1
                   WHERE task_id = ? AND state_revision = ?""",
                (
                    guidance_revision,
                    scene_mode,
                    utc_now(),
                    task_id,
                    expected_state_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise NavigationTaskStateRevisionError(
                    "navigation task state revision changed"
                )

            payload = UserGuidanceObservation(
                guidance_revision=guidance_revision,
                text=text,
            )
            previous_row = connection.execute(
                """SELECT revision_json
                   FROM navigation_observation_revisions
                   WHERE task_id = ?
                   ORDER BY revision DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            previous = (
                NavigationObservationRevision.model_validate_json(
                    previous_row["revision_json"]
                )
                if previous_row is not None
                else None
            )
            revision_number = 1 if previous is None else previous.revision + 1
            completed_kinds = (
                list(previous.completed_kinds) if previous is not None else []
            )
            if "user_guidance" not in completed_kinds:
                completed_kinds.append("user_guidance")
            accumulated_payloads = self._merge_payloads(previous, [payload])
            write = EvidenceWrite(
                kind="user_guidance",
                source_tool="record_navigation_user_guidance_tool",
                payload=payload.model_dump(mode="json"),
                summary=f"navigation user guidance revision {guidance_revision}",
            )
            descriptor = evidence_store.write(
                task_id,
                revision_number,
                write.kind,
                write.source_tool,
                write.payload,
                write.summary,
            )
            written_descriptors.append(descriptor)
            revision = NavigationObservationRevision(
                task_id=task_id,
                revision=revision_number,
                completed_kinds=completed_kinds,
                payloads=accumulated_payloads,
                evidence_refs=[descriptor.ref],
            )
            connection.execute(
                """INSERT INTO navigation_observation_revisions (
                       task_id, revision, revision_json, created_at
                   ) VALUES (?, ?, ?, ?)""",
                (
                    task_id,
                    revision_number,
                    self._canonical_json(revision.model_dump(mode="json")),
                    revision.created_at,
                ),
            )
            connection.execute(
                """INSERT INTO navigation_evidence (
                       ref, task_id, observation_revision, kind, summary,
                       byte_size, source_tool, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    descriptor.ref,
                    descriptor.task_id,
                    descriptor.observation_revision,
                    descriptor.kind,
                    descriptor.summary,
                    descriptor.byte_size,
                    descriptor.source_tool,
                    descriptor.created_at,
                ),
            )
            connection.commit()
            return guidance_revision, revision
        except Exception as append_error:
            connection.rollback()
            cleanup_errors: list[Exception] = []
            for descriptor in reversed(written_descriptors):
                try:
                    evidence_store.delete(task_id, descriptor.ref)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ObservationRollbackCleanupError(
                    append_error,
                    cleanup_errors,
                ) from append_error
            raise
        finally:
            connection.close()

    def latest(self, task_id: str) -> NavigationObservationRevision | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT revision_json
                FROM navigation_observation_revisions
                WHERE task_id = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return self._revision_from_row(row)

    def get(self, task_id: str, revision: int) -> NavigationObservationRevision | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT revision_json
                FROM navigation_observation_revisions
                WHERE task_id = ? AND revision = ?
                """,
                (task_id, revision),
            ).fetchone()
        return self._revision_from_row(row)

    def list_evidence(
        self,
        task_id: str,
        *,
        kind: str | None = None,
        observation_revision: int | None = None,
        cursor: int = 0,
        limit: int = 100,
    ) -> list[EvidenceDescriptor]:
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        clauses = ["task_id = ?"]
        params: list[Any] = [task_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if observation_revision is not None:
            clauses.append("observation_revision = ?")
            params.append(observation_revision)
        params.extend([limit, cursor])
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT ref, task_id, observation_revision, kind, summary,
                       byte_size, source_tool, created_at
                FROM navigation_evidence
                WHERE {' AND '.join(clauses)}
                ORDER BY observation_revision ASC, rowid ASC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [EvidenceDescriptor.model_validate(dict(row)) for row in rows]

    @staticmethod
    def _merge_payloads(
        previous: NavigationObservationRevision | None,
        incoming: list[ObservationPayload],
    ) -> list[ObservationPayload]:
        merged = list(previous.payloads) if previous is not None else []
        indexes = {payload.kind: index for index, payload in enumerate(merged)}
        for payload in incoming:
            index = indexes.get(payload.kind)
            if index is None:
                indexes[payload.kind] = len(merged)
                merged.append(payload)
            else:
                merged[index] = payload
        return merged

    @staticmethod
    def _canonical_json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _revision_from_row(row: sqlite3.Row | None) -> NavigationObservationRevision | None:
        if row is None:
            return None
        return NavigationObservationRevision.model_validate_json(row["revision_json"])

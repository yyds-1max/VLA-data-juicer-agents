from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.navigation.context_budget import (
    ensure_payload_within_limit,
    serialized_chars,
)
from vla_data_juicer_agents.navigation.observation_models import EvidenceDescriptor


EVIDENCE_READ_MAX_CHARS = 5_500
_REF_PREFIX = "nav-evidence:"
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_EVIDENCE_ID = re.compile(r"^[0-9a-f]{32}$")


class FileNavigationEvidenceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write(
        self,
        task_id: str,
        observation_revision: int,
        kind: str,
        source_tool: str,
        payload: dict[str, Any] | list[Any],
        summary: str,
        *,
        ref: str | None = None,
    ) -> EvidenceDescriptor:
        if ref is None:
            ref = self._encode_ref(task_id, observation_revision, uuid4().hex)
        return self.write_with_ref(
            task_id,
            observation_revision,
            kind,
            source_tool,
            payload,
            summary,
            ref=ref,
        )

    def deterministic_ref(
        self,
        task_id: str,
        observation_revision: int,
        identity: str,
    ) -> str:
        self._validate_task_id(task_id)
        if observation_revision < 1:
            raise ValueError("observation_revision must be at least 1")
        if not isinstance(identity, str) or not identity:
            raise ValueError("identity must be a non-empty string")
        evidence_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return self._encode_ref(task_id, observation_revision, evidence_id)

    def write_with_ref(
        self,
        task_id: str,
        observation_revision: int,
        kind: str,
        source_tool: str,
        payload: dict[str, Any] | list[Any],
        summary: str,
        *,
        ref: str,
    ) -> EvidenceDescriptor:
        self._validate_task_id(task_id)
        if observation_revision < 1:
            raise ValueError("observation_revision must be at least 1")
        indexed_task, indexed_revision, evidence_id = self._decode_ref(ref)
        if indexed_task != task_id:
            raise PermissionError("evidence ref belongs to another task")
        if indexed_revision != observation_revision:
            raise PermissionError("evidence ref belongs to another observation revision")
        destination = self.root / task_id / str(observation_revision) / f"{evidence_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise ValueError("evidence ref already contains a different payload")
        else:
            self._atomic_write(destination, encoded)
        created_at = datetime.now(UTC).isoformat()
        return EvidenceDescriptor(
            ref=ref,
            task_id=task_id,
            observation_revision=observation_revision,
            kind=kind,
            summary=summary,
            byte_size=len(encoded),
            source_tool=source_tool,
            created_at=created_at,
        )

    def read(
        self,
        task_id: str,
        ref: str,
        *,
        fields: list[str] | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        path = self._path_for_owned_ref(task_id, ref)
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if not path.is_file():
            raise KeyError(ref)

        payload = json.loads(path.read_text(encoding="utf-8"))
        selected = self._select_fields(payload, fields)
        data, next_cursor = self._paginate(selected, cursor=cursor, limit=limit)
        response = {"data": data, "next_cursor": next_cursor}
        return ensure_payload_within_limit(
            response,
            max_chars=EVIDENCE_READ_MAX_CHARS,
            label="evidence_read",
        )

    def delete(self, task_id: str, ref: str) -> None:
        path = self._path_for_owned_ref(task_id, ref)
        path.unlink(missing_ok=True)

    def exists(self, task_id: str, ref: str) -> bool:
        return self._path_for_owned_ref(task_id, ref).is_file()

    @staticmethod
    def _select_fields(payload: Any, fields: list[str] | None) -> Any:
        if fields is None:
            return payload
        if not isinstance(payload, dict):
            raise ValueError("fields can only be selected from object evidence")
        return {field: payload[field] for field in fields if field in payload}

    @staticmethod
    def _paginate(payload: Any, *, cursor: int, limit: int) -> tuple[Any, int | None]:
        list_extent = FileNavigationEvidenceStore._list_extent(payload)
        if list_extent is None:
            response = {"data": payload, "next_cursor": None}
            ensure_payload_within_limit(
                response,
                max_chars=EVIDENCE_READ_MAX_CHARS,
                label="evidence_read",
            )
            return payload, None

        if cursor >= list_extent:
            data = FileNavigationEvidenceStore._slice_page(payload, cursor, cursor)
            response = {"data": data, "next_cursor": None}
            ensure_payload_within_limit(
                response,
                max_chars=EVIDENCE_READ_MAX_CHARS,
                label="evidence_read",
            )
            return data, None

        max_count = min(limit, list_extent - cursor)
        best: tuple[Any, int | None] | None = None
        for count in range(1, max_count + 1):
            end = cursor + count
            data = FileNavigationEvidenceStore._slice_page(payload, cursor, end)
            next_cursor = end if end < list_extent else None
            response = {"data": data, "next_cursor": next_cursor}
            if serialized_chars(response) <= EVIDENCE_READ_MAX_CHARS:
                best = (data, next_cursor)
        if best is None:
            raise ValueError(
                f"evidence item at cursor {cursor} exceeds "
                f"{EVIDENCE_READ_MAX_CHARS}-character response budget"
            )
        return best

    @staticmethod
    def _list_extent(payload: Any) -> int | None:
        if isinstance(payload, list):
            return len(payload)
        if not isinstance(payload, dict):
            return None
        lengths = [len(value) for value in payload.values() if isinstance(value, list)]
        return max(lengths) if lengths else None

    @staticmethod
    def _slice_page(payload: Any, start: int, end: int) -> Any:
        if isinstance(payload, list):
            return payload[start:end]
        return {
            field: value[start:end] if isinstance(value, list) else value
            for field, value in payload.items()
        }

    @staticmethod
    def _atomic_write(destination: Path, encoded: bytes) -> None:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.stem}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(encoded)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            temp_path.replace(destination)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _encode_ref(task_id: str, revision: int, evidence_id: str) -> str:
        payload = json.dumps(
            {"evidence_id": evidence_id, "revision": revision, "task_id": task_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"{_REF_PREFIX}{token}"

    @classmethod
    def _decode_ref(cls, ref: str) -> tuple[str, int, str]:
        if not ref.startswith(_REF_PREFIX):
            raise KeyError(ref)
        token = ref.removeprefix(_REF_PREFIX)
        try:
            padded = token + "=" * (-len(token) % 4)
            decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
            metadata = json.loads(decoded)
            if set(metadata) != {"evidence_id", "revision", "task_id"}:
                raise ValueError
            indexed_task = metadata["task_id"]
            revision = metadata["revision"]
            evidence_id = metadata["evidence_id"]
            cls._validate_task_id(indexed_task)
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ValueError
            if not isinstance(evidence_id, str) or not _EVIDENCE_ID.fullmatch(evidence_id):
                raise ValueError
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise KeyError(ref) from exc
        return indexed_task, revision, evidence_id

    def _path_for_owned_ref(self, task_id: str, ref: str) -> Path:
        self._validate_task_id(task_id)
        indexed_task, revision, evidence_id = self._decode_ref(ref)
        if indexed_task != task_id:
            raise PermissionError("evidence ref belongs to another task")
        return self.root / indexed_task / str(revision) / f"{evidence_id}.json"

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or not _SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError("task_id contains unsupported path characters")

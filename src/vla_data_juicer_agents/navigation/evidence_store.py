from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.navigation.context_budget import ensure_payload_within_limit
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
    ) -> EvidenceDescriptor:
        self._validate_task_id(task_id)
        if observation_revision < 1:
            raise ValueError("observation_revision must be at least 1")

        evidence_id = uuid4().hex
        destination = self.root / task_id / str(observation_revision) / f"{evidence_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._atomic_write(destination, encoded)
        created_at = datetime.now(UTC).isoformat()
        return EvidenceDescriptor(
            ref=self._encode_ref(task_id, observation_revision, evidence_id),
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

    @staticmethod
    def _select_fields(payload: Any, fields: list[str] | None) -> Any:
        if fields is None:
            return payload
        if not isinstance(payload, dict):
            raise ValueError("fields can only be selected from object evidence")
        return {field: payload[field] for field in fields if field in payload}

    @staticmethod
    def _paginate(payload: Any, *, cursor: int, limit: int) -> tuple[Any, int | None]:
        end = cursor + limit
        if isinstance(payload, list):
            return payload[cursor:end], end if end < len(payload) else None
        if not isinstance(payload, dict):
            return payload, None

        page: dict[str, Any] = {}
        has_more = False
        for field, value in payload.items():
            if isinstance(value, list):
                page[field] = value[cursor:end]
                has_more = has_more or end < len(value)
            else:
                page[field] = value
        return page, end if has_more else None

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

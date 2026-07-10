from __future__ import annotations

import json
from typing import Any


def serialized_chars(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def ensure_payload_within_limit(
    payload: dict[str, Any], *, max_chars: int, label: str
) -> dict[str, Any]:
    size = serialized_chars(payload)
    if size > max_chars:
        raise ValueError(f"{label} exceeds {max_chars} characters: {size}")
    return payload

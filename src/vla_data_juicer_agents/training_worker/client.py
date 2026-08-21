from __future__ import annotations

from dataclasses import dataclass
import json
import platform
import ssl
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .datasets import DatasetFileChunk
from .identity import WorkerIdentity


class CenterClientError(RuntimeError):
    """Safe transport error which never includes request credentials or bodies."""


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    node_ref: str
    worker_token: str


class WorkerCenterClient(Protocol):
    def enroll(
        self,
        identity: WorkerIdentity,
        enrollment_token: str,
        capabilities: Mapping[str, object],
    ) -> EnrollmentResult: ...

    def publish_heartbeat(
        self,
        identity: WorkerIdentity,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def publish_command_result(
        self,
        identity: WorkerIdentity,
        command_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def poll_commands(
        self,
        identity: WorkerIdentity,
        *,
        wait_seconds: int = 25,
        limit: int = 1,
    ) -> Mapping[str, object]: ...

    def poll_training_actions(
        self,
        identity: WorkerIdentity,
        *,
        wait_seconds: int = 25,
        limit: int = 1,
    ) -> Mapping[str, object]: ...

    def publish_training_action_result(
        self,
        identity: WorkerIdentity,
        action_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def publish_run_updates(
        self,
        identity: WorkerIdentity,
        run_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def fetch_dataset_manifest_page(
        self,
        release_ref: str,
        *,
        cursor: int | None,
        limit: int,
    ) -> Mapping[str, object]: ...

    def fetch_dataset_file_chunk(
        self,
        file_ref: str,
        *,
        offset: int,
        max_bytes: int,
    ) -> DatasetFileChunk: ...


class NoRedirectHandler(HTTPRedirectHandler):
    """Reject every redirect so credentials cannot move to another origin."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class HttpCenterClient:
    """Fixed-origin client for Worker control and managed dataset downloads."""

    def __init__(
        self,
        *,
        center_base_url: str,
        worker_token: str | None = None,
        node_ref: str | None = None,
        timeout_seconds: float = 10.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.center_base_url = _validated_base_url(center_base_url)
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("timeout_seconds must be between 0 and 120")
        self.timeout_seconds = timeout_seconds
        self.worker_token = worker_token
        self.node_ref = node_ref
        handlers: list[object] = [NoRedirectHandler()]
        if self.center_base_url.startswith("https://"):
            handlers.append(HTTPSHandler(context=ssl_context or ssl.create_default_context()))
        self._opener = build_opener(*handlers)

    def enroll(
        self,
        identity: WorkerIdentity,
        enrollment_token: str,
        capabilities: Mapping[str, object],
    ) -> EnrollmentResult:
        response = self._post_json(
            "/api/training/nodes/enroll",
            {
                "enrollment_token": enrollment_token,
                "worker_instance_id": identity.worker_id,
                "worker_version": _worker_version(),
                "protocol_version": 1,
                "capabilities": dict(capabilities),
            },
        )
        worker_token = response.get("worker_token")
        node = response.get("node")
        node_ref = node.get("node_ref") if isinstance(node, dict) else None
        if (
            not isinstance(worker_token, str)
            or not worker_token.startswith("worker_")
            or not isinstance(node_ref, str)
            or not node_ref
        ):
            raise CenterClientError("center returned an invalid enrollment response")
        self.worker_token = worker_token
        self.node_ref = node_ref
        return EnrollmentResult(node_ref=node_ref, worker_token=worker_token)

    def publish_heartbeat(
        self,
        identity: WorkerIdentity,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not self.worker_token or not self.node_ref:
            raise CenterClientError("worker is not enrolled")
        health = payload.get("health")
        health_status = health.get("status") if isinstance(health, dict) else None
        if health_status not in {"healthy", "degraded", "repair_required"}:
            raise CenterClientError("heartbeat health status is invalid")
        resources = payload.get("resources")
        if not isinstance(resources, dict):
            raise CenterClientError("heartbeat resources are missing")
        capabilities = _capability_payload(resources)
        request_payload = {
            "worker_instance_id": identity.worker_id,
            "worker_version": _worker_version(),
            "protocol_version": 1,
            "health": health_status,
            "health_message": _health_message(resources),
            "capabilities": capabilities,
            "resources": _control_plane_resources(resources),
        }
        return self._post_json(
            f"/api/training/nodes/{quote(self.node_ref, safe='')}/heartbeat",
            request_payload,
            bearer_token=self.worker_token,
        )

    def publish_command_result(
        self,
        identity: WorkerIdentity,
        command_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not self.worker_token or not self.node_ref:
            raise CenterClientError("worker is not enrolled")
        if not command_ref or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for character in command_ref):
            raise CenterClientError("worker command reference is invalid")
        return self._post_json(
            f"/api/training/nodes/{quote(self.node_ref, safe='')}/commands/{quote(command_ref, safe='')}/result",
            {"worker_instance_id": identity.worker_id, **dict(payload)},
            bearer_token=self.worker_token,
        )

    def poll_commands(
        self,
        identity: WorkerIdentity,
        *,
        wait_seconds: int = 25,
        limit: int = 1,
    ) -> Mapping[str, object]:
        if not self.worker_token or not self.node_ref:
            raise CenterClientError("worker is not enrolled")
        if wait_seconds < 0 or wait_seconds > 30 or limit < 1 or limit > 10:
            raise CenterClientError("worker command poll options are invalid")
        return self._post_json(
            f"/api/training/nodes/{quote(self.node_ref, safe='')}/commands/poll",
            {
                "worker_instance_id": identity.worker_id,
                "wait_seconds": wait_seconds,
                "limit": limit,
            },
            bearer_token=self.worker_token,
            timeout_seconds=max(self.timeout_seconds, float(wait_seconds + 5)),
        )

    def poll_training_actions(
        self,
        identity: WorkerIdentity,
        *,
        wait_seconds: int = 25,
        limit: int = 1,
    ) -> Mapping[str, object]:
        if not self.worker_token or not self.node_ref:
            raise CenterClientError("worker is not enrolled")
        if wait_seconds < 0 or wait_seconds > 30 or limit < 1 or limit > 10:
            raise CenterClientError("worker training action poll options are invalid")
        return self._post_json(
            f"/api/training/nodes/{quote(self.node_ref, safe='')}/training-actions/poll",
            {
                "worker_instance_id": identity.worker_id,
                "wait_seconds": wait_seconds,
                "limit": limit,
            },
            bearer_token=self.worker_token,
            timeout_seconds=max(self.timeout_seconds, float(wait_seconds + 5)),
        )

    def publish_training_action_result(
        self,
        identity: WorkerIdentity,
        action_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not self.worker_token or not self.node_ref:
            raise CenterClientError("worker is not enrolled")
        action_ref = _validated_wire_ref(action_ref)
        return self._post_json(
            f"/api/training/nodes/{quote(self.node_ref, safe='')}/training-actions/"
            f"{quote(action_ref, safe='')}/result",
            {"worker_instance_id": identity.worker_id, **dict(payload)},
            bearer_token=self.worker_token,
        )

    def publish_run_updates(
        self,
        identity: WorkerIdentity,
        run_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not self.worker_token or not self.node_ref:
            raise CenterClientError("worker is not enrolled")
        run_ref = _validated_wire_ref(run_ref)
        return self._post_json(
            f"/api/training/nodes/{quote(self.node_ref, safe='')}/runs/"
            f"{quote(run_ref, safe='')}/updates",
            {"worker_instance_id": identity.worker_id, **dict(payload)},
            bearer_token=self.worker_token,
        )

    def fetch_dataset_manifest_page(
        self,
        release_ref: str,
        *,
        cursor: int | None,
        limit: int,
    ) -> Mapping[str, object]:
        if not self.worker_token or not self.node_ref:
            raise CenterClientError("worker is not enrolled")
        if limit < 1 or limit > 1000:
            raise CenterClientError("dataset manifest page limit is invalid")
        query: dict[str, object] = {"limit": limit}
        if cursor is not None:
            if isinstance(cursor, bool) or cursor < 0:
                raise CenterClientError("dataset manifest cursor is invalid")
            query["cursor"] = cursor
        path = (
            f"/api/training/nodes/{quote(self.node_ref, safe='')}/dataset-releases/"
            f"{quote(_validated_wire_ref(release_ref), safe='')}/manifest?{urlencode(query)}"
        )
        return self._get_json(path, bearer_token=self.worker_token)

    def fetch_dataset_file_chunk(
        self,
        file_ref: str,
        *,
        offset: int,
        max_bytes: int,
    ) -> DatasetFileChunk:
        if not self.worker_token or not self.node_ref:
            raise CenterClientError("worker is not enrolled")
        if offset < 0 or max_bytes < 1 or max_bytes > 16 * 1024 * 1024:
            raise CenterClientError("dataset file range is invalid")
        path = (
            f"/api/training/nodes/{quote(self.node_ref, safe='')}/dataset-files/"
            f"{quote(_validated_wire_ref(file_ref), safe='')}/content"
        )
        request = Request(
            self.center_base_url + path,
            method="GET",
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {self.worker_token}",
                "Range": f"bytes={offset}-{offset + max_bytes - 1}",
                "User-Agent": f"datapilot-training-worker/{_worker_version()}",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw_body = response.read(max_bytes + 1)
                content_range = response.headers.get("Content-Range")
        except HTTPError as exc:
            raise CenterClientError(f"center rejected request with HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise CenterClientError(f"center request failed ({type(exc).__name__})") from None
        if len(raw_body) > max_bytes:
            raise CenterClientError("center returned too much dataset file data")
        if status == 200 and offset == 0:
            total_size = len(raw_body)
            return DatasetFileChunk(raw_body, 0, total_size, True)
        if status != 206 or not isinstance(content_range, str):
            raise CenterClientError("center returned an invalid dataset file range")
        parsed_range = _parse_content_range(content_range)
        if parsed_range is None:
            raise CenterClientError("center returned an invalid dataset file range")
        start, end, total_size = parsed_range
        if start != offset or end - start + 1 != len(raw_body):
            raise CenterClientError("center returned an invalid dataset file range")
        return DatasetFileChunk(raw_body, start, total_size, end + 1 >= total_size)

    def _post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        bearer_token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        request = Request(
            self.center_base_url + path,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"datapilot-training-worker/{_worker_version()}",
                **(
                    {"Authorization": f"Bearer {bearer_token}"}
                    if bearer_token is not None
                    else {}
                ),
            },
        )
        try:
            with self._opener.open(
                request,
                timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            ) as response:
                raw_body = response.read(1024 * 1024 + 1)
        except HTTPError as exc:
            raise CenterClientError(f"center rejected request with HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise CenterClientError(f"center request failed ({type(exc).__name__})") from None
        if len(raw_body) > 1024 * 1024:
            raise CenterClientError("center response exceeded the size limit")
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CenterClientError("center returned an invalid JSON response") from None
        if not isinstance(parsed, dict):
            raise CenterClientError("center response must be a JSON object")
        return parsed

    def _get_json(
        self,
        path: str,
        *,
        bearer_token: str,
    ) -> dict[str, Any]:
        request = Request(
            self.center_base_url + path,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {bearer_token}",
                "User-Agent": f"datapilot-training-worker/{_worker_version()}",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read(4 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise CenterClientError(f"center rejected request with HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise CenterClientError(f"center request failed ({type(exc).__name__})") from None
        if len(raw_body) > 4 * 1024 * 1024:
            raise CenterClientError("center response exceeded the size limit")
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CenterClientError("center returned an invalid JSON response") from None
        if not isinstance(parsed, dict):
            raise CenterClientError("center response must be a JSON object")
        return parsed


class OfflineCenterClient:
    """No-network mode for local inventory inspection."""

    def enroll(
        self,
        identity: WorkerIdentity,
        enrollment_token: str,
        capabilities: Mapping[str, object],
    ) -> EnrollmentResult:
        raise CenterClientError("offline worker cannot enroll")

    def publish_heartbeat(
        self,
        identity: WorkerIdentity,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {}

    def publish_command_result(
        self,
        identity: WorkerIdentity,
        command_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {}

    def poll_commands(
        self,
        identity: WorkerIdentity,
        *,
        wait_seconds: int = 25,
        limit: int = 1,
    ) -> Mapping[str, object]:
        return {"commands": []}

    def poll_training_actions(
        self,
        identity: WorkerIdentity,
        *,
        wait_seconds: int = 25,
        limit: int = 1,
    ) -> Mapping[str, object]:
        return {"actions": []}

    def publish_training_action_result(
        self,
        identity: WorkerIdentity,
        action_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {}

    def publish_run_updates(
        self,
        identity: WorkerIdentity,
        run_ref: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {}

    def fetch_dataset_manifest_page(
        self,
        release_ref: str,
        *,
        cursor: int | None,
        limit: int,
    ) -> Mapping[str, object]:
        raise CenterClientError("offline worker cannot read dataset manifests")

    def fetch_dataset_file_chunk(
        self,
        file_ref: str,
        *,
        offset: int,
        max_bytes: int,
    ) -> DatasetFileChunk:
        raise CenterClientError("offline worker cannot download dataset files")


def capability_payload(resources: Mapping[str, object]) -> dict[str, object]:
    return _capability_payload(resources)


def _capability_payload(resources: Mapping[str, object]) -> dict[str, object]:
    host = resources.get("host")
    if not isinstance(host, dict):
        raise CenterClientError("host capabilities are missing")
    operating_system = " ".join(
        value for value in (host.get("os"), host.get("os_release")) if isinstance(value, str)
    )
    return {
        "hostname": host.get("hostname") or "unknown",
        "operating_system": operating_system or "unknown",
        "architecture": host.get("architecture") or "unknown",
        "python_version": platform.python_version(),
        "nvidia_driver_version": None,
        "cuda_version": None,
        "conda_environments": [],
        "training_execution_v1": True,
        "training_artifact_inspection_v1": True,
        "worker_features": [
            "resource_inventory",
            "restart_reconciliation",
            "model_configuration_verification",
            "directory_browser_v1",
            "dataset_transfer_v1",
            "training_execution_v1",
            "training_artifact_inspection_v1",
        ],
    }


def _control_plane_resources(resources: Mapping[str, object]) -> dict[str, object]:
    return {
        "cpu": resources["cpu"],
        "memory": resources["memory"],
        "disks": resources["disks"],
        "gpus": resources["gpus"],
    }


def _health_message(resources: Mapping[str, object]) -> str | None:
    collection = resources.get("gpu_collection")
    error = collection.get("error") if isinstance(collection, dict) else None
    if not isinstance(error, str):
        return None
    return error[:1000]


def _validated_base_url(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError("center_base_url contains control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("center_base_url must use https or http")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("center_base_url must have a hostname and no user information")
    if parsed.query or parsed.fragment:
        raise ValueError("center_base_url must not have a query or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _worker_version() -> str:
    try:
        from importlib.metadata import version

        return version("vla-data-juicer-agents")
    except Exception:
        return "0.1.0"


def _validated_wire_ref(value: str) -> str:
    if (
        not value
        or len(value) > 255
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
            for character in value
        )
    ):
        raise CenterClientError("worker dataset reference is invalid")
    return value


def _parse_content_range(value: str) -> tuple[int, int, int] | None:
    if not value.startswith("bytes ") or "/" not in value or "-" not in value:
        return None
    try:
        byte_range, raw_total = value[6:].split("/", 1)
        raw_start, raw_end = byte_range.split("-", 1)
        start, end, total = int(raw_start), int(raw_end), int(raw_total)
    except (TypeError, ValueError):
        return None
    if start < 0 or end < start or total <= end:
        return None
    return start, end, total

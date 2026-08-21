from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import math
import re
from typing import Annotated, Any, Callable, Literal, Protocol

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    SecretStr,
    field_validator,
    model_validator,
)

from vla_data_juicer_agents.training.auth import (
    TrainingPrincipal,
    TrainingSettings,
)


IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
JsonScalar = StrictBool | StrictInt | StrictFloat | str
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SHELL_META = re.compile(r"[;&|`$<>]")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


class TrainingServiceProtocol(Protocol):
    def capabilities(self, principal: TrainingPrincipal) -> dict[str, Any]: ...

    def list_servers(self) -> list[dict[str, Any]]: ...

    def get_server_resources(self, server_ref: str) -> dict[str, Any]: ...

    def list_nodes(self) -> list[dict[str, Any]]: ...

    def get_node(self, node_ref: str) -> dict[str, Any]: ...

    def create_node(
        self, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def update_node(
        self, node_ref: str, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def delete_node(
        self, node_ref: str, expected_revision: int, principal: TrainingPrincipal
    ) -> None: ...

    def create_enrollment_token(
        self, node_ref: str, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def discover_node_host_key(
        self, node_ref: str, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def deploy_node_worker(
        self, node_ref: str, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def preflight_node_worker(
        self, node_ref: str, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def enroll_node(self, payload: Any) -> dict[str, Any]: ...

    def heartbeat_node(
        self, node_ref: str, worker_token: str, payload: Any
    ) -> dict[str, Any]: ...

    def finish_model_verification(
        self,
        node_ref: str,
        command_ref: str,
        worker_token: str,
        payload: Any,
    ) -> dict[str, Any]: ...

    def poll_node_commands(
        self, node_ref: str, worker_token: str, payload: Any
    ) -> dict[str, Any]: ...

    def finish_node_command(
        self, node_ref: str, command_ref: str, worker_token: str, payload: Any
    ) -> dict[str, Any]: ...

    def get_node_resources(self, node_ref: str) -> dict[str, Any]: ...

    def list_dataset_releases(self) -> list[dict[str, Any]]: ...
    def list_dataset_replicas(self, node_ref: str, principal: TrainingPrincipal) -> list[dict[str, Any]]: ...
    def request_directory_listing(self, node_ref: str, payload: Any, principal: TrainingPrincipal) -> dict[str, Any]: ...
    def get_directory_listing(self, listing_ref: str, principal: TrainingPrincipal) -> dict[str, Any]: ...
    def create_dataset_transfers(self, payload: Any, idempotency_key: str, principal: TrainingPrincipal) -> list[dict[str, Any]]: ...
    def list_dataset_transfers(self, *, node_ref: str | None = None, status: str | None = None, principal: TrainingPrincipal) -> list[dict[str, Any]]: ...
    def get_dataset_transfer(self, transfer_ref: str, principal: TrainingPrincipal) -> dict[str, Any]: ...
    def pause_dataset_transfer(self, transfer_ref: str, principal: TrainingPrincipal) -> dict[str, Any]: ...
    def cancel_dataset_transfer(self, transfer_ref: str, principal: TrainingPrincipal) -> dict[str, Any]: ...
    def retry_dataset_transfer(self, transfer_ref: str, idempotency_key: str, principal: TrainingPrincipal) -> dict[str, Any]: ...
    def remove_dataset_replica(self, replica_ref: str, principal: TrainingPrincipal) -> dict[str, Any]: ...
    def source_manifest_page(self, node_ref: str, release_ref: str, worker_token: str, *, cursor: int, limit: int) -> dict[str, Any]: ...
    def source_file(self, node_ref: str, file_ref: str, worker_token: str) -> dict[str, Any]: ...
    def poll_training_actions(self, node_ref: str, worker_token: str, payload: Any) -> dict[str, Any]: ...
    def finish_training_action(self, node_ref: str, action_ref: str, worker_token: str, payload: Any) -> dict[str, Any]: ...
    def update_training_run(self, node_ref: str, run_ref: str, worker_token: str, payload: Any) -> dict[str, Any]: ...

    def list_models(
        self, *, include_private: bool = False
    ) -> list[dict[str, Any]]: ...

    def get_model(
        self, family_ref: str, *, include_private: bool = False
    ) -> dict[str, Any]: ...

    def list_model_version_families(
        self, *, query: str | None, after: str | None, limit: int,
        principal: TrainingPrincipal,
    ) -> dict[str, Any]: ...

    def list_model_versions(
        self, family_ref: str, *, after: str | None, limit: int,
        principal: TrainingPrincipal,
    ) -> dict[str, Any]: ...

    def get_model_version(
        self, version_ref: str, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def request_model_version_artifact_inspection(
        self, version_ref: str, idempotency_key: str,
        principal: TrainingPrincipal,
    ) -> dict[str, Any]: ...

    def create_model(
        self, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def update_model(
        self, family_ref: str, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def verify_model(
        self, family_ref: str, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def preview_run(
        self, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def create_run(
        self,
        payload: Any,
        idempotency_key: str,
        principal: TrainingPrincipal,
    ) -> dict[str, Any]: ...

    def list_runs(
        self,
        *,
        status: str | None,
        query: str | None,
        after: str | None,
        limit: int,
    ) -> dict[str, Any]: ...

    def get_run(self, run_ref: str) -> dict[str, Any]: ...

    def stop_run(
        self,
        run_ref: str,
        expected_revision: int,
        idempotency_key: str,
        principal: TrainingPrincipal,
    ) -> dict[str, Any]: ...

    def list_logs(
        self, run_ref: str, *, after_seq: int, limit: int, stage_ref: str | None = None,
        before_seq: int | None = None, tail: bool = False,
        levels: tuple[str, ...] = (), query: str | None = None,
    ) -> dict[str, Any]: ...

    def list_metrics(
        self, run_ref: str, *, after_seq: int, limit: int, stage_ref: str | None = None,
        tail: bool = False, since: str | None = None,
    ) -> dict[str, Any]: ...

    def list_events(self, *, after_seq: int, limit: int) -> dict[str, Any]: ...


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTrainingNodeRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    address: str = Field(
        min_length=1,
        max_length=254,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,253}$",
    )
    ssh_port: Annotated[int, Field(strict=True, ge=1, le=65535)] = 22

    @field_validator("name")
    @classmethod
    def validate_node_name(cls, value: str) -> str:
        if any(
            character in value for character in ("\x00", "\r")
        ):
            raise ValueError("node text contains control characters")
        normalized = value.strip()
        if not normalized:
            raise ValueError("node name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def validate_node_description(cls, value: str | None) -> str | None:
        if value is not None and any(
            character in value for character in ("\x00", "\r")
        ):
            raise ValueError("node text contains control characters")
        return value.strip() if value is not None else None


class UpdateTrainingNodeRequest(StrictRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    address: str | None = Field(
        default=None,
        min_length=1,
        max_length=254,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,253}$",
    )
    ssh_port: Annotated[int, Field(strict=True, ge=1, le=65535)] | None = None
    desired_state: Literal["offline", "repair_required", "disabled"] | None = None

    @field_validator("name")
    @classmethod
    def validate_node_name(cls, value: str | None) -> str | None:
        if value is not None and any(
            character in value for character in ("\x00", "\r")
        ):
            raise ValueError("node text contains control characters")
        normalized = value.strip() if value is not None else None
        if normalized == "":
            raise ValueError("node name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def validate_node_description(cls, value: str | None) -> str | None:
        if value is not None and any(
            character in value for character in ("\x00", "\r")
        ):
            raise ValueError("node text contains control characters")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> UpdateTrainingNodeRequest:
        if all(
            getattr(self, field) is None
            for field in (
                "name",
                "description",
                "address",
                "ssh_port",
                "desired_state",
            )
        ):
            raise ValueError("at least one node field must be changed")
        return self


class CreateEnrollmentTokenRequest(StrictRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    expires_in_seconds: Annotated[
        int, Field(strict=True, ge=60, le=3600)
    ] = 600


class ConfirmedHostKey(StrictRequest):
    algorithm: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9@._+-]+$",
    )
    public_key: str = Field(
        min_length=20,
        max_length=16384,
        pattern=r"^[A-Za-z0-9+/=]+$",
    )
    sha256_fingerprint: str = Field(
        pattern=r"^SHA256:[A-Za-z0-9+/]{43}$"
    )


class DeployTrainingNodeWorkerRequest(StrictRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    ssh_username: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$",
    )
    confirmed_host_key: ConfirmedHostKey
    host_key_confirmed: Literal[True]
    ssh_password: SecretStr
    sudo_password_mode: Literal["same_as_ssh", "separate", "not_required"] = (
        "same_as_ssh"
    )
    sudo_password: SecretStr | None = None

    @model_validator(mode="after")
    def validate_credentials(self) -> DeployTrainingNodeWorkerRequest:
        ssh_password = self.ssh_password.get_secret_value()
        if (
            not ssh_password
            or len(ssh_password) > 1024
            or any(character in ssh_password for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("SSH credential has an unsupported format")
        if self.sudo_password_mode == "separate":
            if self.sudo_password is None:
                raise ValueError("a separate sudo password is required")
            sudo_password = self.sudo_password.get_secret_value()
            if (
                not sudo_password
                or len(sudo_password) > 1024
                or any(character in sudo_password for character in ("\x00", "\n", "\r"))
            ):
                raise ValueError("sudo credential has an unsupported format")
        elif self.sudo_password is not None:
            raise ValueError("sudo_password is only valid in separate mode")
        return self


class RemoveTrainingNodeWorkerRequest(StrictRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    ssh_username: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$",
    )
    ssh_password: SecretStr
    sudo_password_mode: Literal["same_as_ssh", "separate", "not_required"] = (
        "same_as_ssh"
    )
    sudo_password: SecretStr | None = None

    @model_validator(mode="after")
    def validate_credentials(self) -> RemoveTrainingNodeWorkerRequest:
        ssh_password = self.ssh_password.get_secret_value()
        if (
            not ssh_password
            or len(ssh_password) > 1024
            or any(character in ssh_password for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("SSH credential has an unsupported format")
        if self.sudo_password_mode == "separate":
            if self.sudo_password is None:
                raise ValueError("a separate sudo password is required")
            sudo_password = self.sudo_password.get_secret_value()
            if (
                not sudo_password
                or len(sudo_password) > 1024
                or any(character in sudo_password for character in ("\x00", "\n", "\r"))
            ):
                raise ValueError("sudo credential has an unsupported format")
        elif self.sudo_password is not None:
            raise ValueError("sudo_password is only valid in separate mode")
        return self


class NodeCapabilities(StrictRequest):
    hostname: str = Field(min_length=1, max_length=255)
    operating_system: str = Field(min_length=1, max_length=255)
    architecture: str = Field(min_length=1, max_length=64)
    python_version: str | None = Field(default=None, max_length=64)
    nvidia_driver_version: str | None = Field(default=None, max_length=64)
    cuda_version: str | None = Field(default=None, max_length=64)
    conda_environments: list[str] = Field(default_factory=list, max_length=100)
    worker_features: list[str] = Field(default_factory=list, max_length=100)
    training_execution_v1: bool = False
    training_artifact_inspection_v1: bool = False

    @field_validator(
        "hostname",
        "operating_system",
        "architecture",
        "python_version",
        "nvidia_driver_version",
        "cuda_version",
    )
    @classmethod
    def safe_capability_text(cls, value: str | None) -> str | None:
        if value is not None and any(
            character in value for character in ("\x00", "\n", "\r")
        ):
            raise ValueError("capability text must be a single line")
        return value

    @field_validator("conda_environments", "worker_features")
    @classmethod
    def safe_capability_list(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not value
            or len(value) > 255
            or any(character in value for character in ("\x00", "\n", "\r"))
            for value in values
        ):
            raise ValueError("capability list contains invalid or duplicate values")
        return values


class CpuResources(StrictRequest):
    logical_cores: Annotated[int, Field(strict=True, ge=1, le=4096)]
    load_1m: Annotated[float, Field(strict=True, ge=0, le=1_000_000)] | None = None


class MemoryResources(StrictRequest):
    total_bytes: Annotated[int, Field(strict=True, ge=1)]
    available_bytes: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def available_cannot_exceed_total(self) -> MemoryResources:
        if self.available_bytes > self.total_bytes:
            raise ValueError("available memory cannot exceed total memory")
        return self


class DiskResources(StrictRequest):
    mount: str = Field(min_length=1, max_length=1024)
    total_bytes: Annotated[int, Field(strict=True, ge=1)]
    available_bytes: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("mount")
    @classmethod
    def safe_mount(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("mount must be a single line")
        return value

    @model_validator(mode="after")
    def available_cannot_exceed_total(self) -> DiskResources:
        if self.available_bytes > self.total_bytes:
            raise ValueError("available disk cannot exceed total disk")
        return self


class GpuResources(StrictRequest):
    uuid: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    index: Annotated[int, Field(strict=True, ge=0, le=1024)]
    name: str = Field(min_length=1, max_length=255)
    memory_total_bytes: Annotated[int, Field(strict=True, ge=1)]
    memory_used_bytes: Annotated[int, Field(strict=True, ge=0)]
    utilization_percent: Annotated[
        float, Field(strict=True, ge=0, le=100)
    ]
    temperature_celsius: Annotated[
        float, Field(strict=True, ge=-100, le=250)
    ] | None = None

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("GPU name must be a single line")
        return value

    @model_validator(mode="after")
    def used_cannot_exceed_total(self) -> GpuResources:
        if self.memory_used_bytes > self.memory_total_bytes:
            raise ValueError("used GPU memory cannot exceed total GPU memory")
        return self


class NodeResources(StrictRequest):
    cpu: CpuResources
    memory: MemoryResources
    disks: list[DiskResources] = Field(default_factory=list, max_length=128)
    gpus: list[GpuResources] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def unique_gpu_identity(self) -> NodeResources:
        uuids = [gpu.uuid for gpu in self.gpus]
        indexes = [gpu.index for gpu in self.gpus]
        if len(uuids) != len(set(uuids)) or len(indexes) != len(set(indexes)):
            raise ValueError("GPU UUIDs and indexes must be unique")
        return self


class EnrollTrainingNodeRequest(StrictRequest):
    enrollment_token: str = Field(
        min_length=40,
        max_length=256,
        pattern=r"^enroll_[A-Za-z0-9_-]+$",
    )
    worker_instance_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    worker_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$",
    )
    protocol_version: Literal[1]
    capabilities: NodeCapabilities


class TrainingNodeHeartbeatRequest(StrictRequest):
    worker_instance_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    worker_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$",
    )
    protocol_version: Literal[1]
    health: Literal["healthy", "degraded", "repair_required"]
    health_message: str | None = Field(default=None, max_length=1000)
    capabilities: NodeCapabilities | None = None
    resources: NodeResources

    @field_validator("health_message")
    @classmethod
    def safe_health_message(cls, value: str | None) -> str | None:
        if value is not None and any(
            character in value for character in ("\x00", "\r")
        ):
            raise ValueError("health message contains control characters")
        return value


class ModelVerificationCheck(StrictRequest):
    code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=120)
    status: Literal["passed", "warning", "failed"]
    detail: str = Field(min_length=1, max_length=1000)

    @field_validator("label", "detail")
    @classmethod
    def safe_text(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\r")):
            raise ValueError("verification text contains control characters")
        return value


class ModelVerificationResultRequest(StrictRequest):
    worker_instance_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    status: Literal["succeeded", "failed"]
    checks: list[ModelVerificationCheck] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def status_matches_checks(self) -> ModelVerificationResultRequest:
        has_failure = any(check.status == "failed" for check in self.checks)
        if (self.status == "succeeded" and has_failure) or (
            self.status == "failed" and not has_failure
        ):
            raise ValueError("verification status must match its checks")
        return self


class PollNodeCommandsRequest(StrictRequest):
    worker_instance_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    )
    wait_seconds: Annotated[int, Field(strict=True, ge=0, le=30)] = 25
    limit: Annotated[int, Field(strict=True, ge=1, le=10)] = 1


class WorkerCommandResultRequest(StrictRequest):
    worker_instance_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    )
    claim_token: str = Field(
        min_length=32,
        max_length=200,
        pattern=r"^claim_[A-Za-z0-9_-]+$",
    )
    status: Literal["running", "succeeded", "failed", "paused", "cancelled"]
    transfer_ref: str | None = Field(default=None, max_length=200)
    replica_ref: str | None = Field(default=None, max_length=200)
    path: str | None = Field(default=None, max_length=4096)
    parent_path: str | None = Field(default=None, max_length=4096)
    writable: bool | None = None
    free_bytes: Annotated[int, Field(strict=True, ge=0)] | None = None
    directories: list[dict[str, Any]] | None = Field(default=None, max_length=10000)
    progress: dict[str, Any] | None = None
    replica: dict[str, Any] | None = None
    artifact_ref: str | None = Field(default=None, max_length=200)
    version_ref: str | None = Field(default=None, max_length=200)
    availability: Literal[
        "available", "missing", "unreadable", "unsafe", "check_failed"
    ] | None = None
    file_count: Annotated[int, Field(strict=True, ge=0)] | None = None
    total_bytes: Annotated[int, Field(strict=True, ge=0)] | None = None
    reason: str | None = Field(default=None, max_length=200)
    error: dict[str, Any] | None = None


class ParameterChoice(StrictRequest):
    value: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)

    @field_validator("value", "label")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("enum choices cannot contain control characters")
        normalized = value.strip()
        if not normalized:
            raise ValueError("enum choices cannot be blank")
        return normalized


class ParameterVisibilityCondition(StrictRequest):
    parameter_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
    equals: JsonScalar


class ParameterDefinition(StrictRequest):
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
    label: str = Field(min_length=1, max_length=200)
    type: Literal["integer", "number", "boolean", "enum", "string"]
    semantic_role: Literal["hyperparameter", "dataset", "stage_input"] = "hyperparameter"
    default: JsonScalar
    description: str | None = Field(default=None, max_length=120)
    minimum: StrictInt | StrictFloat | None = None
    maximum: StrictInt | StrictFloat | None = None
    choices: list[ParameterChoice] = Field(default_factory=list, max_length=100)
    string_min_length: Annotated[int, Field(strict=True, ge=0, le=512)] | None = None
    string_max_length: Annotated[int, Field(strict=True, ge=0, le=512)] | None = None
    visible_when: ParameterVisibilityCondition | None = None
    display_group: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$",
    )
    display_group_label: str | None = Field(default=None, min_length=2, max_length=30)
    display_group_order: Annotated[int, Field(strict=True, ge=0, le=1000)] | None = None
    editable: StrictBool = True
    sensitive: StrictBool = False
    cli_flag: str | None = Field(
        default=None,
        pattern=r"^--[A-Za-z0-9][A-Za-z0-9_-]{0,99}$",
    )
    argument_style: Literal[
        "value", "explicit_boolean", "flag_when_true"
    ] | None = None

    @field_validator("semantic_role", mode="before")
    @classmethod
    def normalize_legacy_dataset_role(cls, value: Any) -> Any:
        return "hyperparameter" if value == "dataset" else value

    @model_validator(mode="after")
    def validate_definition(self) -> ParameterDefinition:
        default = self.default
        if self.semantic_role == "stage_input" and self.type != "string":
            raise ValueError("stage input parameters must be strings")
        if self.semantic_role == "stage_input" and self.visible_when is not None:
            raise ValueError("stage input parameters cannot depend on another parameter")
        valid_default = {
            "integer": type(default) is int,
            "number": type(default) in {int, float},
            "boolean": type(default) is bool,
            "enum": isinstance(default, str),
            "string": isinstance(default, str),
        }[self.type]
        if not valid_default:
            raise ValueError("default must match the declared parameter type")
        if self.argument_style is None:
            # Backward compatibility for existing clients: omitted booleans
            # keep the original presence-only behaviour, while all other
            # parameters use the normal flag/value form.
            self.argument_style = (
                "flag_when_true" if self.type == "boolean" else "value"
            )
        if self.type == "boolean":
            if self.argument_style not in {"explicit_boolean", "flag_when_true"}:
                raise ValueError(
                    "boolean parameters require explicit_boolean or flag_when_true"
                )
        elif self.argument_style != "value":
            raise ValueError(
                "explicit_boolean and flag_when_true are only valid for boolean parameters"
            )
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("minimum cannot exceed maximum")
        if any(bound is not None and not math.isfinite(bound) for bound in (self.minimum, self.maximum)):
            raise ValueError("numeric bounds must be finite")
        if self.type == "integer":
            if any(bound is not None and type(bound) is not int for bound in (self.minimum, self.maximum)):
                raise ValueError("integer parameter bounds must be integers")
            if abs(default) > _MAX_SAFE_INTEGER or any(bound is not None and abs(bound) > _MAX_SAFE_INTEGER for bound in (self.minimum, self.maximum)):
                raise ValueError("integer parameters must stay within the JavaScript safe integer range")
        elif self.type != "number" and (self.minimum is not None or self.maximum is not None):
            raise ValueError("minimum and maximum are only supported for numeric parameters")
        if self.type in {"integer", "number"}:
            if not math.isfinite(default):
                raise ValueError("numeric default must be finite")
            if self.minimum is not None and default < self.minimum:
                raise ValueError("numeric default cannot be below minimum")
            if self.maximum is not None and default > self.maximum:
                raise ValueError("numeric default cannot exceed maximum")
        if self.type == "enum":
            values = [choice.value for choice in self.choices]
            if not values:
                raise ValueError("enum parameters require at least one choice")
            if len(values) != len(set(values)):
                raise ValueError("enum choice values must be unique")
            if default not in values:
                raise ValueError("enum default must be one of the choices")
        elif self.choices:
            raise ValueError("choices are only supported for enum parameters")
        if self.type != "string" and (self.string_min_length is not None or self.string_max_length is not None):
            raise ValueError("string length limits are only supported for string parameters")
        if self.string_min_length is not None and self.string_max_length is not None and self.string_min_length > self.string_max_length:
            raise ValueError("string_min_length cannot exceed string_max_length")
        if self.type == "string":
            if len(default) > 512 or any(character in default for character in ("\x00", "\n", "\r")):
                raise ValueError("string default must be a safe single-line value of at most 512 characters")
            if self.string_min_length is not None and len(default) < self.string_min_length:
                raise ValueError("string default is shorter than string_min_length")
            if self.string_max_length is not None and len(default) > self.string_max_length:
                raise ValueError("string default exceeds string_max_length")
        if self.display_group is None:
            if self.display_group_label is not None or self.display_group_order is not None:
                raise ValueError("display group metadata requires display_group")
        elif self.display_group_label is None or self.display_group_order is None:
            raise ValueError("display_group requires a label and order")
        return self


class RuntimeEnvironment(StrictRequest):
    kind: Literal["system", "conda"] = "system"
    conda_environment: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )

    @model_validator(mode="after")
    def validate_environment(self) -> RuntimeEnvironment:
        if self.kind == "conda" and self.conda_environment is None:
            raise ValueError("conda runtime requires conda_environment")
        if self.kind == "system" and self.conda_environment is not None:
            raise ValueError("system runtime cannot declare conda_environment")
        return self


class MonitoringContract(StrictRequest):
    source: Literal["stdout"] = "stdout"
    format: Literal["plain", "transformers", "jsonl"] = "plain"


class LaunchTemplate(StrictRequest):
    domain: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    server_ref: str = Field(min_length=1, max_length=200)
    working_directory: str = Field(min_length=1, max_length=1000)
    launcher_kind: Literal["torchrun", "direct"] = "direct"
    executable: str = Field(min_length=1, max_length=500)
    entrypoint: str = Field(min_length=1, max_length=1000)
    fixed_argv: list[str] = Field(default_factory=list, max_length=200)
    output_root: str = Field(min_length=1, max_length=1000)
    output_flag: str = Field(
        default="--output_dir",
        pattern=r"^--[A-Za-z0-9][A-Za-z0-9_-]{0,99}$",
    )
    runtime_environment: RuntimeEnvironment = Field(
        default_factory=RuntimeEnvironment
    )
    monitoring: MonitoringContract = Field(default_factory=MonitoringContract)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_launcher_kind(cls, value: Any) -> Any:
        if isinstance(value, dict) and "launcher_kind" not in value:
            executable = str(value.get("executable", ""))
            value = dict(value)
            value["launcher_kind"] = (
                "torchrun"
                if executable.rsplit("/", 1)[-1] == "torchrun"
                else "direct"
            )
        return value

    @field_validator("server_ref")
    @classmethod
    def safe_server_ref(cls, value: str) -> str:
        if not _SAFE_REF.fullmatch(value):
            raise ValueError("server_ref contains unsupported characters")
        return value

    @field_validator(
        "working_directory",
        "executable",
        "entrypoint",
        "output_root",
    )
    @classmethod
    def single_token(cls, value: str) -> str:
        if any(character in value for character in ("\x00", "\n", "\r")) or (
            _SHELL_META.search(value) is not None
        ):
            raise ValueError(
                "launch template values cannot contain shell syntax or control characters"
            )
        return value

    @field_validator("fixed_argv")
    @classmethod
    def safe_fixed_argv(cls, value: list[str]) -> list[str]:
        if any(
            not token
            or len(token) > 1000
            or any(character in token for character in ("\x00", "\n", "\r"))
            or _SHELL_META.search(token) is not None
            for token in value
        ):
            raise ValueError(
                "fixed_argv cannot contain shell syntax, control characters, or empty tokens"
            )
        return value


class ModelConfigurationRequest(StrictRequest):
    launch_template: LaunchTemplate
    data_access_mode: Literal["datapilot_managed", "self_managed"] = "self_managed"
    parameter_definitions: list[ParameterDefinition] = Field(
        min_length=1,
        max_length=200,
    )

    @field_validator("parameter_definitions")
    @classmethod
    def unique_parameter_keys(
        cls, value: list[ParameterDefinition]
    ) -> list[ParameterDefinition]:
        keys = [definition.key for definition in value]
        if len(keys) != len(set(keys)):
            raise ValueError("parameter keys must be unique")
        return value

    @model_validator(mode="after")
    def reserve_platform_output_flag(self) -> ModelConfigurationRequest:
        if sum(
            definition.semantic_role == "stage_input"
            for definition in self.parameter_definitions
        ) > 1:
            raise ValueError("a model family can declare at most one stage input parameter")
        groups: dict[str, tuple[str, int]] = {}
        labels: dict[str, str] = {}
        for definition in self.parameter_definitions:
            if definition.display_group is None:
                continue
            metadata = (definition.display_group_label or "", definition.display_group_order or 0)
            known = groups.get(definition.display_group)
            if known is not None and known != metadata:
                raise ValueError("parameters in one display group must share its label and order")
            normalized_label = metadata[0].casefold()
            known_key = labels.get(normalized_label)
            if known_key is not None and known_key != definition.display_group:
                raise ValueError("display group labels must be unique")
            groups[definition.display_group] = metadata
            labels[normalized_label] = definition.display_group
        conflicting = [
            definition.key
            for definition in self.parameter_definitions
            if (definition.cli_flag or f"--{definition.key}")
            == self.launch_template.output_flag
        ]
        if conflicting:
            raise ValueError(
                "output_flag is platform-managed and cannot be used by a parameter definition"
            )
        parameter_flags = {
            definition.cli_flag or f"--{definition.key}"
            for definition in self.parameter_definitions
        }
        fixed_token_flags = {
            token.split("=", 1)[0] if token.startswith("--") else token
            for token in self.launch_template.fixed_argv
        }
        duplicate_fixed_flags = parameter_flags.intersection(
            fixed_token_flags
        )
        if duplicate_fixed_flags:
            duplicate = sorted(duplicate_fixed_flags)[0]
            raise ValueError(
                f"fixed_argv cannot redeclare registered parameter flag {duplicate}"
            )
        if self.data_access_mode == "datapilot_managed" and (
            "--dataset_manifest" in parameter_flags
            or "--dataset_manifest" in fixed_token_flags
            or self.launch_template.output_flag == "--dataset_manifest"
        ):
            raise ValueError(
                "--dataset_manifest is reserved for DataPilot managed data"
            )
        definitions = {item.key: item for item in self.parameter_definitions}
        dependencies: dict[str, str] = {}
        for definition in self.parameter_definitions:
            condition = definition.visible_when
            if condition is None:
                continue
            controller = definitions.get(condition.parameter_key)
            if controller is None:
                raise ValueError(
                    f"parameter dependency for {definition.key} references an unknown parameter"
                )
            if controller.key == definition.key:
                raise ValueError("parameter dependency cannot reference itself")
            expected = condition.equals
            valid_type = {
                "integer": type(expected) is int,
                "number": type(expected) in {int, float},
                "boolean": type(expected) is bool,
                "enum": isinstance(expected, str),
                "string": isinstance(expected, str),
            }[controller.type]
            if not valid_type:
                raise ValueError(
                    f"dependency value for {definition.key} must match {controller.key} type"
                )
            if controller.type == "enum" and expected not in {
                choice.value for choice in controller.choices
            }:
                raise ValueError(
                    f"dependency value for {definition.key} is not an allowed choice"
                )
            if type(expected) in {int, float}:
                if controller.minimum is not None and expected < controller.minimum:
                    raise ValueError(
                        f"dependency value for {definition.key} is below the controller minimum"
                    )
                if controller.maximum is not None and expected > controller.maximum:
                    raise ValueError(
                        f"dependency value for {definition.key} exceeds the controller maximum"
                    )
            if controller.type == "string":
                if any(character in expected for character in ("\x00", "\n", "\r")):
                    raise ValueError(f"dependency value for {definition.key} contains control characters")
                if controller.string_min_length is not None and len(expected) < controller.string_min_length:
                    raise ValueError(f"dependency value for {definition.key} is shorter than the controller minimum length")
                if controller.string_max_length is not None and len(expected) > controller.string_max_length:
                    raise ValueError(f"dependency value for {definition.key} exceeds the controller maximum length")
            dependencies[definition.key] = controller.key
        for start in dependencies:
            visited: set[str] = set()
            current = start
            while current in dependencies:
                if current in visited:
                    raise ValueError("parameter dependencies cannot contain a cycle")
                visited.add(current)
                current = dependencies[current]
        return self


class CreateModelRequest(StrictRequest):
    family_name: str = Field(min_length=1, max_length=200)
    configuration: ModelConfigurationRequest

    @field_validator("family_name")
    @classmethod
    def normalize_family_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("family_name cannot be blank")
        return normalized


class UpdateModelRequest(StrictRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    configuration: ModelConfigurationRequest


class VerifyModelRequest(StrictRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class RunStageRequest(StrictRequest):
    parameters: dict[str, JsonScalar] = Field(default_factory=dict, max_length=200)
    stage_input_source: Literal["manual", "previous_stage_output"] = "manual"

    @field_validator("parameters")
    @classmethod
    def safe_parameter_keys(
        cls, value: dict[str, JsonScalar]
    ) -> dict[str, JsonScalar]:
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", key) for key in value):
            raise ValueError("parameters contain an invalid key")
        return value


class DatasetSelectionRequest(StrictRequest):
    train_replica_refs: list[str] = Field(default_factory=list, max_length=1000)
    test_replica_refs: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_selection(self) -> DatasetSelectionRequest:
        combined = [*self.train_replica_refs, *self.test_replica_refs]
        if len(combined) != len(set(combined)):
            raise ValueError("dataset replica selections must be unique and disjoint")
        if any(not _SAFE_REF.fullmatch(ref) for ref in combined):
            raise ValueError("dataset selection contains an invalid replica reference")
        return self


class RunRequest(StrictRequest):
    family_ref: str = Field(min_length=1, max_length=200)
    server_ref: str = Field(min_length=1, max_length=200)
    gpu_uuids: list[str] = Field(min_length=1, max_length=8)
    stages: list[RunStageRequest] = Field(min_length=1, max_length=10)
    execution_mode: Literal["simulation", "real"]
    version_description: str = Field(default="", max_length=500)
    dataset_selection: DatasetSelectionRequest | None = None

    @field_validator("version_description")
    @classmethod
    def normalize_version_description(cls, value: str) -> str:
        if "\x00" in value or "\r" in value:
            raise ValueError("version_description contains control characters")
        return value.strip()

    @field_validator("family_ref", "server_ref")
    @classmethod
    def safe_ref(cls, value: str) -> str:
        if not _SAFE_REF.fullmatch(value):
            raise ValueError("resource reference contains unsupported characters")
        return value

    @field_validator("gpu_uuids")
    @classmethod
    def unique_gpus(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("gpu_uuids must be unique")
        if any(not _SAFE_REF.fullmatch(gpu_uuid) for gpu_uuid in value):
            raise ValueError("gpu_uuids contain an invalid resource reference")
        return value

    @model_validator(mode="after")
    def first_stage_is_manual(self) -> RunRequest:
        if self.stages[0].stage_input_source != "manual":
            raise ValueError("the first stage must use manual input")
        return self


class PreviewRunRequest(RunRequest):
    """Preview-only execution target.

    Previewing a real RunSpec never creates a task, lease, model version or
    process, and remains useful while deployment-level real execution is off.
    """

    execution_mode: Literal["simulation", "real"]


class PollTrainingActionsRequest(StrictRequest):
    worker_instance_id: str = Field(min_length=1, max_length=200)
    wait_seconds: Annotated[int, Field(strict=True, ge=0, le=30)] = 20
    limit: Annotated[int, Field(strict=True, ge=1, le=10)] = 1


class TrainingActionResultRequest(StrictRequest):
    worker_instance_id: str = Field(min_length=1, max_length=200)
    claim_token: str = Field(min_length=32, max_length=512)
    status: Literal["succeeded", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None


class TrainingRunUpdateRequest(StrictRequest):
    worker_instance_id: str = Field(min_length=1, max_length=200)
    owner_epoch: Annotated[int, Field(strict=True, ge=1)]
    worker_seq: Annotated[int, Field(strict=True, ge=1)]
    updates: list[dict[str, Any]] = Field(min_length=1, max_length=200)

    @field_validator("updates")
    @classmethod
    def validate_updates(cls, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(json.dumps(values, ensure_ascii=False).encode("utf-8")) > 524288:
            raise ValueError("training update batch exceeds 512 KiB")
        allowed = {
            "accepted", "started", "heartbeat", "log", "metric",
            "checkpoint", "exited", "reconciliation",
        }
        for value in values:
            if value.get("kind") not in allowed:
                raise ValueError("training update contains an unsupported kind")
            stage_ref = value.get("stage_ref")
            if stage_ref is not None and (
                not isinstance(stage_ref, str) or not _SAFE_REF.fullmatch(stage_ref)
            ):
                raise ValueError("training update contains an invalid stage_ref")
            kind = value.get("kind")
            if kind == "log":
                messages: list[str] = []
                if value.get("message") is not None:
                    if not isinstance(value["message"], str):
                        raise ValueError("log message must be text")
                    messages.append(value["message"])
                lines = value.get("lines")
                if lines is not None:
                    if not isinstance(lines, list) or len(lines) > 200:
                        raise ValueError("log lines must contain at most 200 items")
                    for line in lines:
                        message = line.get("message") if isinstance(line, dict) else line
                        if not isinstance(message, str):
                            raise ValueError("log line must be text")
                        messages.append(message)
                if len(messages) > 200:
                    raise ValueError("log update must contain at most 200 lines")
                if not messages or any(len(message.encode("utf-8")) > 16384 for message in messages):
                    raise ValueError("log lines must be between 1 and 16384 bytes")
                if sum(len(message.encode("utf-8")) for message in messages) > 262144:
                    raise ValueError("log update exceeds the 256 KiB batch limit")
            elif kind == "metric":
                for key in (
                    "step", "total_steps", "epoch", "loss", "learning_rate",
                    "grad_norm", "elapsed_seconds",
                ):
                    number = value.get(key)
                    if number is not None and (
                        isinstance(number, bool) or not isinstance(number, (int, float))
                        or not math.isfinite(float(number))
                    ):
                        raise ValueError("metric fields must be finite numbers")
                for key in ("step", "total_steps"):
                    number = value.get(key)
                    if number is not None and (
                        not isinstance(number, int)
                        or isinstance(number, bool)
                        or number < 0
                    ):
                        raise ValueError("metric step fields must be non-negative integers")
                gpus = value.get("gpus", [])
                if not isinstance(gpus, list) or len(gpus) > 8:
                    raise ValueError("metric GPU samples must contain at most 8 items")
                gpu_uuids: set[str] = set()
                allowed_gpu_fields = {
                    "uuid", "index", "utilization_percent", "gpu_memory_mib",
                    "temperature_celsius",
                }
                for gpu in gpus:
                    if not isinstance(gpu, dict) or not set(gpu) <= allowed_gpu_fields:
                        raise ValueError("metric GPU sample has unsupported fields")
                    gpu_uuid = gpu.get("uuid")
                    index = gpu.get("index")
                    if (
                        not isinstance(gpu_uuid, str)
                        or not _SAFE_REF.fullmatch(gpu_uuid)
                        or gpu_uuid in gpu_uuids
                        or not isinstance(index, int)
                        or isinstance(index, bool)
                        or index < 0
                    ):
                        raise ValueError("metric GPU sample identity is invalid")
                    gpu_uuids.add(gpu_uuid)
                    for field in (
                        "utilization_percent", "gpu_memory_mib", "temperature_celsius"
                    ):
                        number = gpu.get(field)
                        if number is not None and (
                            isinstance(number, bool)
                            or not isinstance(number, (int, float))
                            or not math.isfinite(float(number))
                        ):
                            raise ValueError("metric GPU values must be finite numbers")
                    utilization = gpu.get("utilization_percent")
                    memory = gpu.get("gpu_memory_mib")
                    if utilization is not None and not 0 <= float(utilization) <= 100:
                        raise ValueError("metric GPU utilization is outside 0..100")
                    if memory is not None and float(memory) < 0:
                        raise ValueError("metric GPU memory must not be negative")
            elif kind == "checkpoint":
                relative_path = value.get("relative_path")
                if not isinstance(relative_path, str) or len(relative_path) > 1024:
                    raise ValueError("checkpoint relative_path is invalid")
        return values


class StopRunRequest(StrictRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class DirectoryListingRequest(StrictRequest):
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def absolute_safe_path(cls, value: str) -> str:
        if not value.startswith("/") or any(c in value for c in ("\x00", "\n", "\r")):
            raise ValueError("path must be a safe absolute directory path")
        return value.rstrip("/") or "/"


class CreateDatasetTransfersRequest(StrictRequest):
    node_ref: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    release_refs: list[str] = Field(min_length=1, max_length=100)
    target_parent_directory: str = Field(min_length=1, max_length=4096)

    @field_validator("release_refs")
    @classmethod
    def unique_release_refs(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(not _SAFE_REF.fullmatch(value) for value in values):
            raise ValueError("release_refs must contain unique safe resource references")
        return values

    @field_validator("target_parent_directory")
    @classmethod
    def safe_target_parent(cls, value: str) -> str:
        if not value.startswith("/") or any(c in value for c in ("\x00", "\n", "\r")):
            raise ValueError("target_parent_directory must be a safe absolute path")
        return value.rstrip("/") or "/"


class EmptyRequest(StrictRequest):
    pass


def create_training_router(
    service: TrainingServiceProtocol,
    *,
    settings: TrainingSettings | None = None,
    principal_provider: Callable[[], TrainingPrincipal] | None = None,
) -> APIRouter:
    """Create the simulation-only training API router.

    The default principal is read-only. Development writes require an explicit
    ``VLA_TRAINING_DEV_ADMIN=true`` (or equivalent injected settings) and an
    enabled simulation mode.
    """

    active_settings = settings or TrainingSettings.from_env()
    get_principal = principal_provider or active_settings.principal
    router = APIRouter(prefix="/api/training", tags=["training"])

    @router.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return _capabilities_projection(
            _translate(service.capabilities, get_principal())
        )

    @router.get("/servers")
    def list_servers() -> dict[str, Any]:
        return {
            "servers": [
                _server_projection(server)
                for server in _translate(service.list_servers)
            ]
        }

    @router.get("/servers/{server_ref}/resources")
    def server_resources(server_ref: str) -> dict[str, Any]:
        resources = _translate(service.get_server_resources, server_ref)
        servers = _translate(service.list_servers)
        server = next(
            (item for item in servers if item.get("server_ref") == server_ref),
            {"server_ref": server_ref, "gpu_count": len(resources.get("gpus", []))},
        )
        return _resources_projection(resources, server)

    @router.get("/nodes")
    def list_nodes() -> dict[str, Any]:
        principal = get_principal()
        return {
            "nodes": [
                _node_projection(node, principal)
                for node in _translate(service.list_nodes)
            ]
        }

    @router.post("/nodes", status_code=201)
    def create_node(request: CreateTrainingNodeRequest) -> dict[str, Any]:
        return {
            "node": _translate(service.create_node, request, get_principal())
        }

    @router.post("/nodes/enroll")
    def enroll_node(
        request: EnrollTrainingNodeRequest, response: Response
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _translate(service.enroll_node, request)

    @router.get("/nodes/{node_ref}")
    def get_node(node_ref: str) -> dict[str, Any]:
        principal = get_principal()
        return {
            "node": _node_projection(
                _translate(service.get_node, node_ref), principal
            )
        }

    @router.put("/nodes/{node_ref}")
    def update_node(
        node_ref: str, request: UpdateTrainingNodeRequest
    ) -> dict[str, Any]:
        return {
            "node": _translate(
                service.update_node, node_ref, request, get_principal()
            )
        }

    @router.delete("/nodes/{node_ref}", status_code=204)
    def delete_node(
        node_ref: str,
        expected_revision: int = Query(ge=1),
    ) -> Response:
        _translate(
            service.delete_node,
            node_ref,
            expected_revision,
            get_principal(),
        )
        return Response(status_code=204)

    @router.post("/nodes/{node_ref}/enrollment-tokens", status_code=201)
    def create_enrollment_token(
        node_ref: str,
        request: CreateEnrollmentTokenRequest,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _translate(
            service.create_enrollment_token,
            node_ref,
            request,
            get_principal(),
        )

    @router.post("/nodes/{node_ref}/host-key")
    def discover_node_host_key(node_ref: str) -> dict[str, Any]:
        return {
            "host_key": _translate(
                service.discover_node_host_key,
                node_ref,
                get_principal(),
            )
        }

    @router.post("/nodes/{node_ref}/deploy-worker")
    def deploy_node_worker(
        node_ref: str,
        request: DeployTrainingNodeWorkerRequest,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _translate(
            service.deploy_node_worker,
            node_ref,
            request,
            get_principal(),
        )

    @router.post("/nodes/{node_ref}/preflight-worker")
    def preflight_node_worker(
        node_ref: str,
        request: DeployTrainingNodeWorkerRequest,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _translate(
            service.preflight_node_worker,
            node_ref,
            request,
            get_principal(),
        )

    @router.post("/nodes/{node_ref}/remove-worker")
    def remove_node_worker(
        node_ref: str,
        request: RemoveTrainingNodeWorkerRequest,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _translate(
            service.remove_node_worker,
            node_ref,
            request,
            get_principal(),
        )

    @router.post("/nodes/{node_ref}/heartbeat")
    def heartbeat_node(
        node_ref: str,
        request: TrainingNodeHeartbeatRequest,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization", max_length=512),
        ] = None,
    ) -> dict[str, Any]:
        worker_token = _worker_bearer_token(authorization)
        heartbeat = _translate(
            service.heartbeat_node,
            node_ref,
            worker_token,
            request,
        )
        node = heartbeat["node"]
        return {
            "node_ref": node["node_ref"],
            "status": node["status"],
            "state_revision": node["state_revision"],
            "heartbeat_revision": node["heartbeat_revision"],
            "server_time": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "next_heartbeat_seconds": 15,
            "command": heartbeat.get("command"),
        }

    @router.post("/nodes/{node_ref}/commands/poll")
    def poll_node_commands(
        node_ref: str,
        request: PollNodeCommandsRequest,
        authorization: Annotated[str | None, Header(alias="Authorization", max_length=512)] = None,
    ) -> dict[str, Any]:
        return _translate(
            service.poll_node_commands,
            node_ref,
            _worker_bearer_token(authorization),
            request,
        )

    @router.post("/nodes/{node_ref}/commands/{command_ref}/result")
    def finish_model_verification(
        node_ref: str,
        command_ref: str,
        request: ModelVerificationResultRequest | WorkerCommandResultRequest,
        authorization: Annotated[
            str | None,
            Header(alias="Authorization", max_length=512),
        ] = None,
    ) -> dict[str, Any]:
        if not _SAFE_REF.fullmatch(command_ref):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_command_ref",
                    "message": "Worker command reference contains unsupported characters.",
                },
            )
        if command_ref.startswith("verify_"):
            if not isinstance(request, ModelVerificationResultRequest):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_verification_result", "message": "A model verification result is required."},
                )
            return _translate(
                service.finish_model_verification,
                node_ref,
                command_ref,
                _worker_bearer_token(authorization),
                request,
            )
        if not isinstance(request, WorkerCommandResultRequest):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_worker_command_result", "message": "A Worker command result is required."},
            )
        return _translate(
            service.finish_node_command,
            node_ref,
            command_ref,
            _worker_bearer_token(authorization),
            request,
        )

    @router.post("/nodes/{node_ref}/training-actions/poll")
    def poll_training_actions(
        node_ref: str,
        request: PollTrainingActionsRequest,
        authorization: Annotated[str | None, Header(alias="Authorization", max_length=512)] = None,
    ) -> dict[str, Any]:
        return _translate(
            service.poll_training_actions,
            node_ref,
            _worker_bearer_token(authorization),
            request,
        )

    @router.post("/nodes/{node_ref}/training-actions/{action_ref}/result")
    def finish_training_action(
        node_ref: str,
        action_ref: str,
        request: TrainingActionResultRequest,
        authorization: Annotated[str | None, Header(alias="Authorization", max_length=512)] = None,
    ) -> dict[str, Any]:
        return _translate(
            service.finish_training_action,
            node_ref,
            action_ref,
            _worker_bearer_token(authorization),
            request,
        )

    @router.post("/nodes/{node_ref}/runs/{run_ref}/updates")
    def update_training_run(
        node_ref: str,
        run_ref: str,
        request: TrainingRunUpdateRequest,
        authorization: Annotated[str | None, Header(alias="Authorization", max_length=512)] = None,
    ) -> dict[str, Any]:
        return _translate(
            service.update_training_run,
            node_ref,
            run_ref,
            _worker_bearer_token(authorization),
            request,
        )

    @router.get("/nodes/{node_ref}/resources")
    def node_resources(node_ref: str) -> dict[str, Any]:
        return _translate(service.get_node_resources, node_ref)

    @router.get("/dataset-releases")
    def list_dataset_releases() -> dict[str, Any]:
        return {"releases": _translate(service.list_dataset_releases)}

    @router.get("/nodes/{node_ref}/dataset-replicas")
    def list_dataset_replicas(node_ref: str) -> dict[str, Any]:
        return {"replicas": _translate(service.list_dataset_replicas, node_ref, get_principal())}

    @router.post("/nodes/{node_ref}/directory-listings", status_code=202)
    def request_directory_listing(
        node_ref: str, request: DirectoryListingRequest
    ) -> dict[str, Any]:
        return {"listing": _translate(service.request_directory_listing, node_ref, request, get_principal())}

    @router.get("/directory-listings/{listing_ref}")
    def get_directory_listing(listing_ref: str) -> dict[str, Any]:
        return {"listing": _translate(service.get_directory_listing, listing_ref, get_principal())}

    @router.post("/dataset-transfers", status_code=202)
    def create_dataset_transfers(
        request: CreateDatasetTransfersRequest, idempotency_key: IdempotencyKey
    ) -> dict[str, Any]:
        return {"transfers": _translate(service.create_dataset_transfers, request, idempotency_key, get_principal())}

    @router.get("/dataset-transfers")
    def list_dataset_transfers(
        node_ref: str | None = Query(default=None, max_length=200),
        status: str | None = Query(default=None, max_length=30),
    ) -> dict[str, Any]:
        return {"transfers": _translate(service.list_dataset_transfers, node_ref=node_ref, status=status, principal=get_principal())}

    @router.get("/dataset-transfers/{transfer_ref}")
    def get_dataset_transfer(transfer_ref: str) -> dict[str, Any]:
        return {"transfer": _translate(service.get_dataset_transfer, transfer_ref, get_principal())}

    @router.post("/dataset-transfers/{transfer_ref}/cancel", status_code=202)
    def cancel_dataset_transfer(transfer_ref: str, request: EmptyRequest) -> dict[str, Any]:
        return {"transfer": _translate(service.cancel_dataset_transfer, transfer_ref, get_principal())}

    @router.post("/dataset-transfers/{transfer_ref}/pause", status_code=202)
    def pause_dataset_transfer(transfer_ref: str, request: EmptyRequest) -> dict[str, Any]:
        return {"transfer": _translate(service.pause_dataset_transfer, transfer_ref, get_principal())}

    @router.post("/dataset-transfers/{transfer_ref}/retry", status_code=202)
    def retry_dataset_transfer(
        transfer_ref: str, request: EmptyRequest, idempotency_key: IdempotencyKey
    ) -> dict[str, Any]:
        return {"transfer": _translate(service.retry_dataset_transfer, transfer_ref, idempotency_key, get_principal())}

    @router.post("/dataset-replicas/{replica_ref}/remove", status_code=202)
    def remove_dataset_replica(replica_ref: str, request: EmptyRequest) -> dict[str, Any]:
        return {"replica": _translate(service.remove_dataset_replica, replica_ref, get_principal())}

    @router.get("/nodes/{node_ref}/dataset-releases/{release_ref}/manifest")
    def worker_dataset_manifest(
        node_ref: str,
        release_ref: str,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2000),
        authorization: Annotated[str | None, Header(alias="Authorization", max_length=512)] = None,
    ) -> dict[str, Any]:
        return _translate(
            service.source_manifest_page,
            node_ref,
            release_ref,
            _worker_bearer_token(authorization),
            cursor=cursor,
            limit=limit,
        )

    @router.get("/nodes/{node_ref}/dataset-files/{file_ref}/content")
    def worker_dataset_file(
        node_ref: str,
        file_ref: str,
        authorization: Annotated[str | None, Header(alias="Authorization", max_length=512)] = None,
    ) -> FileResponse:
        item = _translate(
            service.source_file,
            node_ref,
            file_ref,
            _worker_bearer_token(authorization),
        )
        return FileResponse(
            path=item["path"],
            media_type="application/octet-stream",
            headers={
                "ETag": f'"{item["sha256"]}"',
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    @router.get("/models")
    def list_models() -> dict[str, Any]:
        principal = get_principal()
        include_private = principal.can("training:manage_models")
        return {
            "models": [
                _model_projection(model, principal)
                for model in map(
                    _normalize_model,
                    _translate(
                        service.list_models,
                        include_private=include_private,
                    ),
                )
            ]
        }

    @router.get("/model-version-families")
    def list_model_version_families(
        query: str | None = Query(default=None, max_length=200),
        after: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        principal = get_principal()
        page = _translate(
            service.list_model_version_families,
            query=query.strip() if query else None,
            after=after,
            limit=limit,
            principal=principal,
        )
        families = page.get("families") if isinstance(page, dict) else None
        if not isinstance(families, list):
            raise TypeError("training service returned an invalid version family page")
        return {
            "families": [
                _model_version_family_projection(item, principal)
                for item in families
                if isinstance(item, dict)
            ],
            "next_after": page.get("next_after"),
        }

    @router.get("/model-version-families/{family_ref}/versions")
    def list_model_versions(
        family_ref: str,
        after: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=6, ge=1, le=100),
    ) -> dict[str, Any]:
        principal = get_principal()
        page = _translate(
            service.list_model_versions,
            family_ref,
            after=after,
            limit=limit,
            principal=principal,
        )
        versions = page.get("versions") if isinstance(page, dict) else None
        if not isinstance(versions, list):
            raise TypeError("training service returned an invalid model version page")
        return {
            "versions": [
                _model_version_projection(item, principal)
                for item in versions
                if isinstance(item, dict)
            ],
            "next_after": page.get("next_after"),
        }

    @router.get("/model-versions/{version_ref}")
    def get_model_version(version_ref: str) -> dict[str, Any]:
        principal = get_principal()
        version = _translate(service.get_model_version, version_ref, principal)
        if not isinstance(version, dict):
            raise TypeError("training service returned an invalid model version")
        return {"version": _model_version_projection(version, principal)}

    @router.post("/model-versions/{version_ref}/artifact-checks", status_code=202)
    def request_model_version_artifact_inspection(
        version_ref: str, idempotency_key: IdempotencyKey
    ) -> dict[str, Any]:
        inspection = _translate(
            service.request_model_version_artifact_inspection,
            version_ref,
            idempotency_key,
            get_principal(),
        )
        return {"inspection": inspection}

    @router.get("/models/{family_ref}")
    def get_model(family_ref: str) -> dict[str, Any]:
        principal = get_principal()
        model = _normalize_model(
            _translate(
                service.get_model,
                family_ref,
                include_private=principal.can("training:manage_models"),
            )
        )
        return {"model": _model_projection(model, principal)}

    @router.post("/models", status_code=201)
    def create_model(request: CreateModelRequest) -> dict[str, Any]:
        model = _normalize_model(
            _translate(service.create_model, request, get_principal())
        )
        return {"model": model}

    @router.put("/models/{family_ref}")
    def update_model(
        family_ref: str,
        request: UpdateModelRequest,
    ) -> dict[str, Any]:
        model = _normalize_model(
            _translate(
                service.update_model,
                family_ref,
                request,
                get_principal(),
            )
        )
        return {"model": model}

    @router.post("/models/{family_ref}/verify", status_code=202)
    def verify_model(
        family_ref: str,
        request: VerifyModelRequest,
    ) -> dict[str, Any]:
        model = _normalize_model(
            _translate(
                service.verify_model,
                family_ref,
                request,
                get_principal(),
            )
        )
        return {"model": model}

    @router.post("/runs/preview")
    def preview_run(request: PreviewRunRequest) -> dict[str, Any]:
        return _preview_projection(
            _translate(service.preview_run, request, get_principal())
        )

    @router.post("/runs", status_code=201)
    def create_run(
        request: RunRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        run = _normalize_run(
            _translate(
                service.create_run,
                request,
                idempotency_key,
                get_principal(),
            )
        )
        return {"run": run}

    @router.get("/runs")
    def list_runs(
        status: str | None = Query(default=None, max_length=30),
        query: str | None = Query(default=None, max_length=200),
        after: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        page = _translate(
            service.list_runs,
            status=status,
            query=query.strip() if query else None,
            after=after,
            limit=limit,
        )
        projection = _page_projection(page, "runs")
        principal = get_principal()
        projection["runs"] = [
            _run_projection(_normalize_run(run), principal)
            for run in projection["runs"]
        ]
        return projection

    @router.get("/runs/{run_ref}")
    def get_run(run_ref: str) -> dict[str, Any]:
        return {
            "run": _run_projection(
                _normalize_run(_translate(service.get_run, run_ref)),
                get_principal(),
            )
        }

    @router.post("/runs/{run_ref}/stop")
    def stop_run(
        run_ref: str,
        request: StopRunRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        run = _normalize_run(
            _translate(
                service.stop_run,
                run_ref,
                request.expected_revision,
                idempotency_key,
                get_principal(),
            )
        )
        return {"run": run}

    @router.get("/runs/{run_ref}/logs")
    def run_logs(
        run_ref: str,
        after_seq: int = Query(default=0, ge=0),
        before_seq: int | None = Query(default=None, ge=1),
        tail: bool = Query(default=False),
        limit: int = Query(default=500, ge=1, le=2000),
        stage_ref: str | None = Query(default=None, max_length=200),
        levels: list[Literal["info", "warning", "error"]] = Query(default=[]),
        query: str | None = Query(default=None, max_length=200),
    ) -> dict[str, Any]:
        if before_seq is not None and after_seq > 0:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_log_cursor", "message": "before_seq and after_seq cannot be used together."},
            )
        page = _translate(
            service.list_logs,
            run_ref,
            after_seq=after_seq,
            before_seq=before_seq,
            tail=tail,
            limit=limit,
            stage_ref=stage_ref,
            levels=tuple(levels),
            query=query.strip() if query else None,
        )
        projection = _page_projection(page, "logs")
        projection["next_before"] = page.get("next_before")
        return projection

    @router.get("/runs/{run_ref}/metrics")
    def run_metrics(
        run_ref: str,
        after_seq: int = Query(default=0, ge=0),
        tail: bool = Query(default=False),
        since: datetime | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=2000),
        stage_ref: str | None = Query(default=None, max_length=200),
    ) -> dict[str, Any]:
        page = _translate(
            service.list_metrics,
            run_ref,
            after_seq=after_seq,
            tail=tail,
            since=since.astimezone(UTC).isoformat(timespec="milliseconds") if since else None,
            limit=limit,
            stage_ref=stage_ref,
        )
        projection = _page_projection(page, "metrics")
        projection["metrics"] = [
            _metric_projection(metric) for metric in projection["metrics"]
        ]
        return projection

    @router.get("/events")
    async def events(
        request: Request,
        after_seq: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(
            default=None,
            alias="Last-Event-ID",
            max_length=32,
        ),
    ) -> StreamingResponse:
        cursor = max(after_seq, _event_cursor(last_event_id))

        async def stream():
            nonlocal cursor
            loop = asyncio.get_running_loop()
            heartbeat_at = loop.time() + 15.0
            yield "retry: 1000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                page = await asyncio.to_thread(
                    _translate,
                    service.list_events,
                    after_seq=cursor,
                    limit=200,
                )
                raw_events = page.get("items", []) if isinstance(page, dict) else page
                for raw_event in raw_events:
                    event = _safe_event(raw_event)
                    cursor = event["event_id"]
                    payload = json.dumps(
                        event,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    yield (
                        f"id: {cursor}\n"
                        f"event: {event['type']}\n"
                        f"data: {payload}\n\n"
                    )
                now = loop.time()
                if now >= heartbeat_at:
                    yield ": keepalive\n\n"
                    heartbeat_at = now + 15.0
                if not raw_events:
                    await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _translate(callable_: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return callable_(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        if (
            isinstance(status_code, int)
            and status_code in {400, 403, 404, 409, 422, 429, 503}
            and isinstance(code, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code)
        ):
            public_message = getattr(exc, "public_message", None)
            if not isinstance(public_message, str):
                public_message = str(exc)
            detail: dict[str, Any] = {"code": code, "message": public_message}
            current = _safe_error_current(getattr(exc, "current", None))
            if current is not None:
                detail["current"] = current
            raise HTTPException(
                status_code=status_code,
                detail=detail,
            ) from exc
        raise


def _worker_bearer_token(authorization: str | None) -> str:
    if authorization is not None and authorization.startswith("Bearer "):
        token = authorization[7:]
        if re.fullmatch(r"worker_[A-Za-z0-9_-]{40,250}", token):
            return token
    raise HTTPException(
        status_code=403,
        detail={
            "code": "worker_authentication_failed",
            "message": "The Training Worker credential is invalid.",
        },
    )


def _page_projection(page: Any, key: str) -> dict[str, Any]:
    if isinstance(page, list):
        return {key: page, "next_after": None}
    if isinstance(page, dict):
        return {
            key: page.get("items", page.get(key, [])),
            "next_after": page.get("next_after"),
        }
    raise TypeError("training service returned an invalid page")


def _capabilities_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "permissions": value.get("permissions", []),
        "authentication_mode": value.get("authentication_mode", "read_only"),
        "simulation_enabled": bool(value.get("simulation_enabled", False)),
        "real_execution_enabled": bool(
            value.get("real_execution_enabled", value.get("real_training_enabled", False))
        ),
        "real_execution_disabled_reason": value.get(
            "real_execution_disabled_reason",
            value.get(
                "real_training_disabled_reason",
                "Real training is not enabled.",
            ),
        ),
        "node_deployment_enabled": bool(
            value.get("node_deployment_enabled", False)
        ),
        "node_deployment_disabled_reason": value.get(
            "node_deployment_disabled_reason"
        ),
    }


def _node_projection(
    node: dict[str, Any], principal: TrainingPrincipal
) -> dict[str, Any]:
    projection = dict(node)
    if not principal.can("training:manage_nodes"):
        for private_field in (
            "address",
            "ssh_port",
            "ssh_username",
            "host_key_algorithm",
            "host_public_key",
            "host_key_fingerprint",
            "worker_instance_id",
        ):
            projection.pop(private_field, None)
    return projection


def _server_projection(server: dict[str, Any]) -> dict[str, Any]:
    kind = server.get("kind")
    if not isinstance(kind, str):
        kind = "simulation" if server.get("provider") == "fake" else "unknown"
    return {
        "server_ref": server["server_ref"],
        "name": server.get("name", server.get("label", server["server_ref"])),
        "kind": kind,
        "gpu_count": int(server.get("gpu_count", 0)),
        "status": server.get("status"),
        "online": bool(server.get("online", False)),
        "available": bool(server.get("available", server.get("online", False))),
        "stale": bool(server.get("stale", False)),
    }


def _resources_projection(
    resources: dict[str, Any], server: dict[str, Any]
) -> dict[str, Any]:
    sampled_at = resources.get("sampled_at")
    if isinstance(sampled_at, (int, float)):
        sampled_at = datetime.fromtimestamp(sampled_at, UTC).isoformat()
    elif not isinstance(sampled_at, str):
        sampled_at = None
    gpus = []
    for gpu in resources.get("gpus", []):
        gpus.append(
            {
                "gpu_uuid": gpu.get("gpu_uuid", gpu.get("uuid")),
                "index": int(gpu.get("index", 0)),
                "name": gpu.get("name", "GPU"),
                "total_memory_mib": int(
                    gpu.get("total_memory_mib", gpu.get("memory_total_mib", 0))
                ),
                "used_memory_mib": int(
                    gpu.get("used_memory_mib", gpu.get("memory_used_mib", 0))
                ),
                "utilization_percent": float(gpu.get("utilization_percent", 0)),
                "temperature_c": float(gpu.get("temperature_c", 0)),
                "externally_occupied": bool(gpu.get("externally_occupied", False)),
                "lease_run_ref": gpu.get(
                    "lease_run_ref", gpu.get("leased_by_run_ref")
                ),
            }
        )
    cpu = resources.get("cpu")
    memory = resources.get("memory")
    disks = resources.get("disks", [])
    return {
        "server": _server_projection(server),
        "sampled_at": sampled_at,
        "stale": bool(resources.get("stale", False)),
        "cpu": {
            "logical_cores": int(cpu.get("logical_cores", 0)),
            "load_1m": (
                float(cpu["load_1m"])
                if cpu.get("load_1m") is not None
                else None
            ),
        }
        if isinstance(cpu, dict)
        else None,
        "memory": {
            "total_bytes": int(memory.get("total_bytes", 0)),
            "available_bytes": int(memory.get("available_bytes", 0)),
        }
        if isinstance(memory, dict)
        else None,
        "disks": [
            {
                "mount": str(disk.get("mount", "")),
                "total_bytes": int(disk.get("total_bytes", 0)),
                "available_bytes": int(disk.get("available_bytes", 0)),
            }
            for disk in disks
            if isinstance(disk, dict) and disk.get("mount")
        ],
        "gpus": gpus,
    }


def _normalize_parameter_definition(definition: dict[str, Any]) -> dict[str, Any]:
    kind = definition.get("type", definition.get("kind", "string"))
    parameter_type = "number" if kind == "float" else kind
    raw_choices = definition.get("choices") or []
    choices = [
        choice
        if isinstance(choice, dict)
        else {"value": str(choice), "label": str(choice)}
        for choice in raw_choices
    ]
    return {
        "key": definition.get("key", definition.get("name")),
        "label": definition.get(
            "label", definition.get("name", definition.get("key", "parameter"))
        ),
        "type": parameter_type,
        "semantic_role": definition.get("semantic_role", "hyperparameter"),
        "default": definition.get("default"),
        "description": definition.get("description") or None,
        "minimum": definition.get("minimum"),
        "maximum": definition.get("maximum"),
        "choices": choices,
        "string_min_length": definition.get("string_min_length"),
        "string_max_length": definition.get("string_max_length"),
        "visible_when": definition.get("visible_when"),
        "display_group": definition.get("display_group"),
        "display_group_label": definition.get("display_group_label"),
        "display_group_order": definition.get("display_group_order"),
        "editable": bool(definition.get("editable", True)),
        "sensitive": bool(definition.get("sensitive", False)),
        "cli_flag": definition.get("cli_flag"),
        "argument_style": definition.get("argument_style")
        or ("flag_when_true" if parameter_type == "boolean" else "value"),
    }


def _normalize_model(model: dict[str, Any]) -> dict[str, Any]:
    result = {
        "family_ref": model["family_ref"],
        "family_name": model["family_name"],
        "status": model.get("status", "draft"),
        "edit_revision": int(model.get("edit_revision", 1)),
        "trained_version_count": int(model.get("trained_version_count", 0)),
        "available_version_count": int(model.get("available_version_count", 0)),
        "created_at": model.get("created_at"),
        "updated_at": model.get("updated_at"),
        "data_access_mode": model.get("data_access_mode", "self_managed"),
    }
    verification = model.get("verification")
    if isinstance(verification, dict):
        result["verification"] = {
            "verification_ref": verification.get("verification_ref"),
            "status": verification.get("status"),
            "requested_at": verification.get("requested_at"),
            "finished_at": verification.get("finished_at"),
            **(
                {"checks": verification["checks"]}
                if isinstance(verification.get("checks"), list)
                else {}
            ),
        }
    configuration = model.get("configuration")
    if isinstance(configuration, dict):
        definitions = configuration.get("parameter_definitions")
        normalized_configuration: dict[str, Any] = {}
        normalized_configuration["data_access_mode"] = configuration.get(
            "data_access_mode", model.get("data_access_mode", "self_managed")
        )
        if isinstance(definitions, list):
            normalized_configuration["parameter_definitions"] = [
                _normalize_parameter_definition(definition)
                for definition in definitions
            ]
        launch_template = configuration.get("launch_template")
        if isinstance(launch_template, dict):
            normalized_configuration["launch_template"] = launch_template
        result["configuration"] = normalized_configuration
    return result


def _run_spec_projection(spec: dict[str, Any]) -> dict[str, Any]:
    launcher_kind = spec.get("launcher_kind", "torchrun")
    master_port = spec.get("master_port")
    master_addr = spec.get("master_addr")
    node_rank = spec.get("node_rank")
    return {
        "contract_version": int(spec.get("contract_version", spec.get("version", 1))),
        "execution_mode": spec.get("execution_mode", spec.get("mode", "simulation")),
        "server_ref": spec["server_ref"],
        "gpu_uuids": spec.get("gpu_uuids", []),
        "nnodes": int(spec.get("nnodes", 1)),
        "launcher_kind": launcher_kind,
        "master_addr": (
            master_addr
            if master_addr is not None
            else "127.0.0.1" if launcher_kind == "torchrun" else None
        ),
        "master_port": int(master_port) if master_port is not None else None,
        "node_rank": int(node_rank) if node_rank is not None else None,
        "nproc_per_node": int(spec.get("nproc_per_node", 0)),
        "environment": spec.get("environment", {}),
        "runtime_environment": spec.get(
            "runtime_environment", {"kind": "system"}
        ),
        "monitoring": spec.get(
            "monitoring", {"source": "stdout", "format": "plain"}
        ),
        "parameters": spec.get("parameters", {}),
        "entrypoint": spec.get("entrypoint"),
        "argv": spec.get("argv", []),
        "output_preview": spec.get("output_preview"),
        "dataset_manifest": spec.get("dataset_manifest"),
    }


def _preview_projection(preview: dict[str, Any]) -> dict[str, Any]:
    if isinstance(preview.get("stages"), list):
        return {
            "stages": [
                {
                    "stage_number": int(stage["stage_number"]),
                    "stage_name": stage["stage_name"],
                    "stage_input_source": stage.get("stage_input_source", "manual"),
                    "parameters": stage.get("run_spec", {}).get("parameters", {}),
                    "run_spec": _run_spec_projection(stage["run_spec"]),
                    "command_preview": stage.get("command_preview", ""),
                    "output_directory": stage.get("output_directory"),
                    "preflight": stage.get("preflight", []),
                }
                for stage in preview["stages"]
            ],
            "dataset_manifest_preview": preview.get("dataset_manifest_preview"),
            "dataset_manifest_path": preview.get("dataset_manifest_path"),
            "preflight": preview.get("preflight", []),
        }
    raw_preflight = preview.get("preflight", [])
    if isinstance(raw_preflight, dict):
        checks = raw_preflight.get("checks") or []
        preflight = [
            {"ok": bool(raw_preflight.get("ok", False)), "message": str(check)}
            for check in checks
        ] or [
            {
                "ok": bool(raw_preflight.get("ok", False)),
                "message": raw_preflight.get("message", "Preflight completed."),
            }
        ]
    else:
        preflight = raw_preflight
    return {
        "run_spec": _run_spec_projection(preview["run_spec"]),
        "command_preview": preview.get("command_preview", ""),
        "preflight": preflight,
    }


def _normalize_run(run: dict[str, Any]) -> dict[str, Any]:
    progress = run.get("progress_percent")
    if progress is None:
        progress = float(run.get("progress", 0)) * 100
    failure = run.get("failure") if isinstance(run.get("failure"), dict) else {}
    run_spec = run.get("run_spec")
    raw_stages = run.get("stages", [])
    stages = [
        {
            "stage_ref": stage["stage_ref"],
            "stage_number": int(stage["stage_number"]),
            "stage_name": stage["stage_name"],
            "stage_input_source": stage.get("stage_input_source", "manual"),
            "status": stage["status"],
            "progress_percent": round(
                float(stage.get("progress_percent", float(stage.get("progress", 0)) * 100)),
                4,
            ),
            "current_step": int(stage.get("current_step", 0)),
            "total_steps": int(stage.get("total_steps", 0)),
            "current_epoch": float(stage.get("current_epoch", 0)),
            "total_epochs": float(stage.get("total_epochs", 0)),
            "parameters": stage.get("parameters", {}),
            "run_spec": (
                _run_spec_projection(stage["run_spec"])
                if isinstance(stage.get("run_spec"), dict)
                else None
            ),
            "command_preview": stage.get("command_preview"),
            "output_directory": stage.get("output_directory"),
            "artifact": stage.get("artifact"),
            "failure_code": stage.get(
                "failure_code",
                stage.get("failure", {}).get("code")
                if isinstance(stage.get("failure"), dict)
                else None,
            ),
            "failure_message": stage.get(
                "failure_message",
                stage.get("failure", {}).get("message")
                if isinstance(stage.get("failure"), dict)
                else None,
            ),
            "created_at": stage.get("created_at"),
            "started_at": stage.get("started_at"),
            "finished_at": stage.get("finished_at"),
        }
        for stage in raw_stages
        if isinstance(stage, dict)
    ]
    result = {
        "run_ref": run["run_ref"],
        "family_ref": run.get("family_ref"),
        "family_name": run.get("family_name"),
        "version_ref": run.get("version_ref"),
        "version_number": int(run.get("version_number", 1)),
        "version_date": run.get("version_date"),
        "version_label": run.get("version_label"),
        "version_description": run.get("version_description"),
        "dataset_snapshot": run.get("dataset_snapshot"),
        "status": run["status"],
        "execution_mode": run.get("execution_mode", run.get("mode", "simulation")),
        "execution_control_status": run.get("execution_control_status"),
        "execution_control_message": run.get("execution_control_message"),
        "last_execution_heartbeat_at": run.get("last_execution_heartbeat_at"),
        "state_revision": int(run.get("state_revision", 1)),
        "server_ref": run.get("server_ref", ""),
        "server_name": run.get("server_name", run.get("server_ref", "")),
        "gpu_uuids": run.get("gpu_uuids", []),
        "progress_percent": round(float(progress), 4),
        "current_step": int(run.get("current_step", 0)),
        "total_steps": int(run.get("total_steps", 0)),
        "current_epoch": float(run.get("current_epoch", 0)),
        "total_epochs": float(run.get("total_epochs", 0)),
        "latest_metric": run.get("latest_metric"),
        "failure_code": run.get("failure_code", failure.get("code")),
        "failure_message": run.get("failure_message", failure.get("message")),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "parameters": run.get("parameters", {}),
        "audit_events": run.get("audit_events", []),
        "stage_count": int(run.get("stage_count", len(stages) or 1)),
        "current_stage_number": run.get("current_stage_number"),
        "stages": stages,
        "version_model": run.get("version_model", run.get("version_model_output")),
        "artifacts": run.get("artifacts", []),
    }
    if isinstance(run_spec, dict):
        result["run_spec"] = _run_spec_projection(run_spec)
    return result


def _metric_projection(metric: dict[str, Any]) -> dict[str, Any]:
    result = dict(metric)
    gpus = result.pop("gpus", None)
    if isinstance(gpus, list) and gpus:
        utilization = [
            float(gpu["utilization_percent"])
            for gpu in gpus
            if gpu.get("utilization_percent") is not None
        ]
        memory = [
            float(gpu.get("gpu_memory_mib", gpu.get("memory_used_mib")))
            for gpu in gpus
            if gpu.get("gpu_memory_mib", gpu.get("memory_used_mib")) is not None
        ]
        if utilization:
            result["gpu_utilization_percent"] = sum(utilization) / len(utilization)
        if memory:
            result["gpu_memory_mib"] = sum(memory) / len(memory)
    return result


def _event_cursor(last_event_id: str | None) -> int:
    if last_event_id is None:
        return 0
    try:
        cursor = int(last_event_id, 10)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_event_cursor",
                "message": "Last-Event-ID must be a non-negative integer.",
            },
        ) from exc
    if cursor < 0:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_event_cursor",
                "message": "Last-Event-ID must be a non-negative integer.",
            },
        )
    return cursor


def _safe_event(raw_event: Any) -> dict[str, Any]:
    if not isinstance(raw_event, dict):
        raise TypeError("training service returned an invalid event")
    payload = raw_event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    event_id = raw_event.get("event_id", raw_event.get("seq"))
    event_type = raw_event.get("type", raw_event.get("event_type"))
    run_ref = raw_event.get("run_ref", payload.get("run_ref"))
    run_event_types = {"run.updated", "run.log.appended", "run.metric.appended"}
    dataset_event_types = {
        "dataset.transfer.updated",
        "dataset.replica.ready",
        "dataset.replica.removed",
    }
    model_version_event_types = {"model.version.artifact.updated"}
    if (
        type(event_id) is not int
        or event_id < 1
        or event_type
        not in run_event_types | dataset_event_types | model_version_event_types
        or (event_type in run_event_types and not isinstance(run_ref, str))
    ):
        raise TypeError("training service returned an invalid event")
    event: dict[str, Any] = {
        "event_id": event_id,
        "type": event_type,
    }
    if event_type in run_event_types:
        event["run_ref"] = run_ref
    elif event_type in model_version_event_types:
        version_ref = raw_event.get("version_ref", payload.get("version_ref"))
        if not isinstance(version_ref, str) or not _SAFE_REF.fullmatch(version_ref):
            raise TypeError("training service returned an invalid event")
        event["version_ref"] = version_ref
        for field in ("status", "availability"):
            value = raw_event.get(field, payload.get(field))
            if isinstance(value, str) and len(value) <= 50:
                event[field] = value
        return event
    else:
        for field in ("transfer_ref", "replica_ref", "release_ref", "status"):
            value = raw_event.get(field, payload.get(field))
            if isinstance(value, str) and (
                field == "status" or _SAFE_REF.fullmatch(value)
            ):
                event[field] = value
        return event
    item_seq = raw_event.get(
        "item_seq",
        raw_event.get("resource_seq", payload.get("seq")),
    )
    if type(item_seq) is int and item_seq >= 0:
        event["seq"] = item_seq
    stage_ref = raw_event.get("stage_ref", payload.get("stage_ref"))
    stage_number = raw_event.get("stage_number", payload.get("stage_number"))
    if isinstance(stage_ref, str) and _SAFE_REF.fullmatch(stage_ref):
        event["stage_ref"] = stage_ref
    if type(stage_number) is int and 1 <= stage_number <= 10:
        event["stage_number"] = stage_number
    return event


def _model_projection(
    model: dict[str, Any], principal: TrainingPrincipal
) -> dict[str, Any]:
    if principal.can("training:manage_models"):
        return model
    projected = dict(model)
    projected.pop("edit_revision", None)
    verification = projected.get("verification")
    if isinstance(verification, dict):
        projected["verification"] = {
            key: verification.get(key)
            for key in (
                "verification_ref",
                "status",
                "requested_at",
                "finished_at",
            )
        }
    configuration = projected.get("configuration")
    if isinstance(configuration, dict):
        safe_configuration = dict(configuration)
        definitions = safe_configuration.get("parameter_definitions")
        if isinstance(definitions, list):
            sensitive_keys = {
                definition.get("key")
                for definition in definitions
                if isinstance(definition, dict) and definition.get("sensitive")
            }
            safe_definitions: list[Any] = []
            for definition in definitions:
                if not isinstance(definition, dict):
                    safe_definitions.append(definition)
                    continue
                safe_definition = dict(definition)
                if safe_definition.get("sensitive"):
                    safe_definition["default"] = "********"
                condition = safe_definition.get("visible_when")
                if (
                    isinstance(condition, dict)
                    and condition.get("parameter_key") in sensitive_keys
                ):
                    # The condition value can itself be a secret.  Hiding the
                    # condition is safe because the server remains the source
                    # of truth for whether the dependent parameter is active.
                    safe_definition["visible_when"] = None
                safe_definitions.append(safe_definition)
            safe_configuration["parameter_definitions"] = safe_definitions
        template = safe_configuration.get("launch_template")
        if isinstance(template, dict):
            safe_configuration["launch_template"] = {
                key: template[key]
                for key in ("domain", "server_ref")
                if key in template
            }
        projected["configuration"] = safe_configuration
    return projected


def _model_version_family_projection(
    family: dict[str, Any], principal: TrainingPrincipal
) -> dict[str, Any]:
    projected = dict(family)
    latest = projected.get("latest_version")
    if isinstance(latest, dict):
        safe_latest = _model_version_projection(latest, principal)
        projected["latest_version"] = {
            key: safe_latest.get(key)
            for key in (
                "version_ref", "version_label", "version_description", "finished_at"
            )
        }
    return projected


def _model_version_projection(
    version: dict[str, Any], principal: TrainingPrincipal
) -> dict[str, Any]:
    projected = dict(version)
    if principal.can("training:create_runs"):
        return projected
    artifact = projected.get("default_artifact")
    if isinstance(artifact, dict):
        safe_artifact = dict(artifact)
        safe_artifact.pop("path", None)
        safe_artifact.pop("message", None)
        projected["default_artifact"] = safe_artifact
    stages = projected.get("stages")
    if isinstance(stages, list):
        projected["stages"] = [
            {
                key: stage.get(key)
                for key in (
                    "stage_ref", "stage_number", "stage_name", "status",
                    "current_step", "total_steps", "progress", "failure_code",
                    "failure_message", "started_at", "finished_at"
                )
            }
            for stage in stages
            if isinstance(stage, dict)
        ]
    artifacts = projected.get("artifacts")
    if isinstance(artifacts, list):
        projected["artifacts"] = [
            {
                key: artifact_item.get(key)
                for key in (
                    "artifact_ref", "kind", "stage_ref", "stage_number", "step",
                    "relative_path", "created_at"
                )
                if key in artifact_item
            }
            for artifact_item in artifacts
            if isinstance(artifact_item, dict)
        ]
    return projected


def _run_projection(
    run: dict[str, Any], principal: TrainingPrincipal
) -> dict[str, Any]:
    if principal.can("training:create_runs"):
        return run
    projected = dict(run)
    projected.pop("run_spec", None)
    projected.pop("version_model", None)
    projected.pop("dataset_snapshot", None)
    projected.pop("artifacts", None)
    stages = projected.get("stages")
    if isinstance(stages, list):
        projected["stages"] = [
            {
                key: stage.get(key)
                for key in (
                    "stage_ref",
                    "stage_number",
                    "stage_name",
                    "status",
                    "current_step",
                    "total_steps",
                    "failure_code",
                    "failure_message",
                    "started_at",
                    "finished_at",
                )
            }
            for stage in stages
            if isinstance(stage, dict)
        ]
    return projected


def _safe_error_current(value: Any) -> Any | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_safe_error_current(item) for item in value[:100]]
    if isinstance(value, dict):
        blocked = {
            "argv",
            "command",
            "entrypoint",
            "output_directory",
            "output_root",
            "password",
            "secret",
            "token",
            "working_directory",
        }
        return {
            str(key): _safe_error_current(item)
            for key, item in value.items()
            if str(key).lower() not in blocked
        }
    return None

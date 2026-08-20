from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel

from .auth import (
    TRAINING_CREATE_RUNS,
    TRAINING_MANAGE_MODELS,
    TRAINING_MANAGE_NODES,
    TRAINING_STOP_RUNS,
)
from .datasets import DatasetReleaseCatalog, PublishedDatasetCatalog
from .errors import (
    TrainingConflictError,
    TrainingError,
    TrainingForbiddenError,
    TrainingUnavailableError,
    TrainingValidationError,
)
from .models import ParameterDefinition, normalize_parameter_value
from .resources import FakeResourceProvider, TrainingResourceProvider
from .store import TrainingStore


class TrainingNodeDeploymentManager(Protocol):
    def discover_host_key(self, node: dict[str, Any]) -> dict[str, str]: ...

    def deploy_worker(
        self,
        *,
        node: dict[str, Any],
        confirmed_host_key: dict[str, str],
        ssh_password: str,
        sudo_password_mode: str,
        sudo_password: str | None,
        enrollment_token: str,
        force_reenrollment: bool = False,
    ) -> dict[str, str]: ...

    def preflight_worker(
        self,
        *,
        node: dict[str, Any],
        confirmed_host_key: dict[str, str],
        ssh_password: str,
        sudo_password_mode: str,
        sudo_password: str | None,
    ) -> dict[str, Any]: ...

    def remove_worker(
        self,
        *,
        node: dict[str, Any],
        ssh_password: str,
        sudo_password_mode: str,
        sudo_password: str | None,
    ) -> dict[str, str]: ...


class TrainingService:
    def __init__(
        self,
        store: TrainingStore,
        provider: FakeResourceProvider | TrainingResourceProvider,
        *,
        simulation_enabled: bool = True,
        real_execution_enabled: bool = False,
        node_deployment_manager: TrainingNodeDeploymentManager | None = None,
        repair_heartbeat_timeout_seconds: float = 8.0,
        heartbeat_poll_interval_seconds: float = 0.25,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        dataset_catalog: DatasetReleaseCatalog | None = None,
    ) -> None:
        self.store, self.provider = store, provider
        self.simulation_enabled = simulation_enabled
        self.real_execution_enabled = real_execution_enabled
        self.node_deployment_manager = node_deployment_manager
        self.repair_heartbeat_timeout_seconds = max(
            0.0, float(repair_heartbeat_timeout_seconds)
        )
        self.heartbeat_poll_interval_seconds = max(
            0.01, float(heartbeat_poll_interval_seconds)
        )
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self.dataset_catalog = dataset_catalog or PublishedDatasetCatalog(
            store.path.with_name("annotation.sqlite")
        )

    def _wait_for_node_heartbeat(
        self, node_ref: str, *, after_revision: int
    ) -> bool:
        deadline = (
            self._monotonic_clock() + self.repair_heartbeat_timeout_seconds
        )
        while True:
            node = self.store.get_node(node_ref)
            if int(node["heartbeat_revision"]) > after_revision:
                return True
            remaining = deadline - self._monotonic_clock()
            if remaining <= 0:
                return False
            self._sleeper(
                min(self.heartbeat_poll_interval_seconds, remaining)
            )

    @staticmethod
    def _data(payload: Any) -> dict[str, Any]:
        if isinstance(payload, BaseModel): return payload.model_dump(mode="json", exclude_none=False)
        return dict(payload)

    @staticmethod
    def _require(principal: Any, permission: str) -> None:
        if not principal.can(permission):
            raise TrainingForbiddenError("training_write_forbidden", "This deployment does not allow that training operation.")

    def capabilities(self, principal: Any) -> dict[str, Any]:
        deployment_enabled = self.node_deployment_manager is not None
        return {
            **principal.public_projection(),
            "simulation_enabled": self.simulation_enabled,
            "real_execution_enabled": self.real_execution_enabled,
            "training_execution_v1": self.real_execution_enabled,
            "real_execution_disabled_reason": None
            if self.real_execution_enabled
            else "Real training is disabled by deployment configuration.",
            "node_deployment_enabled": deployment_enabled,
            "node_deployment_disabled_reason": None
            if deployment_enabled
            else "Training Worker deployment requires a configured HTTPS center URL.",
        }

    def list_servers(self) -> list[dict[str, Any]]: return self.provider.list_servers()
    def get_server_resources(self, server_ref: str) -> dict[str, Any]: return self.provider.resources(server_ref)

    def list_nodes(self) -> list[dict[str, Any]]:
        return self.store.list_nodes()

    def get_node(self, node_ref: str) -> dict[str, Any]:
        return self.store.get_node(node_ref)

    def create_node(self, payload: Any, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_NODES)
        return self.store.create_node(self._data(payload), principal.subject)

    def update_node(
        self, node_ref: str, payload: Any, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_NODES)
        data = self._data(payload)
        expected_revision = int(data.pop("expected_revision"))
        return self.store.update_node(
            node_ref, expected_revision, data, principal.subject
        )

    def delete_node(
        self, node_ref: str, expected_revision: int, principal: Any
    ) -> None:
        self._require(principal, TRAINING_MANAGE_NODES)
        self.store.assert_node_has_no_active_real_runs(node_ref)
        self.store.delete_node(node_ref, expected_revision, principal.subject)

    def create_enrollment_token(
        self, node_ref: str, payload: Any, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_NODES)
        data = self._data(payload)
        return self.store.create_enrollment_token(
            node_ref,
            int(data["expected_revision"]),
            int(data["expires_in_seconds"]),
            principal.subject,
        )

    def discover_node_host_key(
        self, node_ref: str, principal: Any
    ) -> dict[str, str]:
        self._require(principal, TRAINING_MANAGE_NODES)
        if self.node_deployment_manager is None:
            raise TrainingUnavailableError(
                "training_node_deployment_unavailable",
                "Training Worker deployment is not configured on this server.",
            )
        return self.node_deployment_manager.discover_host_key(
            self.store.get_node(node_ref)
        )

    def deploy_node_worker(
        self, node_ref: str, payload: Any, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_NODES)
        self.store.assert_node_has_no_active_real_runs(node_ref)
        if self.node_deployment_manager is None:
            raise TrainingUnavailableError(
                "training_node_deployment_unavailable",
                "Training Worker deployment is not configured on this server.",
            )
        confirmed = payload.confirmed_host_key.model_dump(mode="json")
        ssh_password = payload.ssh_password.get_secret_value()
        sudo_password = (
            payload.sudo_password.get_secret_value()
            if payload.sudo_password is not None
            else None
        )
        deploying = self.store.begin_node_deployment(
            node_ref,
            int(payload.expected_revision),
            host_key_algorithm=confirmed["algorithm"],
            host_public_key=confirmed["public_key"],
            host_key_fingerprint=confirmed["sha256_fingerprint"],
            actor=principal.subject,
        )
        heartbeat_revision = int(deploying["heartbeat_revision"])
        is_repair = (
            deploying["status"] != "online"
            and any(
                deploying.get(field) is not None
                for field in (
                    "installed_worker_version",
                    "worker_instance_id",
                    "enrolled_at",
                )
            )
        )
        try:
            token_result = self.store.create_enrollment_token(
                node_ref,
                int(deploying["state_revision"]),
                600,
                principal.subject,
            )
            deployment = self.node_deployment_manager.deploy_worker(
                node={
                    **token_result["node"],
                    "ssh_username": payload.ssh_username,
                },
                confirmed_host_key=confirmed,
                ssh_password=ssh_password,
                sudo_password_mode=payload.sudo_password_mode,
                sudo_password=sudo_password,
                enrollment_token=token_result["enrollment_token"],
                force_reenrollment=False,
            )
            credentials_refreshed = False
            if is_repair and not self._wait_for_node_heartbeat(
                node_ref, after_revision=heartbeat_revision
            ):
                deployment = self.node_deployment_manager.deploy_worker(
                    node={
                        **token_result["node"],
                        "ssh_username": payload.ssh_username,
                    },
                    confirmed_host_key=confirmed,
                    ssh_password=ssh_password,
                    sudo_password_mode=payload.sudo_password_mode,
                    sudo_password=sudo_password,
                    enrollment_token=token_result["enrollment_token"],
                    force_reenrollment=True,
                )
                credentials_refreshed = True
                if not self._wait_for_node_heartbeat(
                    node_ref, after_revision=heartbeat_revision
                ):
                    raise TrainingUnavailableError(
                        "training_node_worker_heartbeat_not_restored",
                        "Worker restarted but did not reconnect to the center service.",
                    )
        except Exception as exc:
            self.store.invalidate_node_enrollment_tokens(node_ref)
            message = (
                str(exc)
                if isinstance(exc, TrainingError)
                else "Training Worker deployment failed."
            )
            self.store.finish_node_deployment(
                node_ref,
                succeeded=False,
                message=message,
                worker_version=None,
                actor=principal.subject,
            )
            raise
        self.store.invalidate_node_enrollment_tokens(node_ref)
        worker_version = deployment.get("worker_version", "0.1.0")
        deployment_message = deployment.get(
            "message", "Training Worker deployed."
        )
        if is_repair:
            deployment_message = (
                "Training Worker repaired and credentials refreshed."
                if credentials_refreshed
                else "Training Worker repaired and reconnected."
            )
        node = self.store.finish_node_deployment(
            node_ref,
            succeeded=True,
            message=deployment_message,
            worker_version=worker_version,
            actor=principal.subject,
            ssh_username=payload.ssh_username,
        )
        return {
            "node": node,
            "deployment": {
                "status": "succeeded",
                "worker_version": worker_version,
                "message": deployment_message,
            },
        }

    def preflight_node_worker(
        self, node_ref: str, payload: Any, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_NODES)
        if self.node_deployment_manager is None:
            raise TrainingUnavailableError(
                "training_node_deployment_unavailable",
                "Training Worker deployment is not configured on this server.",
            )
        node = {
            **self.store.get_node(node_ref),
            "ssh_username": payload.ssh_username,
        }
        expected_revision = int(payload.expected_revision)
        if int(node["state_revision"]) != expected_revision:
            raise TrainingConflictError(
                "training_node_revision_conflict",
                "Training node was changed by another operation.",
                current=node,
            )
        confirmed = payload.confirmed_host_key.model_dump(mode="json")
        sudo_password = (
            payload.sudo_password.get_secret_value()
            if payload.sudo_password is not None
            else None
        )
        return self.node_deployment_manager.preflight_worker(
            node=node,
            confirmed_host_key=confirmed,
            ssh_password=payload.ssh_password.get_secret_value(),
            sudo_password_mode=payload.sudo_password_mode,
            sudo_password=sudo_password,
        )

    def remove_node_worker(
        self, node_ref: str, payload: Any, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_NODES)
        self.store.assert_node_has_no_active_real_runs(node_ref)
        if self.node_deployment_manager is None:
            raise TrainingUnavailableError(
                "training_node_deployment_unavailable",
                "Training Worker management is not configured on this server.",
            )
        ssh_password = payload.ssh_password.get_secret_value()
        sudo_password = (
            payload.sudo_password.get_secret_value()
            if payload.sudo_password is not None
            else None
        )
        removing = self.store.begin_node_worker_removal(
            node_ref,
            int(payload.expected_revision),
            actor=principal.subject,
        )
        try:
            removal = self.node_deployment_manager.remove_worker(
                node={**removing, "ssh_username": payload.ssh_username},
                ssh_password=ssh_password,
                sudo_password_mode=payload.sudo_password_mode,
                sudo_password=sudo_password,
            )
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, TrainingError)
                else "Training Worker removal failed."
            )
            self.store.finish_node_worker_removal(
                node_ref,
                succeeded=False,
                message=message,
                actor=principal.subject,
            )
            raise
        node = self.store.finish_node_worker_removal(
            node_ref,
            succeeded=True,
            message=removal.get("message", "Training Worker removed."),
            actor=principal.subject,
        )
        return {
            "node": node,
            "removal": {
                "status": "succeeded",
                "message": removal.get("message", "Training Worker removed."),
            },
        }

    def enroll_node(self, payload: Any) -> dict[str, Any]:
        return self.store.enroll_node(self._data(payload))

    def heartbeat_node(
        self, node_ref: str, worker_token: str, payload: Any
    ) -> dict[str, Any]:
        data = self._data(payload)
        node = self.store.record_node_heartbeat(node_ref, worker_token, data)
        verification = self.store.claim_model_verification(
            node_ref, data["worker_instance_id"]
        )
        commands = (
            []
            if verification is not None
            else self.store.claim_node_commands(
                node_ref, worker_token, data["worker_instance_id"], 1
            )
        )
        return {
            "node": node,
            "command": verification or (commands[0] if commands else None),
        }

    def poll_node_commands(
        self, node_ref: str, worker_token: str, payload: Any
    ) -> dict[str, Any]:
        data = self._data(payload)
        deadline = self._monotonic_clock() + float(data.get("wait_seconds", 25))
        while True:
            commands = self.store.claim_node_commands(
                node_ref,
                worker_token,
                data["worker_instance_id"],
                int(data.get("limit", 1)),
            )
            if commands or self._monotonic_clock() >= deadline:
                return {"commands": commands}
            self._sleeper(min(0.25, max(0.0, deadline - self._monotonic_clock())))

    def finish_node_command(
        self, node_ref: str, command_ref: str, worker_token: str, payload: Any
    ) -> dict[str, Any]:
        return self.store.finish_node_command(
            node_ref, worker_token, command_ref, self._data(payload)
        )

    def finish_model_verification(
        self,
        node_ref: str,
        command_ref: str,
        worker_token: str,
        payload: Any,
    ) -> dict[str, Any]:
        return self.store.finish_model_verification(
            node_ref,
            worker_token,
            command_ref,
            self._data(payload),
        )

    def get_node_resources(self, node_ref: str) -> dict[str, Any]:
        return self.store.get_node_resources(node_ref)

    def list_models(self, *, include_private: bool = False) -> list[dict[str, Any]]:
        models = self.store.list_models()
        if include_private:
            models = [
                self.store.get_model(item["family_ref"], include_private=True)
                for item in models
            ]
        return [self._project_model(item) for item in models]

    def get_model(self, family_ref: str, *, include_private: bool = False) -> dict[str, Any]:
        return self._project_model(
            self.store.get_model(family_ref, include_private=include_private)
        )

    def create_model(self, payload: Any, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_MODELS)
        data = self._adapt_model(self._data(payload))
        self._require_registered_server(data["launch_template"]["server_ref"])
        return self._project_model(self.store.create_model(data, principal.subject))

    def update_model(self, family_ref: str, payload: Any, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_MODELS)
        data = self._adapt_model(self._data(payload)); expected = int(data.pop("expected_revision"))
        self._require_registered_server(data["launch_template"]["server_ref"])
        return self._project_model(self.store.update_model(family_ref, expected, data, principal.subject))

    def verify_model(
        self, family_ref: str, payload: Any, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_MODELS)
        data = self._data(payload)
        return self._project_model(
            self.store.request_model_verification(
                family_ref,
                int(data["expected_revision"]),
                principal.subject,
            )
        )

    def _require_registered_server(self, server_ref: str) -> None:
        if not any(
            server["server_ref"] == server_ref
            for server in self.provider.list_servers()
        ):
            raise TrainingValidationError(
                "server_not_found",
                "请先登记训练节点，并从已登记节点中选择模型运行位置。",
            )

    @staticmethod
    def _project_model(model: dict[str, Any]) -> dict[str, Any]:
        result = dict(model)
        if "parameter_definitions" in model:
            definitions = []
            for item in model["parameter_definitions"]:
                visible_when = item.get("visible_when")
                choices = [choice if isinstance(choice, dict) else {"value": choice, "label": choice} for choice in (item.get("choices") or [])]
                definitions.append({"key": item["name"], "label": item.get("label", item["name"]), "type": "number" if item["kind"] == "float" else item["kind"], "semantic_role": "hyperparameter" if item.get("semantic_role") == "dataset" else item.get("semantic_role", "hyperparameter"), "default": item["default"], "description": item.get("description"), "minimum": item.get("minimum"), "maximum": item.get("maximum"), "choices": choices, "string_min_length": item.get("string_min_length"), "string_max_length": item.get("string_max_length"), "visible_when": {"parameter_key": visible_when["parameter_name"], "equals": visible_when["equals"]} if visible_when else None, "display_group": item.get("display_group"), "display_group_label": item.get("display_group_label"), "display_group_order": item.get("display_group_order"), "editable": True, "sensitive": item.get("sensitive", False), "cli_flag": item.get("cli_flag"), "argument_style": item.get("argument_style") or ("flag_when_true" if item["kind"] == "boolean" else "value")})
            configuration: dict[str, Any] = {
                "parameter_definitions": definitions,
                "data_access_mode": model.get("data_access_mode", "self_managed"),
            }
            template = model.get("launch_template")
            if isinstance(template, dict):
                projected_template = dict(template)
                if "executable" in template:
                    projected_template.setdefault(
                        "launcher_kind",
                        "torchrun"
                        if str(template["executable"]).rsplit("/", 1)[-1]
                        == "torchrun"
                        else "direct",
                    )
                    projected_template.setdefault("output_flag", "--output_dir")
                configuration["launch_template"] = projected_template
            result["configuration"] = configuration
            result["data_access_mode"] = model.get(
                "data_access_mode", "self_managed"
            )
        for internal in ("parameter_definitions", "launch_template", "output_template"):
            result.pop(internal, None)
        return result

    def _adapt_model(self, data: dict[str, Any]) -> dict[str, Any]:
        configuration = data.pop("configuration")
        definitions = []
        for item in configuration["parameter_definitions"]:
            kind = "float" if item["type"] == "number" else item["type"]
            visible_when = item.get("visible_when")
            definitions.append({"name": item["key"], "label": item["label"], "kind": kind, "semantic_role": item.get("semantic_role", "hyperparameter"), "default": item["default"], "description": item.get("description") or "", "minimum": item.get("minimum"), "maximum": item.get("maximum"), "choices": item.get("choices") or None, "string_min_length": item.get("string_min_length"), "string_max_length": item.get("string_max_length"), "visible_when": {"parameter_name": visible_when["parameter_key"], "equals": visible_when["equals"]} if visible_when else None, "display_group": item.get("display_group"), "display_group_label": item.get("display_group_label"), "display_group_order": item.get("display_group_order"), "editable": True, "sensitive": item.get("sensitive", False), "cli_flag": item.get("cli_flag") or f"--{item['key']}", "argument_style": item.get("argument_style") or ("flag_when_true" if kind == "boolean" else "value")})
        adapted = {
            **data,
            "launch_template": configuration["launch_template"],
            "parameter_definitions": definitions,
            "data_access_mode": configuration.get(
                "data_access_mode", "self_managed"
            ),
        }
        return adapted

    def list_dataset_releases(self) -> list[dict[str, Any]]:
        releases: list[dict[str, Any]] = []
        for release in self.dataset_catalog.list_releases():
            source_manifest = self.store.get_source_manifest_by_release(
                release["release_ref"]
            )
            releases.append({**release, "source_manifest": source_manifest})
        return releases

    def list_dataset_replicas(self, node_ref: str, principal: Any) -> list[dict[str, Any]]:
        self.store.get_node(node_ref)
        items = self.store.list_dataset_replicas(node_ref)
        if principal.can(TRAINING_CREATE_RUNS):
            return items
        return [
            {key: value for key, value in item.items() if key != "local_root"}
            for item in items
        ]

    def request_directory_listing(
        self, node_ref: str, payload: Any, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        self._require_dataset_worker_feature(node_ref, "directory_browser_v1")
        return self.store.create_directory_listing(
            node_ref, self._data(payload)["path"], principal.subject
        )

    def get_directory_listing(self, listing_ref: str, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        return self.store.get_directory_listing(listing_ref)

    def create_dataset_transfers(
        self, payload: Any, idempotency_key: str, principal: Any
    ) -> list[dict[str, Any]]:
        self._require(principal, TRAINING_CREATE_RUNS)
        data = self._data(payload)
        self._require_dataset_worker_feature(data["node_ref"], "dataset_transfer_v1")
        releases = {
            release["release_ref"]: release
            for release in self.dataset_catalog.list_releases()
        }
        selected: list[dict[str, Any]] = []
        for release_ref in data["release_refs"]:
            release = releases.get(release_ref)
            if release is None or release.get("status") != "released":
                raise TrainingValidationError(
                    "dataset_release_not_available",
                    "Only released dataset dates can be transferred.",
                )
            selected.append(self.store.ensure_source_manifest_placeholder(release))
        return self.store.create_dataset_transfers(
            node_ref=data["node_ref"],
            source_manifests=selected,
            target_parent_directory=data["target_parent_directory"],
            idempotency_key=idempotency_key,
            request_payload=data,
            actor=principal.subject,
        )

    def list_dataset_transfers(
        self, *, node_ref: str | None = None, status: str | None = None, principal: Any
    ) -> list[dict[str, Any]]:
        items = self.store.list_dataset_transfers(node_ref=node_ref, status=status)
        if principal.can(TRAINING_CREATE_RUNS):
            return items
        return [
            {
                key: value
                for key, value in item.items()
                if key not in {"target_parent_directory", "final_directory"}
            }
            for item in items
        ]

    def get_dataset_transfer(self, transfer_ref: str, principal: Any) -> dict[str, Any]:
        item = self.store.get_dataset_transfer(transfer_ref)
        if principal.can(TRAINING_CREATE_RUNS):
            return item
        return {
            key: value
            for key, value in item.items()
            if key not in {"target_parent_directory", "final_directory"}
        }

    def cancel_dataset_transfer(
        self, transfer_ref: str, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        return self.store.cancel_dataset_transfer(transfer_ref, principal.subject)

    def pause_dataset_transfer(
        self, transfer_ref: str, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        return self.store.pause_dataset_transfer(transfer_ref, principal.subject)

    def retry_dataset_transfer(
        self, transfer_ref: str, idempotency_key: str, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        return self.store.retry_dataset_transfer(
            transfer_ref, idempotency_key, principal.subject
        )

    def remove_dataset_replica(
        self, replica_ref: str, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        replica = self.store.get_dataset_replica(replica_ref)
        self._require_dataset_worker_feature(
            replica["node_ref"], "dataset_transfer_v1"
        )
        return self.store.remove_dataset_replica(replica_ref, principal.subject)

    def _require_dataset_worker_feature(self, node_ref: str, feature: str) -> None:
        node = self.store.get_node(node_ref)
        capabilities = node.get("capabilities") or {}
        features = capabilities.get("worker_features") or []
        if feature not in features:
            raise TrainingConflictError(
                "training_worker_update_required",
                "Update the Training Worker before using managed training data.",
                current={"node_ref": node_ref, "required_feature": feature},
            )

    def source_manifest_page(
        self,
        node_ref: str,
        release_ref: str,
        worker_token: str,
        *,
        cursor: int,
        limit: int,
    ) -> dict[str, Any]:
        self.store.authenticate_worker(node_ref, worker_token)
        self.store.authorize_source_download(node_ref, release_ref)
        return self.store.source_manifest_page(
            release_ref, cursor=cursor, limit=limit
        )

    def source_file(
        self, node_ref: str, file_ref: str, worker_token: str
    ) -> dict[str, Any]:
        self.store.authenticate_worker(node_ref, worker_token)
        item = self.store.get_source_file(file_ref)
        self.store.authorize_source_download(node_ref, item["release_ref"])
        return item

    def poll_training_actions(
        self, node_ref: str, worker_token: str, payload: Any
    ) -> dict[str, Any]:
        data = self._data(payload)
        wait_seconds = max(0, min(int(data.get("wait_seconds") or 0), 30))
        deadline = self._monotonic_clock() + wait_seconds
        while True:
            result = self.store.poll_training_actions(
                node_ref,
                worker_token,
                str(data["worker_instance_id"]),
                int(data.get("limit") or 1),
            )
            if result["actions"] or self._monotonic_clock() >= deadline:
                return result
            self._sleeper(min(0.25, deadline - self._monotonic_clock()))

    def finish_training_action(
        self,
        node_ref: str,
        action_ref: str,
        worker_token: str,
        payload: Any,
    ) -> dict[str, Any]:
        data = self._data(payload)
        return self.store.finish_training_action(
            node_ref,
            worker_token,
            action_ref,
            worker_instance_id=str(data["worker_instance_id"]),
            claim_token=str(data["claim_token"]),
            status=str(data["status"]),
            result=dict(data.get("result") or {}),
            error=dict(data.get("error") or {}) if data.get("error") else None,
        )

    def update_training_run(
        self,
        node_ref: str,
        run_ref: str,
        worker_token: str,
        payload: Any,
    ) -> dict[str, Any]:
        data = self._data(payload)
        return self._project_run(
            self.store.apply_training_run_updates(
                node_ref,
                worker_token,
                run_ref,
                worker_instance_id=str(data["worker_instance_id"]),
                owner_epoch=int(data["owner_epoch"]),
                worker_seq=int(data["worker_seq"]),
                updates=list(data["updates"]),
            )
        )

    def _prepare(
        self,
        payload: Any,
        *,
        ignore_platform_leases: bool = False,
        allow_real_preview: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        data = self._data(payload)
        execution_mode = data.get("execution_mode", data.get("mode"))
        if execution_mode not in {"simulation", "real"}:
            raise TrainingValidationError(
                "unsupported_execution_mode",
                "The requested execution mode is not supported.",
            )
        server = next((item for item in self.provider.list_servers() if item["server_ref"] == data["server_ref"]), None)
        if execution_mode == "simulation":
            if not self.simulation_enabled:
                raise TrainingForbiddenError(
                    "simulation_disabled", "Training simulation is disabled."
                )
            if server is not None and server.get("kind") != "simulation":
                raise TrainingValidationError(
                    "real_execution_disabled",
                    "真实训练尚未启用；真实训练节点当前只允许生成预览。",
                )
        else:
            if not allow_real_preview and not self.real_execution_enabled:
                raise TrainingValidationError(
                    "real_execution_disabled",
                    "Real training is disabled by deployment configuration.",
                )
            if server is not None and server.get("kind") != "training_node":
                raise TrainingValidationError(
                    "invalid_real_preview_target",
                    "真实训练预览必须选择已登记的真实训练节点。",
                )
        selected = self.provider.require_available(
            data["server_ref"], data["gpu_uuids"],
            ignore_platform_leases=ignore_platform_leases,
        )
        model = self.store.get_model_record(data["family_ref"])
        if model["status"] == "disabled":
            raise TrainingValidationError("model_unavailable", "The selected model family is disabled.")
        if model["launch_template"]["server_ref"] != data["server_ref"]:
            raise TrainingValidationError("server_mismatch", "The model template belongs to another server.")
        if execution_mode == "real" and not allow_real_preview:
            if model["status"] != "verified":
                raise TrainingValidationError(
                    "real_training_model_not_verified",
                    "The current model configuration must be verified before real training.",
                )
            node = self.store.get_node(data["server_ref"])
            capabilities = node.get("capabilities") or {}
            features = set(capabilities.get("worker_features") or [])
            if node.get("status") != "online":
                raise TrainingValidationError(
                    "real_training_node_unavailable",
                    "The selected Training Worker node must be online.",
                )
            if not (
                capabilities.get("training_execution_v1") is True
                or "training_execution_v1" in features
            ):
                raise TrainingValidationError(
                    "real_training_worker_upgrade_required",
                    "Update the Training Worker before starting real training.",
                )

        data_access_mode = model.get("data_access_mode", "self_managed")
        selection = data.get("dataset_selection")
        dataset_splits: dict[str, list[dict[str, Any]]] | None = None
        if data_access_mode == "datapilot_managed":
            if not selection or not selection.get("train_replica_refs"):
                raise TrainingValidationError(
                    "managed_dataset_training_set_required",
                    "DataPilot managed data requires at least one training date.",
                )
            dataset_splits = self.store.resolve_dataset_selection(
                node_ref=data["server_ref"],
                train_replica_refs=list(selection.get("train_replica_refs") or []),
                test_replica_refs=list(selection.get("test_replica_refs") or []),
            )
        elif selection and (
            selection.get("train_replica_refs") or selection.get("test_replica_refs")
        ):
            raise TrainingValidationError(
                "self_managed_dataset_selection_not_allowed",
                "This model family manages its own training data.",
            )

        definitions = [ParameterDefinition.model_validate(item) for item in model["parameter_definitions"]]
        by_name = {item.name: item for item in definitions}
        stage_input = next((item for item in definitions if item.semantic_role == "stage_input"), None)
        normalized_stages: list[dict[str, Any]] = []

        def normalize_stage(stage: dict[str, Any], stage_number: int) -> dict[str, Any]:
            supplied = dict(stage.get("parameters") or {})
            source = stage.get("stage_input_source", "manual")
            if stage_number == 1 and source != "manual":
                raise TrainingValidationError("invalid_stage_input_source", "The first stage must use manual input.")
            if source == "previous_stage_output" and stage_input is None:
                raise TrainingValidationError("stage_input_not_registered", "The model family has no stage input parameter; use manual input for every stage.")
            if set(supplied) - set(by_name):
                raise TrainingValidationError("unknown_parameter", f"Stage {stage_number} contains an unknown training parameter.")
            normalized: dict[str, Any] = {}
            sensitive: list[str] = []
            active: dict[str, bool] = {}
            resolving: set[str] = set()

            def resolve(definition: ParameterDefinition) -> bool:
                if definition.name in active:
                    return active[definition.name]
                if definition.name in resolving:
                    raise TrainingValidationError("invalid_parameter_dependency", "The model contains a cyclic parameter dependency.")
                resolving.add(definition.name)
                if (
                    source == "previous_stage_output"
                    and stage_input is not None
                    and definition.name == stage_input.name
                ):
                    # The real value is the previous stage's output directory,
                    # which is only known while RunSpecs are built.  Validate
                    # that real path below instead of validating a placeholder.
                    resolving.remove(definition.name)
                    active[definition.name] = True
                    if definition.sensitive:
                        sensitive.append(definition.name)
                    return True
                condition = definition.visible_when
                if condition is not None:
                    controller = by_name.get(condition.parameter_name)
                    if controller is None:
                        raise TrainingValidationError("invalid_parameter_dependency", "The model contains an unknown dependency controller.")
                    if not resolve(controller) or normalized.get(controller.name) != condition.equals:
                        resolving.remove(definition.name)
                        active[definition.name] = False
                        return False
                try:
                    normalized[definition.name] = normalize_parameter_value(
                        definition, supplied.get(definition.name, definition.default)
                    )
                except ValueError as exc:
                    raise TrainingValidationError("invalid_parameter", f"Stage {stage_number}: {exc}") from exc
                resolving.remove(definition.name)
                active[definition.name] = True
                if definition.sensitive:
                    sensitive.append(definition.name)
                return True

            for definition in definitions:
                resolve(definition)
            total_steps = int(normalized.get("max_steps", 20))
            maximum_steps = 10_000 if execution_mode == "simulation" else 10_000_000
            if total_steps < 1 or total_steps > maximum_steps:
                raise TrainingValidationError(
                    "invalid_max_steps",
                    f"max_steps must be between 1 and {maximum_steps}.",
                )
            return {"parameters": normalized, "sensitive_parameters": sensitive, "stage_input_source": source, "total_steps": total_steps}

        for index, stage in enumerate(data["stages"], start=1):
            normalized_stages.append(normalize_stage(stage, index))

        indexes = [item["index"] for item in selected]
        template = model["launch_template"]
        launcher_kind = template.get("launcher_kind") or ("torchrun" if str(template["executable"]).rsplit("/", 1)[-1] == "torchrun" else "direct")
        uses_torchrun = launcher_kind == "torchrun"
        nproc_per_node = len(indexes) if uses_torchrun else 1
        stage_names = ("第一阶段", "第二阶段", "第三阶段", "第四阶段", "第五阶段", "第六阶段", "第七阶段", "第八阶段", "第九阶段", "第十阶段")

        def build(run_ref: str, port: int | None, version_meta: dict[str, Any]) -> dict[str, Any]:
            if uses_torchrun and port is None:
                raise TrainingValidationError("master_port_required", "Torchrun requires an allocated master port.")
            version_segment = "preview" if run_ref == "preview" else version_meta["version_label"]
            version_output_root = f"{template['output_root'].rstrip('/')}/{model['family_ref']}/{version_segment}"
            dataset_manifest_path = (
                f"{version_output_root}/dataset-manifest.json"
                if dataset_splits is not None
                else None
            )
            dataset_manifest = (
                {
                    "contract": "datapilot_dataset_manifest_v1",
                    "snapshot_ref": version_meta.get("snapshot_ref") or "preview",
                    "run_ref": None if run_ref == "preview" else run_ref,
                    "family_ref": model["family_ref"],
                    "splits": dataset_splits,
                }
                if dataset_splits is not None
                else None
            )
            built_stages: list[dict[str, Any]] = []
            previous_output: str | None = None
            for stage_number, source_stage in enumerate(normalized_stages, start=1):
                output = f"{template['output_root'].rstrip('/')}/{model['family_ref']}/{version_segment}/stage-{stage_number:02d}"
                parameters = dict(source_stage["parameters"])
                if source_stage["stage_input_source"] == "previous_stage_output" and stage_input is not None:
                    if previous_output is None:
                        raise TrainingValidationError(
                            "invalid_stage_input_source",
                            "A previous stage output is required for automatic stage input.",
                        )
                    try:
                        parameters[stage_input.name] = normalize_parameter_value(
                            stage_input, previous_output
                        )
                    except ValueError as exc:
                        raise TrainingValidationError(
                            "invalid_parameter",
                            f"Stage {stage_number}: {exc}",
                        ) from exc
                if uses_torchrun:
                    argv = [template["executable"], "--nnodes=1", f"--nproc_per_node={nproc_per_node}", "--master_addr=127.0.0.1", f"--master_port={port}", "--node_rank=0", template["entrypoint"], *template.get("fixed_argv", [])]
                else:
                    argv = [template["executable"], template["entrypoint"], *template.get("fixed_argv", [])]
                safe_argv = list(argv)
                if dataset_manifest_path is not None:
                    argv.extend(["--dataset_manifest", dataset_manifest_path])
                    safe_argv.extend(["--dataset_manifest", dataset_manifest_path])
                for definition in definitions:
                    if definition.name not in parameters:
                        continue
                    value = parameters[definition.name]
                    if definition.argument_style == "flag_when_true":
                        if value:
                            argv.append(definition.cli_flag); safe_argv.append(definition.cli_flag)
                        continue
                    rendered = "True" if definition.argument_style == "explicit_boolean" and value else "False" if definition.argument_style == "explicit_boolean" else str(value)
                    argv.extend([definition.cli_flag, rendered])
                    safe_argv.extend([definition.cli_flag, "********" if definition.sensitive else rendered])
                output_flag = template.get("output_flag", "--output_dir")
                argv.extend([output_flag, output]); safe_argv.extend([output_flag, output])
                distributed = {"master_addr": "127.0.0.1" if uses_torchrun else None, "master_port": port if uses_torchrun else None, "node_rank": 0 if uses_torchrun else None}
                public_parameters = {key: "********" if key in source_stage["sensitive_parameters"] else value for key, value in parameters.items()}
                public = {"contract_version": 2, "execution_mode": execution_mode, "family_ref": model["family_ref"], "family_name": model["family_name"], "version_label": version_meta.get("version_label"), "stage_number": stage_number, "stage_name": stage_names[stage_number - 1], "server_ref": data["server_ref"], "gpu_uuids": data["gpu_uuids"], "launcher_kind": launcher_kind, "nnodes": 1, **distributed, "nproc_per_node": nproc_per_node, "environment": {"CUDA_VISIBLE_DEVICES": ",".join(map(str, indexes))}, "runtime_environment": template.get("runtime_environment", {"kind": "system"}), "monitoring": template.get("monitoring", {"source": "stdout", "format": "plain"}), "parameters": public_parameters, "entrypoint": template["entrypoint"], "argv": safe_argv, "output_preview": output, "dataset_manifest": dataset_manifest_path}
                preview_check = "simulation_only" if execution_mode == "simulation" else "real_preview_only"
                ready_code = "simulation_ready" if execution_mode == "simulation" else "real_preview_ready"
                ready_message = "Simulation inputs and GPU availability are valid." if execution_mode == "simulation" else "真实节点、GPU 和参数已通过预览校验；未创建任务、租约或进程。"
                private = {**public, "version": 2, "mode": execution_mode, "model_ref": model["model_ref"], "internal_model_revision": model["internal_revision"], "revision_ref": model["revision_ref"], "gpu_indexes": indexes, "parameters": parameters, "sensitive_parameters": source_stage["sensitive_parameters"], "working_directory": template["working_directory"], "entrypoint": template["entrypoint"], "output_root": template["output_root"], "output_directory": output, "argv": argv, "preflight": {"ok": True, "checks": [preview_check, "gpu_available", "parameters_valid"]}, "safe_command_preview": shlex.join(safe_argv), "public_spec": public}
                built_stages.append({"stage_number": stage_number, "stage_name": stage_names[stage_number - 1], "stage_input_source": source_stage["stage_input_source"], "parameters": parameters, "private_spec": private, "run_spec": public, "command_preview": private["safe_command_preview"], "output_directory": output, "total_steps": source_stage["total_steps"], "preflight": [{"ok": True, "code": ready_code, "message": ready_message}]})
                previous_output = output
            return {"stages": built_stages, "total_steps": sum(stage["total_steps"] for stage in built_stages), "dataset_manifest": dataset_manifest, "dataset_manifest_path": dataset_manifest_path}

        prepared = {"family_ref": model["family_ref"], "model_ref": model["model_ref"], "revision_ref": model["revision_ref"], "server_ref": data["server_ref"], "gpu_uuids": data["gpu_uuids"], "stages": normalized_stages, "total_steps": sum(stage["total_steps"] for stage in normalized_stages), "requires_master_port": uses_torchrun, "data_access_mode": data_access_mode, "dataset_splits": dataset_splits, "version_description": str(data.get("version_description") or "").strip(), "execution_mode": execution_mode}
        return prepared, build

    def preview_run(self, payload: Any, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        prepared, builder = self._prepare(payload, allow_real_preview=True)
        port = (
            self.store.find_available_port(self._data(payload)["server_ref"])
            if prepared["requires_master_port"]
            else None
        )
        built = builder(
            "preview",
            port,
            {
                "version_ref": None,
                "version_number": None,
                "version_date": None,
                "version_label": "preview",
            },
        )
        execution_mode = self._data(payload).get("execution_mode", "simulation")
        ready_code = (
            "simulation_ready"
            if execution_mode == "simulation"
            else "real_preview_ready"
        )
        ready_message = (
            "All simulation stages and GPU selections are valid."
            if execution_mode == "simulation"
            else "真实训练预览已生成；未创建任务、GPU 租约、模型版本或进程。"
        )
        return {
            "stages": built["stages"],
            "dataset_manifest_preview": built.get("dataset_manifest"),
            "dataset_manifest_path": built.get("dataset_manifest_path"),
            "preflight": [
                {"ok": True, "code": ready_code, "message": ready_message}
            ],
        }

    def create_run(self, payload: Any, idempotency_key: str, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        public_request = self._data(payload)
        existing = self.store.find_create_run_by_idempotency(
            idempotency_key, public_request
        )
        if existing is not None:
            return self._project_run(existing)
        if not str(public_request.get("version_description") or "").strip():
            raise TrainingValidationError(
                "version_description_required",
                "A description of this training version is required before starting.",
            )
        # Store idempotency is checked before its atomic GPU lease check.  We
        # therefore ignore only our own platform leases here, allowing a
        # byte-equivalent retry to return the original run while still
        # rejecting external occupancy and conflicting new submissions.
        prepared, builder = self._prepare(payload, ignore_platform_leases=True)
        return self._project_run(
            self.store.create_run(
                data=prepared,
                run_spec_builder=builder,
                idempotency_key=idempotency_key,
                actor=principal.subject,
                idempotency_payload=public_request,
            )
        )

    def _project_run(self, run: dict[str, Any]) -> dict[str, Any]:
        metric_page = self.store.list_recent_metrics(run["run_ref"], 2000)
        metrics = metric_page["items"]
        current_stage_number = run.get("current_stage_number")
        current_stage = next(
            (
                stage
                for stage in run.get("stages", [])
                if stage.get("stage_number") == current_stage_number
            ),
            run.get("stages", [])[-1] if run.get("stages") else None,
        )
        current_stage_ref = current_stage.get("stage_ref") if current_stage else None
        current_stage_metrics = [
            metric
            for metric in metrics
            if current_stage_ref is None or metric.get("stage_ref") == current_stage_ref
        ]

        # GPU-only samples intentionally omit trainer fields.  Do not let a
        # later resource sample erase the most recent loss/epoch summary.
        trainer_fields = ("step", "epoch", "loss", "learning_rate", "grad_norm")
        latest_trainer_metric = next(
            (
                metric
                for metric in reversed(current_stage_metrics)
                if any(metric.get(field) is not None for field in trainer_fields)
            ),
            None,
        )
        latest = dict(latest_trainer_metric) if latest_trainer_metric else None
        if latest is not None:
            for field in trainer_fields + ("total_steps", "elapsed_seconds"):
                recent_value = next(
                    (
                        metric.get(field)
                        for metric in reversed(current_stage_metrics)
                        if metric.get(field) is not None
                    ),
                    None,
                )
                latest[field] = recent_value

        projected_stages: list[dict[str, Any]] = []
        for stage in run.get("stages", []):
            stage_metrics = [
                metric
                for metric in metrics
                if metric.get("stage_ref") == stage.get("stage_ref")
            ]
            epoch_metric = next(
                (
                    metric
                    for metric in reversed(stage_metrics)
                    if metric.get("epoch") is not None
                ),
                None,
            )
            raw_total_epochs = stage.get("parameters", {}).get("num_train_epochs")
            try:
                total_epochs = max(0.0, float(raw_total_epochs))
            except (TypeError, ValueError):
                total_epochs = 0.0
            projected_stages.append(
                {
                    **stage,
                    "current_epoch": float(epoch_metric["epoch"]) if epoch_metric else 0.0,
                    "total_epochs": total_epochs,
                }
            )

        projected_current_stage = next(
            (
                stage
                for stage in projected_stages
                if stage.get("stage_number") == current_stage_number
            ),
            projected_stages[-1] if projected_stages else None,
        )
        return {
            **run,
            "stages": projected_stages,
            "progress_percent": round(run["progress"] * 100, 2),
            "current_epoch": (
                projected_current_stage.get("current_epoch", 0.0)
                if projected_current_stage
                else 0.0
            ),
            "total_epochs": (
                projected_current_stage.get("total_epochs", 0.0)
                if projected_current_stage
                else 0.0
            ),
            "latest_metric": latest,
            "failure_code": (
                run.get("failure", {}).get("code") if run.get("failure") else None
            ),
            "failure_message": (
                run.get("failure", {}).get("message") if run.get("failure") else None
            ),
            "audit_events": self.store.list_audit_events(run["run_ref"]),
        }

    def list_runs(self, *, status: str | None, after: str | None, limit: int) -> dict[str, Any]:
        page = self.store.list_runs(status=status, after=after, limit=limit); page["items"] = [self._project_run(item) for item in page["items"]]; return page
    def get_run(self, run_ref: str) -> dict[str, Any]: return self._project_run(self.store.get_run(run_ref))
    def stop_run(self, run_ref: str, expected_revision: int, idempotency_key: str, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_STOP_RUNS); return self._project_run(self.store.stop_run(run_ref, expected_revision, idempotency_key, principal.subject))
    def list_logs(self, run_ref: str, *, after_seq: int, limit: int, stage_ref: str | None = None) -> dict[str, Any]: return self.store.list_logs(run_ref, after_seq, limit, stage_ref=stage_ref)
    def list_metrics(self, run_ref: str, *, after_seq: int, limit: int, stage_ref: str | None = None) -> dict[str, Any]: return self.store.list_metrics(run_ref, after_seq, limit, stage_ref=stage_ref)
    def list_events(self, *, after_seq: int, limit: int) -> dict[str, Any]: return self.store.list_events(after_seq, limit)

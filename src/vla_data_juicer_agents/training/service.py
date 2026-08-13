from __future__ import annotations

import shlex
from typing import Any, Protocol

from pydantic import BaseModel

from .auth import (
    TRAINING_CREATE_RUNS,
    TRAINING_MANAGE_MODELS,
    TRAINING_MANAGE_NODES,
    TRAINING_STOP_RUNS,
)
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
    def __init__(self, store: TrainingStore, provider: FakeResourceProvider | TrainingResourceProvider, *, simulation_enabled: bool = True, node_deployment_manager: TrainingNodeDeploymentManager | None = None) -> None:
        self.store, self.provider = store, provider
        self.simulation_enabled = simulation_enabled
        self.node_deployment_manager = node_deployment_manager

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
        return {**principal.public_projection(), "simulation_enabled": self.simulation_enabled, "real_execution_enabled": False, "real_execution_disabled_reason": "Real training is intentionally disabled until server paths and credentials are verified.", "node_deployment_enabled": deployment_enabled, "node_deployment_disabled_reason": None if deployment_enabled else "Training Worker deployment requires a configured HTTPS center URL."}

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
        try:
            token_result = self.store.create_enrollment_token(
                node_ref,
                int(deploying["state_revision"]),
                600,
                principal.subject,
            )
            deployment = self.node_deployment_manager.deploy_worker(
                node=token_result["node"],
                confirmed_host_key=confirmed,
                ssh_password=ssh_password,
                sudo_password_mode=payload.sudo_password_mode,
                sudo_password=sudo_password,
                enrollment_token=token_result["enrollment_token"],
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
        node = self.store.finish_node_deployment(
            node_ref,
            succeeded=True,
            message=deployment.get("message", "Training Worker deployed."),
            worker_version=worker_version,
            actor=principal.subject,
        )
        return {
            "node": node,
            "deployment": {
                "status": "succeeded",
                "worker_version": worker_version,
                "message": deployment.get("message", "Training Worker deployed."),
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
        node = self.store.get_node(node_ref)
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
                node=removing,
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
        return self.store.record_node_heartbeat(
            node_ref, worker_token, self._data(payload)
        )

    def get_node_resources(self, node_ref: str) -> dict[str, Any]:
        return self.store.get_node_resources(node_ref)

    def list_models(self, *, include_private: bool = False) -> list[dict[str, Any]]:
        models = self.store.list_models()
        if include_private:
            models = [self.store.get_model(item["model_ref"], include_private=True) for item in models]
        return [self._project_model(item) for item in models]

    def get_model(self, model_ref: str, *, include_private: bool = False) -> dict[str, Any]:
        return self._project_model(self.store.get_model(model_ref, include_private=include_private))

    def create_model(self, payload: Any, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_MODELS)
        data = self._adapt_model(self._data(payload))
        self._require_registered_server(data["launch_template"]["server_ref"])
        return self._project_model(self.store.create_model(data, principal.subject))

    def create_model_version(
        self, family_ref: str, payload: Any, principal: Any
    ) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_MODELS)
        data = self._adapt_model(self._data(payload))
        self._require_registered_server(data["launch_template"]["server_ref"])
        return self._project_model(
            self.store.create_model_version(family_ref, data, principal.subject)
        )

    def update_model(self, model_ref: str, payload: Any, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_MANAGE_MODELS)
        data = self._adapt_model(self._data(payload)); expected = int(data.pop("expected_revision"))
        self._require_registered_server(data["launch_template"]["server_ref"])
        return self._project_model(self.store.update_model(model_ref, expected, data, principal.subject))

    def _require_registered_server(self, server_ref: str) -> None:
        if not any(
            server["server_ref"] == server_ref
            for server in self.provider.list_servers()
        ):
            raise TrainingValidationError(
                "server_not_found",
                "请从已登记的训练节点或模拟服务器中选择模型运行位置。",
            )

    @staticmethod
    def _project_model(model: dict[str, Any]) -> dict[str, Any]:
        result = dict(model)
        if "parameter_definitions" in model:
            definitions = []
            for item in model["parameter_definitions"]:
                visible_when = item.get("visible_when")
                choices = [choice if isinstance(choice, dict) else {"value": choice, "label": choice} for choice in (item.get("choices") or [])]
                definitions.append({"key": item["name"], "label": item.get("label", item["name"]), "type": "number" if item["kind"] == "float" else item["kind"], "semantic_role": item.get("semantic_role", "hyperparameter"), "default": item["default"], "description": item.get("description"), "minimum": item.get("minimum"), "maximum": item.get("maximum"), "choices": choices, "string_min_length": item.get("string_min_length"), "string_max_length": item.get("string_max_length"), "visible_when": {"parameter_key": visible_when["parameter_name"], "equals": visible_when["equals"]} if visible_when else None, "display_group": item.get("display_group"), "display_group_label": item.get("display_group_label"), "display_group_order": item.get("display_group_order"), "editable": True, "sensitive": item.get("sensitive", False), "cli_flag": item.get("cli_flag"), "argument_style": item.get("argument_style") or ("flag_when_true" if item["kind"] == "boolean" else "value")})
            configuration: dict[str, Any] = {"parameter_definitions": definitions}
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
        }
        return adapted

    def _prepare(self, payload: Any, *, ignore_platform_leases: bool = False) -> tuple[dict[str, Any], Any]:
        if not self.simulation_enabled: raise TrainingForbiddenError("simulation_disabled", "Training simulation is disabled.")
        data = self._data(payload)
        mode = data.get("execution_mode", data.get("mode"))
        if mode != "simulation": raise TrainingValidationError("unsupported_execution_mode", "Only simulation mode is supported.")
        server = next(
            (
                item
                for item in self.provider.list_servers()
                if item["server_ref"] == data["server_ref"]
            ),
            None,
        )
        if server is not None and server.get("kind") != "simulation":
            raise TrainingValidationError(
                "real_execution_disabled",
                "真实训练尚未启用；真实训练节点当前只用于登记模型和查看资源。",
            )
        selected = self.provider.require_available(
            data["server_ref"],
            data["gpu_uuids"],
            ignore_platform_leases=ignore_platform_leases,
        )
        model = self.store.get_model_record(data["model_ref"])
        if model["status"] != "draft": raise TrainingValidationError("model_unavailable", "The selected model is not an editable simulation draft.")
        if model["launch_template"]["server_ref"] != data["server_ref"]: raise TrainingValidationError("server_mismatch", "The model template belongs to another server.")
        normalized: dict[str, Any] = {}
        sensitive: list[str] = []
        supplied = data.get("parameters", {})
        definitions = [ParameterDefinition.model_validate(item) for item in model["parameter_definitions"]]
        by_name = {item.name: item for item in definitions}
        known = set(by_name)
        if set(supplied) - known: raise TrainingValidationError("unknown_parameter", "The request contains an unknown training parameter.")
        active: dict[str, bool] = {}
        resolving: set[str] = set()

        def resolve_parameter(definition: ParameterDefinition) -> bool:
            cached = active.get(definition.name)
            if cached is not None:
                return cached
            if definition.name in resolving:
                raise TrainingValidationError("invalid_parameter_dependency", "The model contains a cyclic parameter dependency.")
            resolving.add(definition.name)
            condition = definition.visible_when
            if condition is not None:
                controller = by_name.get(condition.parameter_name)
                if controller is None:
                    raise TrainingValidationError("invalid_parameter_dependency", "The model contains an unknown dependency controller.")
                if not resolve_parameter(controller) or normalized.get(controller.name) != condition.equals:
                    resolving.remove(definition.name)
                    active[definition.name] = False
                    return False
            try:
                normalized[definition.name] = normalize_parameter_value(
                    definition,
                    supplied.get(definition.name, definition.default),
                )
            except ValueError as exc:
                raise TrainingValidationError("invalid_parameter", str(exc)) from exc
            resolving.remove(definition.name)
            active[definition.name] = True
            if definition.sensitive:
                sensitive.append(definition.name)
            return True

        for definition in definitions:
            resolve_parameter(definition)
        indexes = [item["index"] for item in selected]
        template = model["launch_template"]
        launcher_kind = template.get("launcher_kind") or (
            "torchrun"
            if str(template["executable"]).rsplit("/", 1)[-1] == "torchrun"
            else "direct"
        )
        uses_torchrun = launcher_kind == "torchrun"
        nproc_per_node = len(indexes) if uses_torchrun else 1

        def build(run_ref: str, port: int | None) -> dict[str, Any]:
            output = f"{template['output_root'].rstrip('/')}/{run_ref}"
            if uses_torchrun:
                if port is None:
                    raise TrainingValidationError(
                        "master_port_required",
                        "Torchrun requires an allocated master port.",
                    )
                argv = [
                    template["executable"],
                    "--nnodes=1",
                    f"--nproc_per_node={nproc_per_node}",
                    "--master_addr=127.0.0.1",
                    f"--master_port={port}",
                    "--node_rank=0",
                    template["entrypoint"],
                    *template.get("fixed_argv", []),
                ]
            else:
                argv = [
                    template["executable"],
                    template["entrypoint"],
                    *template.get("fixed_argv", []),
                ]
            safe_argv = list(argv)
            for definition in definitions:
                if definition.name not in normalized:
                    continue
                value = normalized[definition.name]
                if definition.argument_style == "flag_when_true":
                    if value: argv.append(definition.cli_flag); safe_argv.append(definition.cli_flag)
                    continue
                if definition.argument_style == "explicit_boolean":
                    rendered = "True" if value else "False"
                else:
                    rendered = str(value)
                argv.extend([definition.cli_flag, rendered])
                safe_argv.extend([definition.cli_flag, "********" if definition.sensitive else rendered])
            output_flag = template.get("output_flag", "--output_dir")
            argv.extend([output_flag, output])
            safe_argv.extend([output_flag, output])
            distributed = {
                "master_addr": "127.0.0.1" if uses_torchrun else None,
                "master_port": port if uses_torchrun else None,
                "node_rank": 0 if uses_torchrun else None,
            }
            private = {"version": 1, "mode": "simulation", "model_ref": model["model_ref"], "family_ref": model["family_ref"], "family_name": model["family_name"], "model_version_number": model["version_number"], "model_display_name": f"{model['family_name']} v{model['version_number']}", "internal_model_revision": model["internal_revision"], "revision_ref": model["revision_ref"], "server_ref": data["server_ref"], "gpu_uuids": data["gpu_uuids"], "gpu_indexes": indexes, "launcher_kind": launcher_kind, "nnodes": 1, "nproc_per_node": nproc_per_node, **distributed, "environment": {"CUDA_VISIBLE_DEVICES": ",".join(map(str,indexes))}, "runtime_environment": template.get("runtime_environment", {"kind": "system"}), "monitoring": template.get("monitoring", {"source": "stdout", "format": "plain"}), "parameters": normalized, "sensitive_parameters": sensitive, "working_directory": template["working_directory"], "entrypoint": template["entrypoint"], "output_directory": output, "argv": argv, "preflight": {"ok": True, "checks": ["simulation_only", "gpu_available", "parameters_valid"]}, "safe_command_preview": shlex.join(safe_argv)}
            safe = {"contract_version": 1, "execution_mode": "simulation", "server_ref": data["server_ref"], "gpu_uuids": data["gpu_uuids"], "launcher_kind": launcher_kind, "nnodes": 1, **distributed, "nproc_per_node": nproc_per_node, "environment": {"CUDA_VISIBLE_DEVICES": ",".join(map(str, indexes))}, "runtime_environment": template.get("runtime_environment", {"kind": "system"}), "monitoring": template.get("monitoring", {"source": "stdout", "format": "plain"}), "parameters": normalized, "argv": safe_argv, "output_preview": output}
            safe["parameters"] = {key: "********" if key in sensitive else value for key,value in normalized.items()}
            private["public_spec"] = safe
            return {"private_spec": private, "run_spec": safe, "command_preview": private["safe_command_preview"]}
        total_steps = int(normalized.get("max_steps", 20))
        if total_steps < 1 or total_steps > 10_000:
            raise TrainingValidationError("invalid_max_steps", "Simulation max_steps must be between 1 and 10000.")
        prepared = {"model_ref": model["model_ref"], "revision_ref": model["revision_ref"], "server_ref": data["server_ref"], "gpu_uuids": data["gpu_uuids"], "parameters": normalized, "total_steps": total_steps, "requires_master_port": uses_torchrun}
        return prepared, build

    def preview_run(self, payload: Any, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        prepared, builder = self._prepare(payload)
        port = (
            self.store.find_available_port(self._data(payload)["server_ref"])
            if prepared["requires_master_port"]
            else None
        )
        built = builder("preview", port)
        return {"run_spec": built["run_spec"], "command_preview": built["command_preview"], "preflight": [{"ok": True, "code": "simulation_ready", "message": "Simulation inputs and GPU availability are valid."}]}

    def create_run(self, payload: Any, idempotency_key: str, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_CREATE_RUNS)
        # Store idempotency is checked before its atomic GPU lease check.  We
        # therefore ignore only our own platform leases here, allowing a
        # byte-equivalent retry to return the original run while still
        # rejecting external occupancy and conflicting new submissions.
        prepared, builder = self._prepare(payload, ignore_platform_leases=True)
        return self._project_run(self.store.create_run(data=prepared, run_spec_builder=builder, idempotency_key=idempotency_key, actor=principal.subject))

    def _project_run(self, run: dict[str, Any]) -> dict[str, Any]:
        metric_page = self.store.list_metrics(run["run_ref"], 0, 2000)
        latest = metric_page["items"][-1] if metric_page["items"] else None
        current_epoch = latest["epoch"] if latest else 0
        return {**run, "progress_percent": round(run["progress"] * 100, 2), "current_epoch": current_epoch, "total_epochs": 3, "latest_metric": latest, "failure_code": run.get("failure", {}).get("code") if run.get("failure") else None, "failure_message": run.get("failure", {}).get("message") if run.get("failure") else None, "audit_events": self.store.list_audit_events(run["run_ref"])}

    def list_runs(self, *, status: str | None, after: str | None, limit: int) -> dict[str, Any]:
        page = self.store.list_runs(status=status, after=after, limit=limit); page["items"] = [self._project_run(item) for item in page["items"]]; return page
    def get_run(self, run_ref: str) -> dict[str, Any]: return self._project_run(self.store.get_run(run_ref))
    def stop_run(self, run_ref: str, expected_revision: int, idempotency_key: str, principal: Any) -> dict[str, Any]:
        self._require(principal, TRAINING_STOP_RUNS); return self._project_run(self.store.stop_run(run_ref, expected_revision, idempotency_key, principal.subject))
    def list_logs(self, run_ref: str, *, after_seq: int, limit: int) -> dict[str, Any]: return self.store.list_logs(run_ref, after_seq, limit)
    def list_metrics(self, run_ref: str, *, after_seq: int, limit: int) -> dict[str, Any]: return self.store.list_metrics(run_ref, after_seq, limit)
    def list_events(self, *, after_seq: int, limit: int) -> dict[str, Any]: return self.store.list_events(after_seq, limit)

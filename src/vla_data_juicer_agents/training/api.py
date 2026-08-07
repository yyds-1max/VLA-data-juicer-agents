from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import math
import re
from typing import Annotated, Any, Callable, Literal, Protocol

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
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

    def list_models(
        self, *, include_private: bool = False
    ) -> list[dict[str, Any]]: ...

    def get_model(
        self, model_ref: str, *, include_private: bool = False
    ) -> dict[str, Any]: ...

    def create_model(
        self, payload: Any, principal: TrainingPrincipal
    ) -> dict[str, Any]: ...

    def update_model(
        self, model_ref: str, payload: Any, principal: TrainingPrincipal
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
        self, *, status: str | None, after: str | None, limit: int
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
        self, run_ref: str, *, after_seq: int, limit: int
    ) -> dict[str, Any]: ...

    def list_metrics(
        self, run_ref: str, *, after_seq: int, limit: int
    ) -> dict[str, Any]: ...

    def list_events(self, *, after_seq: int, limit: int) -> dict[str, Any]: ...


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


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

    @model_validator(mode="after")
    def validate_definition(self) -> ParameterDefinition:
        default = self.default
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


class LaunchTemplate(StrictRequest):
    domain: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    server_ref: str = Field(min_length=1, max_length=200)
    working_directory: str = Field(min_length=1, max_length=1000)
    executable: str = Field(min_length=1, max_length=500)
    entrypoint: str = Field(min_length=1, max_length=1000)
    fixed_argv: list[str] = Field(default_factory=list, max_length=200)
    output_root: str = Field(min_length=1, max_length=1000)
    output_flag: str = Field(
        default="--output_dir",
        pattern=r"^--[A-Za-z0-9][A-Za-z0-9_-]{0,99}$",
    )

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


class CreateModelRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    launch_template: LaunchTemplate
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
    def reserve_platform_output_flag(self) -> CreateModelRequest:
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


class UpdateModelRequest(CreateModelRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    name: str | None = Field(default=None, min_length=1, max_length=200)


class RunRequest(StrictRequest):
    model_ref: str = Field(min_length=1, max_length=200)
    model_revision: Annotated[int, Field(strict=True, ge=1)] | None = None
    server_ref: str = Field(min_length=1, max_length=200)
    gpu_uuids: list[str] = Field(min_length=1, max_length=8)
    parameters: dict[str, JsonScalar] = Field(default_factory=dict, max_length=200)
    execution_mode: Literal["simulation"]

    @field_validator("model_ref", "server_ref")
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

    @field_validator("parameters")
    @classmethod
    def safe_parameter_keys(
        cls, value: dict[str, JsonScalar]
    ) -> dict[str, JsonScalar]:
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", key) for key in value):
            raise ValueError("parameters contain an invalid key")
        return value


class StopRunRequest(StrictRequest):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


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

    @router.get("/models/{model_ref}")
    def get_model(model_ref: str) -> dict[str, Any]:
        principal = get_principal()
        model = _normalize_model(
            _translate(
                service.get_model,
                model_ref,
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

    @router.put("/models/{model_ref}")
    def update_model(
        model_ref: str,
        request: UpdateModelRequest,
    ) -> dict[str, Any]:
        model = _normalize_model(
            _translate(
                service.update_model,
                model_ref,
                request,
                get_principal(),
            )
        )
        return {"model": model}

    @router.post("/runs/preview")
    def preview_run(request: RunRequest) -> dict[str, Any]:
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
        after: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        page = _translate(
            service.list_runs,
            status=status,
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
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> dict[str, Any]:
        page = _translate(
            service.list_logs,
            run_ref,
            after_seq=after_seq,
            limit=limit,
        )
        return _page_projection(page, "logs")

    @router.get("/runs/{run_ref}/metrics")
    def run_metrics(
        run_ref: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> dict[str, Any]:
        page = _translate(
            service.list_metrics,
            run_ref,
            after_seq=after_seq,
            limit=limit,
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
    }


def _server_projection(server: dict[str, Any]) -> dict[str, Any]:
    kind = server.get("kind")
    if not isinstance(kind, str):
        kind = "simulation" if server.get("provider") == "fake" else "unknown"
    return {
        "server_ref": server["server_ref"],
        "name": server.get("name", server.get("label", server["server_ref"])),
        "kind": kind,
        "gpu_count": int(server.get("gpu_count", 0)),
    }


def _resources_projection(
    resources: dict[str, Any], server: dict[str, Any]
) -> dict[str, Any]:
    sampled_at = resources.get("sampled_at")
    if isinstance(sampled_at, (int, float)):
        sampled_at = datetime.fromtimestamp(sampled_at, UTC).isoformat()
    elif not isinstance(sampled_at, str):
        sampled_at = datetime.now(UTC).isoformat()
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
    return {
        "server": _server_projection(server),
        "sampled_at": sampled_at,
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
    raw_revision = model.get("revision")
    revision_number = model.get("latest_revision")
    if revision_number is None:
        if isinstance(raw_revision, dict):
            revision_number = raw_revision.get("revision")
        else:
            revision_number = raw_revision
    result = {
        "model_ref": model["model_ref"],
        "name": model["name"],
        "description": model.get("description") or None,
        "status": model.get("status", "draft"),
        "latest_revision": int(revision_number or 1),
        "created_at": model.get("created_at"),
        "updated_at": model.get("updated_at"),
    }
    revision_source = raw_revision if isinstance(raw_revision, dict) else model
    definitions = revision_source.get("parameter_definitions")
    if definitions is not None:
        launch_template = revision_source.get("launch_template")
        revision = {
            "revision": result["latest_revision"],
            "created_at": revision_source.get(
                "created_at", model.get("updated_at", model.get("created_at"))
            ),
            "parameter_definitions": [
                _normalize_parameter_definition(definition)
                for definition in definitions
            ],
            "fixed_argv": revision_source.get(
                "fixed_argv",
                launch_template.get("fixed_argv", [])
                if isinstance(launch_template, dict)
                else [],
            ),
            "output_preview": revision_source.get("output_preview"),
        }
        if isinstance(launch_template, dict):
            revision["launch_template"] = launch_template
        result["revision"] = revision
    return result


def _run_spec_projection(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": int(spec.get("contract_version", spec.get("version", 1))),
        "execution_mode": spec.get("execution_mode", spec.get("mode", "simulation")),
        "server_ref": spec["server_ref"],
        "gpu_uuids": spec.get("gpu_uuids", []),
        "nnodes": int(spec.get("nnodes", 1)),
        "master_addr": spec.get("master_addr", "127.0.0.1"),
        "master_port": int(spec.get("master_port", 0)),
        "node_rank": int(spec.get("node_rank", 0)),
        "nproc_per_node": int(spec.get("nproc_per_node", 0)),
        "environment": spec.get("environment", {}),
        "parameters": spec.get("parameters", {}),
        "argv": spec.get("argv", []),
        "output_preview": spec.get("output_preview"),
    }


def _preview_projection(preview: dict[str, Any]) -> dict[str, Any]:
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
    result = {
        "run_ref": run["run_ref"],
        "model_ref": run.get("model_ref", "unknown-model"),
        "model_name": run.get("model_name", run.get("model_ref", "Training model")),
        "model_revision": int(run.get("model_revision", 1)),
        "status": run["status"],
        "state_revision": int(run.get("state_revision", 1)),
        "server_ref": run.get("server_ref", ""),
        "gpu_uuids": run.get("gpu_uuids", []),
        "progress_percent": round(float(progress), 4),
        "current_step": int(run.get("current_step", 0)),
        "total_steps": int(run.get("total_steps", 0)),
        "current_epoch": float(run.get("current_epoch", 0)),
        "total_epochs": float(run.get("total_epochs", 3)),
        "latest_metric": run.get("latest_metric"),
        "failure_code": run.get("failure_code", failure.get("code")),
        "failure_message": run.get("failure_message", failure.get("message")),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "parameters": run.get("parameters", {}),
        "audit_events": run.get("audit_events", []),
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
    if (
        type(event_id) is not int
        or event_id < 1
        or event_type
        not in {"run.updated", "run.log.appended", "run.metric.appended"}
        or not isinstance(run_ref, str)
    ):
        raise TypeError("training service returned an invalid event")
    event: dict[str, Any] = {
        "event_id": event_id,
        "type": event_type,
        "run_ref": run_ref,
    }
    item_seq = raw_event.get(
        "item_seq",
        raw_event.get("resource_seq", payload.get("seq")),
    )
    if type(item_seq) is int and item_seq >= 0:
        event["seq"] = item_seq
    return event


def _model_projection(
    model: dict[str, Any], principal: TrainingPrincipal
) -> dict[str, Any]:
    if principal.can("training:manage_models"):
        return model
    projected = dict(model)
    revision = projected.get("revision")
    if isinstance(revision, dict):
        safe_revision = dict(revision)
        definitions = safe_revision.get("parameter_definitions")
        if isinstance(definitions, list):
            safe_revision["parameter_definitions"] = [
                {
                    **definition,
                    "default": "********",
                }
                if isinstance(definition, dict) and definition.get("sensitive")
                else definition
                for definition in definitions
            ]
        for field in (
            "fixed_argv",
            "launch_template",
            "output_preview",
            "working_directory",
            "entrypoint",
            "output_template",
        ):
            safe_revision.pop(field, None)
        projected["revision"] = safe_revision
    return projected


def _run_projection(
    run: dict[str, Any], principal: TrainingPrincipal
) -> dict[str, Any]:
    if principal.can("training:create_runs"):
        return run
    projected = dict(run)
    projected.pop("run_spec", None)
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

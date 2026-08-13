from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any, Mapping


def verify_model_configuration(payload: Mapping[str, object]) -> dict[str, object]:
    """Perform deterministic read-only checks for one registered model version."""
    working_directory = _required_text(payload, "working_directory", 1000)
    executable = _required_text(payload, "executable", 500)
    entrypoint = _required_text(payload, "entrypoint", 1000)
    output_root = _required_text(payload, "output_root", 1000)
    runtime = payload.get("runtime_environment")
    if not isinstance(runtime, Mapping):
        raise ValueError("runtime_environment must be an object")

    checks: list[dict[str, str]] = []
    workdir = Path(working_directory)
    workdir_ok = (
        workdir.is_absolute()
        and workdir.is_dir()
        and os.access(workdir, os.R_OK | os.X_OK)
    )
    checks.append(
        _check(
            "working_directory",
            "工程目录",
            workdir_ok,
            "工程目录存在且 Worker 可以读取。"
            if workdir_ok
            else "工程目录不存在，或 Worker 没有读取权限。",
        )
    )

    entry_path = Path(entrypoint)
    if not entry_path.is_absolute():
        entry_path = workdir / entry_path
    try:
        resolved_workdir = workdir.resolve(strict=False)
        resolved_entry = entry_path.resolve(strict=False)
        entry_in_workdir = resolved_entry.is_relative_to(resolved_workdir)
    except (OSError, RuntimeError):
        entry_in_workdir = False
        resolved_entry = entry_path
    entry_ok = (
        workdir_ok
        and entry_in_workdir
        and resolved_entry.is_file()
        and os.access(resolved_entry, os.R_OK)
    )
    checks.append(
        _check(
            "entrypoint",
            "训练入口",
            entry_ok,
            "训练入口存在且 Worker 可以读取。"
            if entry_ok
            else "训练入口不存在、不可读，或不在工程目录内。",
        )
    )

    executable_path: str | None
    executable_candidate = Path(executable)
    if executable_candidate.is_absolute() or "/" in executable:
        if not executable_candidate.is_absolute():
            executable_candidate = workdir / executable_candidate
        executable_path = str(executable_candidate) if executable_candidate.is_file() and os.access(executable_candidate, os.X_OK) else None
    else:
        executable_path = shutil.which(executable)
    executable_ok = executable_path is not None
    checks.append(
        _check(
            "executable",
            "启动程序",
            executable_ok,
            "启动程序可由 Worker 当前运行环境找到。"
            if executable_ok
            else "Worker 当前运行环境找不到可执行的启动程序。",
        )
    )

    runtime_kind = runtime.get("kind", "system")
    runtime_ok = runtime_kind == "system"
    runtime_detail = "使用 Worker 当前系统运行环境。"
    runtime_status = "passed"
    if runtime_kind == "conda":
        environment = runtime.get("conda_environment")
        conda_available = shutil.which("conda") is not None
        runtime_ok = isinstance(environment, str) and bool(environment) and conda_available
        runtime_detail = (
            "已找到 Conda；具体环境将在真实执行接入时再次确认。"
            if runtime_ok
            else "未填写 Conda 环境，或 Worker 当前环境找不到 conda。"
        )
        runtime_status = "warning" if runtime_ok else "failed"
    elif runtime_kind != "system":
        runtime_detail = "运行环境类型不受支持。"
    checks.append(
        {
            "code": "runtime_environment",
            "label": "运行环境",
            "status": runtime_status if runtime_ok else "failed",
            "detail": runtime_detail,
        }
    )

    output_path = Path(output_root)
    output_parent = _nearest_existing_parent(output_path)
    output_ok = (
        output_path.is_absolute()
        and output_parent is not None
        and output_parent.is_dir()
        and os.access(output_parent, os.W_OK | os.X_OK)
    )
    checks.append(
        _check(
            "output_root",
            "输出目录",
            output_ok,
            "输出目录或其最近父目录可写；验证过程未创建文件。"
            if output_ok
            else "输出目录及其现有父目录不可写，或路径不是绝对路径。",
        )
    )

    disk_ok = False
    disk_detail = "无法读取输出位置的磁盘剩余空间。"
    if output_parent is not None:
        try:
            usage = shutil.disk_usage(output_parent)
            disk_ok = usage.free > 0
            disk_detail = f"输出位置可用空间约 {usage.free / (1024 ** 3):.1f} GiB。"
        except OSError:
            pass
    checks.append(_check("disk_space", "磁盘空间", disk_ok, disk_detail))

    succeeded = all(check["status"] != "failed" for check in checks)
    return {
        "status": "succeeded" if succeeded else "failed",
        "checks": checks,
    }


def _required_text(payload: Mapping[str, object], key: str, maximum: int) -> str:
    value = payload.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError(f"{key} is invalid")
    return value


def _nearest_existing_parent(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate


def _check(code: str, label: str, passed: bool, detail: str) -> dict[str, str]:
    return {
        "code": code,
        "label": label,
        "status": "passed" if passed else "failed",
        "detail": detail,
    }

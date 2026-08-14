from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping


def verify_model_configuration(payload: Mapping[str, object]) -> dict[str, object]:
    """Perform deterministic read-only checks for a model family's current definition."""
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

    runtime_kind = runtime.get("kind", "system")
    runtime_ok = runtime_kind == "system"
    runtime_detail = "使用 Worker 当前系统运行环境。"
    executable_path: str | None = None
    if runtime_kind == "conda":
        environment = runtime.get("conda_environment")
        conda_executable = _find_conda_executable()
        runtime_ok, executable_path = _probe_conda_environment(
            conda_executable,
            environment,
            executable,
            workdir,
        )
        if conda_executable is None:
            runtime_detail = "SSH 运行账号下未找到 Conda。"
        elif runtime_ok:
            runtime_detail = f"Conda 环境 {environment} 可用。"
        else:
            runtime_detail = f"Conda 环境 {environment or ''} 不存在或无法启动。"
    elif runtime_kind != "system":
        runtime_detail = "运行环境类型不受支持。"
    else:
        executable_path = _find_system_executable(executable, workdir)
    checks.append(
        {
            "code": "runtime_environment",
            "label": "运行环境",
            "status": "passed" if runtime_ok else "failed",
            "detail": runtime_detail,
        }
    )
    executable_ok = executable_path is not None
    checks.append(
        _check(
            "executable",
            "启动程序",
            executable_ok,
            "启动程序可由所选运行环境找到。"
            if executable_ok
            else "所选运行环境找不到可执行的启动程序。",
        )
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


def _find_system_executable(executable: str, workdir: Path) -> str | None:
    candidate = Path(executable)
    if candidate.is_absolute() or "/" in executable:
        if not candidate.is_absolute():
            candidate = workdir / candidate
        return (
            str(candidate)
            if candidate.is_file() and os.access(candidate, os.X_OK)
            else None
        )
    return shutil.which(executable)


def _find_conda_executable() -> str | None:
    home = Path.home()
    candidates = (
        os.environ.get("DATAPILOT_CONDA_EXECUTABLE"),
        os.environ.get("CONDA_EXE"),
        shutil.which("conda"),
        *(str(home / name / "bin" / "conda") for name in (
            "miniconda3",
            "anaconda3",
            "miniforge3",
            "mambaforge",
        )),
        "/opt/conda/bin/conda",
    )
    for raw_candidate in candidates:
        if not raw_candidate:
            continue
        candidate = Path(raw_candidate).expanduser()
        if (
            candidate.is_absolute()
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ):
            return str(candidate.resolve())
    return None


def _probe_conda_environment(
    conda_executable: str | None,
    environment: object,
    executable: str,
    workdir: Path,
) -> tuple[bool, str | None]:
    if (
        conda_executable is None
        or not isinstance(environment, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", environment)
        or not workdir.is_dir()
    ):
        return False, None
    try:
        completed = subprocess.run(
            [
                conda_executable,
                "info",
                "--json",
            ],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        return False, None
    try:
        response = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None
    if not isinstance(response, dict):
        return False, None
    root_prefix = response.get("root_prefix")
    raw_environments = response.get("envs")
    prefixes = [
        value
        for value in (
            root_prefix,
            *(raw_environments if isinstance(raw_environments, list) else []),
        )
        if isinstance(value, str) and Path(value).is_absolute()
    ]
    selected_prefix = next(
        (
            prefix
            for prefix in prefixes
            if (environment == "base" and prefix == root_prefix)
            or Path(prefix).name == environment
        ),
        None,
    )
    if selected_prefix is None:
        return False, None
    candidate = Path(executable)
    if candidate.is_absolute() or "/" in executable:
        if not candidate.is_absolute():
            candidate = workdir / candidate
    else:
        candidate = Path(selected_prefix) / "bin" / executable
    resolved = (
        str(candidate)
        if candidate.is_file() and os.access(candidate, os.X_OK)
        else None
    )
    return True, resolved


def _check(code: str, label: str, passed: bool, detail: str) -> dict[str, str]:
    return {
        "code": code,
        "label": label,
        "status": "passed" if passed else "failed",
        "detail": detail,
    }

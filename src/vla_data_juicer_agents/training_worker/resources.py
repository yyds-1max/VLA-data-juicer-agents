from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
from typing import Callable, Sequence


NVIDIA_SMI_COMMAND = (
    "nvidia-smi",
    "--query-gpu=uuid,index,name,memory.total,memory.used,utilization.gpu,temperature.gpu",
    "--format=csv,noheader,nounits",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


GpuCommandRunner = Callable[[tuple[str, ...], float], CommandResult]


class ResourceCollector:
    """Collect read-only host resources without inspecting model projects."""

    def __init__(
        self,
        *,
        disk_paths: Sequence[Path] = (Path("/"),),
        gpu_command_runner: GpuCommandRunner | None = None,
    ) -> None:
        self.disk_paths = tuple(Path(path).expanduser() for path in disk_paths)
        self._gpu_command_runner = gpu_command_runner or _run_fixed_nvidia_smi

    def collect(self) -> dict[str, object]:
        gpu_payload, gpu_source, gpu_error = self._collect_gpus()
        return {
            "schema_version": 1,
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "host": {
                "hostname": socket.gethostname(),
                "os": platform.system(),
                "os_release": platform.release(),
                "architecture": platform.machine(),
            },
            "cpu": {
                "logical_cores": os.cpu_count() or 1,
                "load_1m": _load_one_minute(),
            },
            "memory": _memory_payload(),
            "disks": [_disk_payload(path) for path in self.disk_paths],
            "gpus": gpu_payload,
            "gpu_collection": {
                "source": gpu_source,
                "error": gpu_error,
            },
        }

    def _collect_gpus(self) -> tuple[list[dict[str, object]], str, str | None]:
        try:
            gpus = _collect_nvml()
        except Exception as exc:
            nvml_error = f"{type(exc).__name__}: {exc}"
        else:
            return gpus, "nvml", None

        try:
            result = self._gpu_command_runner(NVIDIA_SMI_COMMAND, 5.0)
        except (OSError, subprocess.SubprocessError) as exc:
            return [], "unavailable", f"nvidia-smi unavailable ({type(exc).__name__})"
        except Exception as exc:
            return [], "unavailable", f"nvidia-smi collection failed ({type(exc).__name__})"
        if result.returncode != 0:
            return [], "unavailable", f"nvidia-smi exited with status {result.returncode}"
        try:
            return _parse_nvidia_smi(result.stdout), "nvidia-smi", None
        except ValueError as exc:
            return [], "unavailable", f"invalid nvidia-smi output: {exc} (NVML: {nvml_error})"


def _run_fixed_nvidia_smi(command: tuple[str, ...], timeout_seconds: float) -> CommandResult:
    """Run only the immutable inventory command; never invoke a shell."""

    if command != NVIDIA_SMI_COMMAND:
        raise ValueError("only the fixed nvidia-smi inventory command is allowed")
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _collect_nvml() -> list[dict[str, object]]:
    import pynvml  # type: ignore[import-not-found]

    pynvml.nvmlInit()
    try:
        samples: list[dict[str, object]] = []
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
            samples.append(
                {
                    "uuid": _decode_nvml(pynvml.nvmlDeviceGetUUID(handle)),
                    "index": index,
                    "name": _decode_nvml(pynvml.nvmlDeviceGetName(handle)),
                    "memory_total_bytes": int(memory.total),
                    "memory_used_bytes": int(memory.used),
                    "utilization_percent": float(utilization.gpu),
                    "temperature_celsius": float(
                        pynvml.nvmlDeviceGetTemperature(
                            handle,
                            pynvml.NVML_TEMPERATURE_GPU,
                        )
                    ),
                }
            )
        return samples
    finally:
        pynvml.nvmlShutdown()


def _parse_nvidia_smi(output: str) -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    for row in csv.reader(line for line in output.splitlines() if line.strip()):
        if len(row) != 7:
            raise ValueError("expected seven GPU fields")
        uuid, index, name, memory_total, memory_used, utilization, temperature = (
            value.strip() for value in row
        )
        try:
            samples.append(
                {
                    "uuid": uuid,
                    "index": int(index),
                    "name": name,
                    "memory_total_bytes": int(float(memory_total) * 1024 * 1024),
                    "memory_used_bytes": int(float(memory_used) * 1024 * 1024),
                    "utilization_percent": float(utilization),
                    "temperature_celsius": float(temperature),
                }
            )
        except ValueError as exc:
            raise ValueError("GPU fields must be numeric where required") from exc
    return samples


def _decode_nvml(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _load_one_minute() -> float | None:
    try:
        return round(os.getloadavg()[0], 4)
    except (AttributeError, OSError):
        return None


def _memory_payload() -> dict[str, int]:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                key, raw_value = line.split(":", 1)
                values[key] = int(raw_value.strip().split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            values = {}
        if "MemTotal" in values:
            available = values.get("MemAvailable")
            return {
                "total_bytes": values["MemTotal"],
                "available_bytes": available if available is not None else 0,
            }

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total_pages = os.sysconf("SC_PHYS_PAGES")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        # The control-plane contract requires positive totals.  This only
        # occurs on unusual platforms which expose no physical memory API.
        return {"total_bytes": 1, "available_bytes": 0}
    total = int(page_size * total_pages)
    available = int(page_size * available_pages)
    return {
        "total_bytes": total,
        "available_bytes": available,
    }


def _disk_payload(path: Path) -> dict[str, object]:
    resolved = path.resolve()
    try:
        usage = shutil.disk_usage(resolved)
    except OSError as exc:
        return {
            "mount": str(resolved),
            "total_bytes": 1,
            "available_bytes": 0,
        }
    return {
        "mount": str(resolved),
        "total_bytes": usage.total,
        "available_bytes": usage.free,
    }

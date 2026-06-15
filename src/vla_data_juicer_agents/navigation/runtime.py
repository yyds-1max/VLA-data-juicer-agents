from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Sequence


@dataclass(frozen=True)
class NavigationRuntime:
    data_python: str = "python3"
    data_env_setup: Path | None = None


def quote_argv(argv: Sequence[str | Path]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in argv)


def python_data_command(
    runtime: NavigationRuntime,
    script: str | Path,
    args: Sequence[str | Path] = (),
) -> list[str]:
    script_argv = [script, *args]
    if runtime.data_env_setup is None:
        return [runtime.data_python, *[str(arg) for arg in script_argv]]

    shell = " && ".join(
        [
            f"export AGENT_DATA_PYTHON={shlex.quote(runtime.data_python)}",
            f"source {shlex.quote(str(runtime.data_env_setup))}",
            f'exec "$AGENT_DATA_PYTHON" {quote_argv(script_argv)}',
        ]
    )
    return ["bash", "-lc", shell]


def data_runtime_command(
    runtime: NavigationRuntime,
    argv: Sequence[str | Path],
) -> list[str]:
    if runtime.data_env_setup is None:
        return [str(arg) for arg in argv]

    shell = (
        f"source {shlex.quote(str(runtime.data_env_setup))} "
        f"&& exec {quote_argv(argv)}"
    )
    return ["bash", "-lc", shell]


def run_u_python_command(
    runtime: NavigationRuntime,
    *,
    script_name: str | Path,
    args: Sequence[str | Path] = (),
    ros2_setup_bash: str | Path,
    ros2_ws_setup_bash: str | Path,
    shm_msgs_lib_dir: str | Path,
) -> list[str]:
    setup_parts = [
        f"export AGENT_DATA_PYTHON={shlex.quote(runtime.data_python)}",
    ]
    if runtime.data_env_setup is not None:
        setup_parts.append(f"source {shlex.quote(str(runtime.data_env_setup))}")
    setup_parts.extend(
        [
            f"source {shlex.quote(str(ros2_setup_bash))}",
            f"source {shlex.quote(str(ros2_ws_setup_bash))}",
            (
                "export LD_LIBRARY_PATH="
                f"{shlex.quote(str(shm_msgs_lib_dir))}:"
                '${LD_LIBRARY_PATH:-}'
            ),
            f'exec "$AGENT_DATA_PYTHON" {quote_argv([script_name, *args])}',
        ]
    )
    return ["bash", "-lc", " && ".join(setup_parts)]

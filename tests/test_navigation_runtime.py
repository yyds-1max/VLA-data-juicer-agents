from pathlib import Path

from vla_data_juicer_agents.navigation.runtime import (
    NavigationRuntime,
    data_runtime_command,
    python_data_command,
    quote_argv,
    run_u_python_command,
)
from vla_data_juicer_agents.navigation.config import NavigationSettings


def test_quote_argv_quotes_shell_arguments():
    assert (
        quote_argv(["python3.8", "script path.py", "--name", "a b"])
        == "python3.8 'script path.py' --name 'a b'"
    )


def test_python_data_command_without_setup_uses_data_python():
    runtime = NavigationRuntime(data_python="/usr/bin/python3.8", data_env_setup=None)

    command = python_data_command(runtime, Path("/tools/script.py"), ["--date", "20270605"])

    assert command == ["/usr/bin/python3.8", "/tools/script.py", "--date", "20270605"]


def test_python_data_command_with_setup_sources_legacy_env():
    runtime = NavigationRuntime(
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    command = python_data_command(runtime, Path("/tools/script.py"), ["--date", "20270605"])

    assert command[:2] == ["bash", "-lc"]
    assert "export AGENT_DATA_PYTHON=/usr/bin/python3.8" in command[2]
    assert "source /env/setup_data_runtime.sh" in command[2]
    assert 'exec "$AGENT_DATA_PYTHON" /tools/script.py --date 20270605' in command[2]


def test_data_runtime_command_sources_setup_for_binary():
    runtime = NavigationRuntime(
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    command = data_runtime_command(runtime, ["./bin/main"])

    assert command == ["bash", "-lc", "source /env/setup_data_runtime.sh && exec ./bin/main"]


def test_run_u_python_command_sources_ros_and_shm_paths():
    runtime = NavigationRuntime(
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    command = run_u_python_command(
        runtime,
        script_name="1_extract_data_from_bag_multi_process_ros2_U.py",
        args=["--data_path", "/raw"],
        ros2_setup_bash=Path("/gt/modules/message/ros2/install/setup.bash"),
        ros2_ws_setup_bash=Path("/gt/modules/ros2_ws/src/install/setup.bash"),
        shm_msgs_lib_dir=Path("/gt/modules/message/shm/install/shm_msgs/lib"),
    )

    assert command[:2] == ["bash", "-lc"]
    shell = command[2]
    assert "source /env/setup_data_runtime.sh" in shell
    assert "source /gt/modules/message/ros2/install/setup.bash" in shell
    assert "source /gt/modules/ros2_ws/src/install/setup.bash" in shell
    assert "/gt/modules/message/shm/install/shm_msgs/lib" in shell
    assert (
        'exec "$AGENT_DATA_PYTHON" 1_extract_data_from_bag_multi_process_ros2_U.py '
        "--data_path /raw"
    ) in shell


def test_navigation_settings_reads_legacy_runtime_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_PYTHON", "/usr/bin/python3.8")
    monkeypatch.setenv("AGENT_DATA_ENV_SETUP", str(tmp_path / "setup_data_runtime.sh"))
    monkeypatch.setenv("VLA_GT_DOG_ROOT", str(tmp_path / "GT_dog"))

    settings = NavigationSettings()

    assert settings.runtime.data_python == "/usr/bin/python3.8"
    assert settings.runtime.data_env_setup == tmp_path / "setup_data_runtime.sh"
    assert (
        settings.ros2_setup_bash
        == tmp_path / "GT_dog" / "modules" / "message" / "ros2" / "install" / "setup.bash"
    )
    assert (
        settings.ros2_ws_setup_bash
        == tmp_path / "GT_dog" / "modules" / "ros2_ws" / "src" / "install" / "setup.bash"
    )
    assert (
        settings.shm_msgs_lib_dir
        == tmp_path / "GT_dog" / "modules" / "message" / "shm" / "install" / "shm_msgs" / "lib"
    )

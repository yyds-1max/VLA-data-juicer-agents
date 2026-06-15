import os
from pathlib import Path

from pydantic import BaseModel, Field

from vla_data_juicer_agents.navigation.runtime import NavigationRuntime


DEFAULT_VLADATASETS_ROOT = Path("/media/heying/hy_data1/VLADatasets")
DEFAULT_PROCESSING_ROOT = Path(
    "/media/heying/hy_data1/Trajectory_visualization/"
    "Object_location_gh_v3_fisheye_five_U_add_SF_01"
)
DEFAULT_DATATOOLBOX_SRC = Path("/media/heying/hy_data2/GT_dog/modules_ros2/DataToolbox/src")
DEFAULT_GT_DOG_ROOT = Path("/media/heying/hy_data2/GT_dog")


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value) if value else default


def _optional_env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value else None


class NavigationSettings(BaseModel):
    vladatasets_root: Path = Field(
        default_factory=lambda: _env_path("VLA_VLADATASETS_ROOT", DEFAULT_VLADATASETS_ROOT)
    )
    processing_root: Path = Field(
        default_factory=lambda: _env_path("VLA_PROCESSING_ROOT", DEFAULT_PROCESSING_ROOT)
    )
    datatoolbox_src: Path = Field(
        default_factory=lambda: _env_path("VLA_DATATOOLBOX_SRC", DEFAULT_DATATOOLBOX_SRC)
    )
    runs_root: Path = Field(default_factory=lambda: _env_path("VLA_RUNS_ROOT", Path("runs/navigation")))
    python_bin: str = Field(default_factory=lambda: os.getenv("VLA_PYTHON_BIN", "python3"))
    data_python: str = Field(default_factory=lambda: os.getenv("AGENT_DATA_PYTHON", "python3"))
    data_env_setup: Path | None = Field(
        default_factory=lambda: _optional_env_path("AGENT_DATA_ENV_SETUP")
    )
    gt_dog_root: Path = Field(
        default_factory=lambda: _env_path("VLA_GT_DOG_ROOT", DEFAULT_GT_DOG_ROOT)
    )
    process_owner: str | None = Field(default_factory=lambda: os.getenv("VLA_PROCESS_OWNER"))

    @property
    def runtime(self) -> NavigationRuntime:
        return NavigationRuntime(data_python=self.data_python, data_env_setup=self.data_env_setup)

    @property
    def raw_data_root(self) -> Path:
        return self.vladatasets_root / "raw_data"

    @property
    def clip_data_root(self) -> Path:
        return self.vladatasets_root / "clip_data"

    @property
    def finish_data_root(self) -> Path:
        return self.vladatasets_root / "finish_data"

    @property
    def pcd_to_grid_script(self) -> Path:
        return self.processing_root / "other_code" / "pcd_to_grid.py"

    @property
    def gen_box_script(self) -> Path:
        return self.processing_root / "0_1th_box" / "gen_box.py"

    @property
    def ros2_setup_bash(self) -> Path:
        return self.gt_dog_root / "modules" / "message" / "ros2" / "install" / "setup.bash"

    @property
    def ros2_ws_setup_bash(self) -> Path:
        return self.gt_dog_root / "modules" / "ros2_ws" / "src" / "install" / "setup.bash"

    @property
    def shm_msgs_lib_dir(self) -> Path:
        return self.gt_dog_root / "modules" / "message" / "shm" / "install" / "shm_msgs" / "lib"

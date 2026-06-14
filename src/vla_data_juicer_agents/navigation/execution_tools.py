from agents import function_tool


@function_tool(strict_mode=False)
def prepare_raw_data_tool(date: str, segments: list[str] | None = None) -> dict:
    """Prepare raw navigation data for a date, using all raw segments when omitted."""
    return {"ok": True, "dry_run": True, "tool_name": "prepare_raw_data", "date": date, "segments": segments}


@function_tool(strict_mode=False)
def extract_and_sync_navigation_data_tool(
    date: str,
    dataset_profile: str,
    segments: list[str] | None = None,
    processes_num: int = 4,
) -> dict:
    """Extract and synchronize navigation data for the selected raw segments."""
    return {
        "ok": True,
        "dry_run": True,
        "tool_name": "extract_and_sync_navigation_data",
        "date": date,
        "dataset_profile": dataset_profile,
        "segments": segments,
        "processes_num": processes_num,
    }


@function_tool(strict_mode=False)
def generate_gridmap_from_pcd_tool(date: str, segments: list[str] | None = None) -> dict:
    """Generate grid maps from PCD outputs, or report dry-run intent."""
    return {"ok": True, "dry_run": True, "tool_name": "generate_gridmap_from_pcd", "date": date, "segments": segments}


@function_tool(strict_mode=False)
def assemble_finish_temp_tool(date: str, segments: list[str] | None = None) -> dict:
    """Assemble finish_temp inputs for stage-one navigation processing."""
    return {"ok": True, "dry_run": True, "tool_name": "assemble_finish_temp", "date": date, "segments": segments}


@function_tool
def run_noobscene_preprocessing_tool(finish_temp_path: str) -> dict:
    """Run noobscene preprocessing for a finish_temp path."""
    return {
        "ok": True,
        "dry_run": True,
        "tool_name": "run_noobscene_preprocessing",
        "finish_temp_path": finish_temp_path,
    }


@function_tool
def run_initial_annotation_gui_tool(finish_temp_path: str) -> dict:
    """Launch the human-blocking gen_box.py annotation GUI for finish_temp."""
    return {
        "ok": True,
        "dry_run": True,
        "tool_name": "run_initial_annotation_gui",
        "finish_temp_path": finish_temp_path,
        "human_blocking": True,
    }


@function_tool
def run_tracking_and_projection_tool(finish_temp_path: str, finish_path: str) -> dict:
    """Run tracking and projection after human annotation is complete."""
    return {
        "ok": True,
        "dry_run": True,
        "tool_name": "run_tracking_and_projection",
        "finish_temp_path": finish_temp_path,
        "finish_path": finish_path,
    }


@function_tool
def validate_navigation_outputs_tool(date: str) -> dict:
    """Validate the expected stage-one navigation outputs for a date."""
    return {"ok": True, "dry_run": True, "tool_name": "validate_navigation_outputs", "date": date}

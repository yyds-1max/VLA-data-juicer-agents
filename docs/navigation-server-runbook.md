# Navigation Server Runbook

## Environment

```bash
cd /path/to/VLA-data-juicer-agents
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export DASHSCOPE_API_KEY="sk-..."
export DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export VLA_AGENT_MODEL="qwen3.5-plus"
export VLA_VLADATASETS_ROOT="/media/heying/hy_data1/VLADatasets"
export VLA_PROCESSING_ROOT="/media/heying/hy_data1/Trajectory_visualization/Object_location_gh_v3_fisheye_five_U_add_SF_01"
export VLA_DATATOOLBOX_SRC="/media/heying/hy_data2/GT_dog/modules_ros2/DataToolbox/src"
```

## Plan only

```bash
vla-nav-agent plan --date 20270605
```

## Dry-run without LLM for command inspection

```bash
vla-nav-agent plan --date 20270605 --segments 20260605_152856 --dry-run --no-llm
```

## Full run

```bash
vla-nav-agent run --date 20270605
```

When `gen_box.py` opens, complete the manual annotation. The workflow continues after the GUI process exits.

## Stage-one scope

This runbook covers `prepare.sh`, `run_U.sh`, and `run_odom.sh`. It does not run `run_fix.sh`.

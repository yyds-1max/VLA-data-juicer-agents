# VLA Data Juicer Agents

This project builds an OpenAI Agents SDK workflow for the first-stage navigation data pipeline:

1. prepare raw ROS bag segment links
2. extract and synchronize navigation data
3. generate gridmap from PCD when needed
4. assemble `finish_data/<date>_temp`
5. run `run_odom.sh` stages through initial annotation, tracking, projection, and final move

Stage one intentionally excludes `run_fix.sh`.

## WSL setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export DASHSCOPE_API_KEY="sk-..."
export DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export VLA_AGENT_MODEL="qwen3.5-plus"
```

## Dry run

```bash
vla-nav-agent plan --date 20270605 --dry-run
```

## Execute

```bash
vla-nav-agent run --date 20270605
```

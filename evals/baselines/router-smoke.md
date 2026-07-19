# Evaluation baseline

Total: 9 — PASS 3, FAIL 6, TIMEOUT 0, ERROR 0

## Version anchors

- Git commit: `b639c16978505378c61cbc1b20e7fa07f01bc5d2`
- Model: `qwen3.5-plus`
- Model parameters: `{"parallel_tool_calls": false}`
- AgentScope: `2.0.1`
- Cases SHA-256: `a1b71a7b975a1b8e29bca8fcfbef6a98a5171b71b605301c7dc2c9226c7dcbe3`
- Prompt SHA-256: `ffcc4e2bc4431ff0b3d4a544ca0898b0ab16354823f8e681b360e6aa1873893f`
- Tool Schema SHA-256: `bb6af219a260b1f2dd6504437da2945cf72772e622dd0006c52bd602e02b3ee1`

## Stability

| Case | Attempts | Stability | Pass rate | PASS | FAIL | TIMEOUT | ERROR | Failure signatures |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| router_capability_no_handoff | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_missing_target_clarifies | 3 | STABLE_FAIL | 0.000 | 0 | 3 | 0 | 0 | response.length ×2; response.question ×3 |
| router_shortcut_preserves_scope | 3 | STABLE_FAIL | 0.000 | 0 | 3 | 0 | 0 | handoff.count ×2; handoff.request ×1; limits.tool_calls ×2; response.length ×1; tools.allowed ×3; tools.count.start_navigation_data_task ×2; tools.safety ×3 |

## Results

| Case | Repeat | Status | Model calls | Tool calls | Tokens | Failure signatures |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| router_capability_no_handoff | 1 | PASS | 1 | 0 | 9431 | — |
| router_capability_no_handoff | 2 | PASS | 1 | 0 | 9427 | — |
| router_capability_no_handoff | 3 | PASS | 1 | 0 | 9434 | — |
| router_missing_target_clarifies | 1 | FAIL | 1 | 0 | 9379 | response.question |
| router_missing_target_clarifies | 2 | FAIL | 1 | 0 | 9406 | response.length; response.question |
| router_missing_target_clarifies | 3 | FAIL | 1 | 0 | 9404 | response.length; response.question |
| router_shortcut_preserves_scope | 1 | FAIL | 2 | 2 | 19275 | handoff.count; limits.tool_calls; tools.allowed; tools.count.start_navigation_data_task; tools.safety |
| router_shortcut_preserves_scope | 2 | FAIL | 1 | 1 | 9659 | handoff.count; tools.allowed; tools.count.start_navigation_data_task; tools.safety |
| router_shortcut_preserves_scope | 3 | FAIL | 3 | 2 | 29539 | handoff.request; limits.tool_calls; response.length; tools.allowed; tools.safety |

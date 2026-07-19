# Evaluation baseline

Total: 9 — PASS 3, FAIL 6, TIMEOUT 0, ERROR 0

## Version anchors

- Git commit: `fd311752d949a7e5997ad35e20e702e47f6d9d83`
- Evaluation contract: `2`
- Model: `qwen3.5-plus`
- Model parameters: `{"parallel_tool_calls": false}`
- AgentScope: `2.0.1`
- Cases SHA-256: `a1b71a7b975a1b8e29bca8fcfbef6a98a5171b71b605301c7dc2c9226c7dcbe3`
- Prompt SHA-256: `ffcc4e2bc4431ff0b3d4a544ca0898b0ab16354823f8e681b360e6aa1873893f`
- Tool Schema SHA-256: `bb6af219a260b1f2dd6504437da2945cf72772e622dd0006c52bd602e02b3ee1`

## Stability

| Case | Attempts | Stability | Pass rate | PASS | FAIL | TIMEOUT | ERROR | Failure signatures |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| router_capability_no_handoff | 3 | FLAKY | 0.667 | 2 | 1 | 0 | 0 | response.language ×1; response.required_group.0 ×1 |
| router_missing_target_clarifies | 3 | FLAKY | 0.333 | 1 | 2 | 0 | 0 | response.question ×2 |
| router_shortcut_preserves_scope | 3 | STABLE_FAIL | 0.000 | 0 | 3 | 0 | 0 | handoff.count ×3; response.language ×3; tools.allowed ×3; tools.count.start_navigation_data_task ×3; tools.safety ×3 |

## Results

| Case | Repeat | Status | Model calls | Tool calls | Tokens | Failure signatures |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| router_capability_no_handoff | 1 | FAIL | 1 | 0 | 9438 | response.language; response.required_group.0 |
| router_capability_no_handoff | 2 | PASS | 1 | 0 | 9436 | — |
| router_capability_no_handoff | 3 | PASS | 1 | 0 | 9422 | — |
| router_missing_target_clarifies | 1 | PASS | 1 | 0 | 9376 | — |
| router_missing_target_clarifies | 2 | FAIL | 1 | 0 | 9385 | response.question |
| router_missing_target_clarifies | 3 | FAIL | 1 | 0 | 9384 | response.question |
| router_shortcut_preserves_scope | 1 | FAIL | 1 | 1 | 9617 | handoff.count; response.language; tools.allowed; tools.count.start_navigation_data_task; tools.safety |
| router_shortcut_preserves_scope | 2 | FAIL | 1 | 1 | 9565 | handoff.count; response.language; tools.allowed; tools.count.start_navigation_data_task; tools.safety |
| router_shortcut_preserves_scope | 3 | FAIL | 1 | 1 | 9627 | handoff.count; response.language; tools.allowed; tools.count.start_navigation_data_task; tools.safety |

# Evaluation baseline

Total: 3 — PASS 3, FAIL 0, TIMEOUT 0, ERROR 0

## Version anchors

- Git commit: `0e738ab302dc9090ca74cbee4f2e60e54b991d3f`
- Model: `qwen3.5-plus`
- Model parameters: `{"parallel_tool_calls": false}`
- AgentScope: `2.0.1`
- Cases SHA-256: `a1b71a7b975a1b8e29bca8fcfbef6a98a5171b71b605301c7dc2c9226c7dcbe3`
- Prompt SHA-256: `c44de49dced6fb5e88468cd3caf3cca2ed6ecde7fcb05504ed0ce0e581179b2c`
- Tool Schema SHA-256: `7e29843752dd0b6322c84e1c79f06e3b99cb55ca2c6e99fef7ac90afaceaf8fd`

## Results

| Case | Repeat | Status | Model calls | Tool calls | Tokens | Failure reasons |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| router_capability_no_handoff | 1 | PASS | 1 | 0 | 9563 | — |
| router_missing_target_clarifies | 1 | PASS | 1 | 0 | 9453 | — |
| router_shortcut_preserves_scope | 1 | PASS | 2 | 1 | 19667 | — |

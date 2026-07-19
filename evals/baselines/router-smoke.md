# Evaluation baseline

Total: 3 — PASS 1, FAIL 2, TIMEOUT 0, ERROR 0

## Version anchors

- Git commit: `bd5b3ac08469a6d9bd463fb0820ca41bf85a9502`
- Model: `qwen3.5-plus`
- Model parameters: `{"parallel_tool_calls": false}`
- AgentScope: `2.0.1`
- Cases SHA-256: `a1b71a7b975a1b8e29bca8fcfbef6a98a5171b71b605301c7dc2c9226c7dcbe3`
- Prompt SHA-256: `ffcc4e2bc4431ff0b3d4a544ca0898b0ab16354823f8e681b360e6aa1873893f`
- Tool Schema SHA-256: `bb6af219a260b1f2dd6504437da2945cf72772e622dd0006c52bd602e02b3ee1`

## Results

| Case | Repeat | Status | Model calls | Tool calls | Tokens | Failure reasons |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| router_capability_no_handoff | 1 | PASS | 1 | 0 | 9427 | — |
| router_missing_target_clarifies | 1 | FAIL | 1 | 0 | 9384 | response did not contain a question; response length 122 exceeded 120 |
| router_shortcut_preserves_scope | 1 | FAIL | 2 | 1 | 19364 | handoff request did not exactly match the expected value |

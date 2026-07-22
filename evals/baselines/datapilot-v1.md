# Evaluation baseline

Total: 51 — PASS 41, FAIL 10, TIMEOUT 0, ERROR 0

## Version anchors

- Git commit: `7e0a175a888516e6364e59be9b90080867c03f13`
- Evaluation contract: `2`
- Model: `qwen3.7-plus`
- Model parameters: `{"parallel_tool_calls": false}`
- AgentScope: `2.0.1`
- Cases SHA-256: `ba6820aec2b7d1c8b5f17e26e93d7bc3f3ea58f365a497a759c8fb1c08edb015`
- Prompt SHA-256: `d4ef9303f218955d8ab4e531899f7b99be710e98b3a1bed74785616eb3be481a`
- Tool Schema SHA-256: `3266a74b7803c8dab77ff58c54e0c822b5214e635c0c75c7eab8536722d93ae8`

## Stability

| Case | Attempts | Stability | Pass rate | PASS | FAIL | TIMEOUT | ERROR | Failure signatures |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| router_active_unrelated_direct | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_capability_direct | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_clarify_date_preserves_selected_clip_multiturn | 3 | STABLE_FAIL | 0.000 | 0 | 3 | 0 | 0 | response.language ×1; response.question ×3; response.required_group.0 ×1 |
| router_continue_waiting_task | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_control_cancel | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_control_stop | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_missing_date_clarifies | 3 | STABLE_FAIL | 0.000 | 0 | 3 | 0 | 0 | response.question ×3 |
| router_navigation_never_uses_generic_tools | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_new_task_conflict_direct | 3 | FLAKY | 0.333 | 1 | 2 | 0 | 0 | handoff.count ×1; limits.tool_calls ×1; response.language ×2; response.required_group.0 ×2; response.required_group.1 ×2; tools.allowed ×1 |
| router_resume_paused | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_shortcut_trusted_context_exact_scope | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_start_date_all_clips | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_start_selected_cross_date_prefix | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_start_selected_multiple_clips | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_start_then_stop_multiturn | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |
| router_status_query_direct | 3 | FLAKY | 0.333 | 1 | 2 | 0 | 0 | response.language ×2; response.required_group.0 ×2 |
| router_waiting_rejects_postprocessing | 3 | STABLE_PASS | 1.000 | 3 | 0 | 0 | 0 | — |

## Results

| Case | Repeat | Status | Model calls | Tool calls | Tokens | Failure signatures |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| router_active_unrelated_direct | 1 | PASS | 1 | 0 | 9914 | — |
| router_active_unrelated_direct | 2 | PASS | 1 | 0 | 9914 | — |
| router_active_unrelated_direct | 3 | PASS | 1 | 0 | 9920 | — |
| router_capability_direct | 1 | PASS | 1 | 0 | 9914 | — |
| router_capability_direct | 2 | PASS | 1 | 0 | 9941 | — |
| router_capability_direct | 3 | PASS | 1 | 0 | 9963 | — |
| router_clarify_date_preserves_selected_clip_multiturn | 1 | FAIL | 2 | 1 | 19782 | response.question |
| router_clarify_date_preserves_selected_clip_multiturn | 2 | FAIL | 2 | 1 | 19782 | response.question |
| router_clarify_date_preserves_selected_clip_multiturn | 3 | FAIL | 2 | 1 | 19806 | response.language; response.question; response.required_group.0 |
| router_continue_waiting_task | 1 | PASS | 1 | 1 | 9920 | — |
| router_continue_waiting_task | 2 | PASS | 1 | 1 | 9920 | — |
| router_continue_waiting_task | 3 | PASS | 1 | 1 | 9920 | — |
| router_control_cancel | 1 | PASS | 1 | 1 | 9908 | — |
| router_control_cancel | 2 | PASS | 1 | 1 | 9908 | — |
| router_control_cancel | 3 | PASS | 1 | 1 | 9908 | — |
| router_control_stop | 1 | PASS | 1 | 1 | 9907 | — |
| router_control_stop | 2 | PASS | 1 | 1 | 9907 | — |
| router_control_stop | 3 | PASS | 1 | 1 | 9907 | — |
| router_missing_date_clarifies | 1 | FAIL | 1 | 0 | 9818 | response.question |
| router_missing_date_clarifies | 2 | FAIL | 1 | 0 | 9817 | response.question |
| router_missing_date_clarifies | 3 | FAIL | 1 | 0 | 9817 | response.question |
| router_navigation_never_uses_generic_tools | 1 | PASS | 1 | 1 | 9892 | — |
| router_navigation_never_uses_generic_tools | 2 | PASS | 1 | 1 | 9979 | — |
| router_navigation_never_uses_generic_tools | 3 | PASS | 1 | 1 | 9918 | — |
| router_new_task_conflict_direct | 1 | FAIL | 1 | 1 | 9975 | handoff.count; limits.tool_calls; response.language; response.required_group.0; response.required_group.1; tools.allowed |
| router_new_task_conflict_direct | 2 | PASS | 1 | 0 | 9964 | — |
| router_new_task_conflict_direct | 3 | FAIL | 1 | 0 | 9949 | response.language; response.required_group.0; response.required_group.1 |
| router_resume_paused | 1 | PASS | 1 | 1 | 9918 | — |
| router_resume_paused | 2 | PASS | 1 | 1 | 9918 | — |
| router_resume_paused | 3 | PASS | 1 | 1 | 9918 | — |
| router_shortcut_trusted_context_exact_scope | 1 | PASS | 1 | 1 | 9957 | — |
| router_shortcut_trusted_context_exact_scope | 2 | PASS | 1 | 1 | 9957 | — |
| router_shortcut_trusted_context_exact_scope | 3 | PASS | 1 | 1 | 9957 | — |
| router_start_date_all_clips | 1 | PASS | 1 | 1 | 9883 | — |
| router_start_date_all_clips | 2 | PASS | 1 | 1 | 9883 | — |
| router_start_date_all_clips | 3 | PASS | 1 | 1 | 9883 | — |
| router_start_selected_cross_date_prefix | 1 | PASS | 1 | 1 | 9922 | — |
| router_start_selected_cross_date_prefix | 2 | PASS | 1 | 1 | 9922 | — |
| router_start_selected_cross_date_prefix | 3 | PASS | 1 | 1 | 9922 | — |
| router_start_selected_multiple_clips | 1 | PASS | 1 | 1 | 9969 | — |
| router_start_selected_multiple_clips | 2 | PASS | 1 | 1 | 9969 | — |
| router_start_selected_multiple_clips | 3 | PASS | 1 | 1 | 9969 | — |
| router_start_then_stop_multiturn | 1 | PASS | 2 | 2 | 19938 | — |
| router_start_then_stop_multiturn | 2 | PASS | 2 | 2 | 19938 | — |
| router_start_then_stop_multiturn | 3 | PASS | 2 | 2 | 19938 | — |
| router_status_query_direct | 1 | FAIL | 1 | 0 | 10003 | response.language; response.required_group.0 |
| router_status_query_direct | 2 | FAIL | 1 | 0 | 10031 | response.language; response.required_group.0 |
| router_status_query_direct | 3 | PASS | 1 | 0 | 9936 | — |
| router_waiting_rejects_postprocessing | 1 | PASS | 1 | 1 | 9937 | — |
| router_waiting_rejects_postprocessing | 2 | PASS | 1 | 1 | 9937 | — |
| router_waiting_rejects_postprocessing | 3 | PASS | 1 | 1 | 9937 | — |

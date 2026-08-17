# Training task status simplification design QA

- Source visual truth (task page): `/private/var/folders/80/bg0v0z7n3jd43rr470zsfjmr0000gn/T/codex-clipboard-548d7adc-6125-44c8-939d-cc9418514a40.png`
- Source visual truth (status filter): `/private/var/folders/80/bg0v0z7n3jd43rr470zsfjmr0000gn/T/codex-clipboard-bb549e62-0dc7-46fa-9e2a-1b77d8b16108.png`
- Rendered implementation: `/private/tmp/training-runs-status-simplified.png`
- Source pixels: `1658 × 995` and `696 × 514`
- Browser CSS viewport: `1666 × 999`, device scale factor `1`
- Implementation screenshot pixels: `1609 × 990`; the in-app browser surface crops a small amount of browser-owned width
- State: training task list, empty task data, simplified status filter set to `训练中`

## Full-view comparison evidence

- The four-column training overview requested for removal is no longer rendered. The task heading, actions, table header and empty state now move up without leaving a placeholder gap.
- Existing page typography, navigation, table density, blue primary actions and empty-state hierarchy remain unchanged.
- The change uses the existing responsive toolbar. Search, status filter and primary action retain their existing wrapping behavior rather than introducing a new layout.

## Focused-region comparison evidence

- The status filter now contains exactly six visible choices: `全部状态`, `训练中`, `已取消`, `失败`, `已完成`, `状态丢失`.
- Internal `queued`, `preparing`, `running` and `stop_requested` states are all presented as `训练中`; completed, cancelled, failed and lost states retain their existing semantic colors.
- The filter remains a labelled native select with keyboard operation and a visible focus treatment. Selecting `训练中` was exercised successfully.
- No imagery is introduced or replaced. Existing Lucide icons remain consistent with the established product UI.

## Required fidelity surfaces

- Fonts and typography: unchanged from the source product; heading, helper copy, toolbar labels and table labels preserve their existing scale and weight.
- Spacing and layout rhythm: removal of the overview closes the unnecessary vertical gap and keeps the task toolbar aligned with the table.
- Colors and visual tokens: existing console background, border, muted text, primary blue and semantic status tokens are reused.
- Image quality and assets: no raster assets are part of the affected task-list region; existing vector icons remain sharp.
- Copy and content: user-facing status vocabulary is reduced to the five requested concepts without exposing scheduler implementation details.

## Findings and comparison history

No actionable P0, P1 or P2 findings were found in the first comparison. The intentional differences from the source are exactly the requested removal of the overview region and simplification of status vocabulary.

## Primary interactions tested

- Training task page loads with no overview placeholder.
- Status filter exposes only the intended user-facing states.
- Selecting `训练中` updates the select value to the active-state group.
- Unit coverage verifies that queued and running tasks are both included while cancelled tasks are excluded.
- Browser console errors checked: none.

## Residual test gap

The local browser data set is empty, so row colors for all five terminal/active states were verified through component tests rather than live task records.

final result: passed

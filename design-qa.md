# Training UI design QA

- Source visual truth:
  - `/var/folders/80/bg0v0z7n3jd43rr470zsfjmr0000gn/T/codex-clipboard-96815b60-2754-4d63-a8dc-90b2c1e9486f.png`
  - `/var/folders/80/bg0v0z7n3jd43rr470zsfjmr0000gn/T/codex-clipboard-b1c69ff6-280c-4c8a-8040-04c70daaf09f.png`
- Implementation screenshots:
  - `/private/tmp/training-model-editor-viewport.png`
  - `/private/tmp/training-model-editor-sticky.png`
  - `/private/tmp/training-new-run-resources-final.png`
  - `/private/tmp/training-parameter-controls.png`
- Viewport: 1600 × 900 CSS px; device pixel ratio 1.
- Pixel dimensions:
  - Model registration source: 1634 × 846.
  - Resource-panel source: 1663 × 905.
  - Implementation captures: 1585 × 892 (browser content viewport after scrollbar/chrome allocation).
- Density normalization: all compared at DPR 1 and fitted to their native width; no density conversion was required.
- State: new model editor before and after loading the NaVILA preset; sticky command summary after 720 px page scroll; new training with a real Worker resource snapshot; first-stage parameter controls.

## Full-view comparison evidence

The model editor retains the existing page hierarchy and visual tokens while using the previously empty horizontal space for a right-hand command summary. The new-training resource panel follows the reference screen's restrained card, label, progress-bar, and right-rail treatment without copying its placeholder data.

## Focused-region comparison evidence

- The command summary now keeps one executable/argument pair per physical row, scrolls internally on both axes, and remains at `top: 16px` after the page is scrolled 720 px.
- The preset button has no initial focus ring; focus moves to the editor heading when the screen opens. Keyboard-triggered focus styling remains available.
- The new-training resource panel uses the selected Worker's real CPU, memory, GPU, disk, and sample-time fields.
- Bounded numeric parameters keep the exact number input and add a synchronized range control. Boolean parameters use a full-card selected/unselected state while retaining the native checkbox.
- At 900 px viewport width the command summary returns to normal document flow below the form, and the document has no horizontal overflow.

## Findings and comparison history

1. P2: the first resource-panel implementation was tall enough to meet the fixed assistant control at the lower-right edge.
   - Fix: condensed the resource metrics into four compact rows and removed redundant explanatory copy.
   - Post-fix evidence: `/private/tmp/training-new-run-resources-final.png`; all four metrics fit above the assistant control.
2. P2: long command values wrapped across visual rows, weakening the requested one-parameter-per-line structure.
   - Fix: changed the command area to preserved, non-wrapping whitespace with internal horizontal scrolling.
   - Post-fix evidence: sticky positioning is visible in `/private/tmp/training-model-editor-sticky.png`; the final non-wrapping and overflow contract is covered by the TrainingPlatform DOM regression test.
3. P2: entering the editor could leave an apparent blue focus ring on the preset action.
   - Fix: move initial focus to the screen heading without removing keyboard focus styles from the button.
   - Post-fix evidence: browser inspection reported the active element as `H2 登记新模型`; `/private/tmp/training-model-editor-viewport.png` shows no button ring.

No actionable P0, P1, or P2 findings remain.

## Required fidelity surfaces

- Typography: existing product font stack, weights, label sizes, and mono command text are retained.
- Spacing/layout: wide-screen two-column editor and compact right resource rail align with the existing grid rhythm; narrow screens collapse without overflow.
- Colors/tokens: existing console borders, muted text, cyan focus/selection, emerald availability, and slate command surface are reused.
- Image quality/assets: no new bitmap assets were introduced; existing Lucide icons remain sharp and consistent.
- Copy/content: labels describe live summaries, selected resources, and default-value semantics without implying that preview actions execute training.

## Primary interactions tested

- Open model registration and enter new-model mode.
- Load the NaVILA preset and inspect the generated command rows.
- Scroll the editor and confirm the summary remains sticky.
- Open new training and inspect real Worker resources.
- Verify narrow-screen stacking at 900 px.
- Check browser console for blocking errors; none were observed in the tested flow.

final result: passed

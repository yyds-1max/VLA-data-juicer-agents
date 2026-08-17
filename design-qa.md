# Training UI design QA

- Source visual truth:
  - `/var/folders/80/bg0v0z7n3jd43rr470zsfjmr0000gn/T/codex-clipboard-43ff94a6-957c-4e78-bc34-3e120527bff1.png`
  - `/var/folders/80/bg0v0z7n3jd43rr470zsfjmr0000gn/T/codex-clipboard-85cbb3ee-768e-494b-bb5c-fef16ce78576.png`
  - `/var/folders/80/bg0v0z7n3jd43rr470zsfjmr0000gn/T/codex-clipboard-afaeda8e-8fb6-4720-ad88-e00764dd48ce.png`
- Implementation screenshots:
  - `/private/tmp/training-gpu-card-bars-active.png`
  - `/private/tmp/training-model-basic-config-aligned.png`
  - `/private/tmp/training-parameters-compact-restored.png`
- Browser-rendered viewport: 1265 × 712 CSS px; device pixel ratio 1.
- Pixel dimensions:
  - GPU source: 1630 × 507; implementation: 1265 × 712.
  - Model basic-config source: 1169 × 919; implementation: 1265 × 712.
  - Parameter-layout source: 1659 × 998; implementation: 1265 × 712.
- Density normalization: all artifacts are DPR 1. Comparisons use focused content regions because the supplied images use different crops and viewport widths.
- State: real Worker resource snapshot; GPU 6/7 under high utilization; editing a verified NaVILA family; first-stage common parameters.

## Full-view comparison evidence

The revised new-training screen preserves the established single-page flow and right-hand real-resource overview. GPU cards remain in the existing four-column grid instead of changing the page composition. The model editor retains its left form/right sticky command layout, while the basic configuration rows now share a consistent input baseline. The training parameter area returns to the original compact two-column form.

## Focused-region comparison evidence

- GPU cards now pair exact utilization and memory values with independent progress bars. High utilization and memory are immediately visible on GPU 6/7 without removing temperature or platform-lease status.
- Model basic-configuration controls align at their input/select bottom edges even when only one label in the row contains helper copy.
- Boolean parameters are again native compact checkboxes on the field heading row. Integer and floating-point parameters use their original numeric inputs without secondary range sliders.
- Focused regions were required because the important changes are dense form controls and 8-card resource metrics that are not legible in a full-page capture.

## Findings and comparison history

1. P2: the prior GPU cards exposed exact values only, making high utilization and memory pressure slow to scan.
   - Fix: added separate utilization and memory progress bars with warning/danger thresholds while retaining exact values.
   - Post-fix evidence: `/private/tmp/training-gpu-card-bars-active.png`.
2. P2: helper text in one column pushed its control down while the neighboring control stayed high.
   - Fix: bottom-aligned the two-column basic-configuration grid at desktop widths.
   - Post-fix evidence: `/private/tmp/training-model-basic-config-aligned.png`.
3. P1: large boolean cards and numeric sliders changed the established parameter-form density and made long configurations harder to scan.
   - Fix: restored compact boolean checkboxes and number inputs, retaining labels, field names, tooltips, validation, dependencies, and keyboard focus.
   - Post-fix evidence: `/private/tmp/training-parameters-compact-restored.png`.

No actionable P0, P1, or P2 findings remain.

## Required fidelity surfaces

- Typography: existing product font stack, weights, field-name monospace text, and label hierarchy are retained.
- Spacing/layout: resource cards keep the four-column rhythm; model inputs align consistently; compact parameter density is restored.
- Colors/tokens: existing cyan/info, amber/warning, rose/danger, emerald availability, muted text, and panel tokens are reused.
- Image quality/assets: no bitmap assets were introduced; existing Lucide icons and native form controls remain sharp.
- Copy/content: GPU metrics distinguish utilization from memory occupation, while lease status remains a separate platform fact.

## Primary interactions tested

- Open New Training from Training Tasks.
- Inspect real Worker CPU, memory, disk, and all eight GPU snapshots.
- Select and deselect GPU 0.
- Open Model Registration and edit an existing model family.
- Return to Training Tasks and reopen New Training.
- Inspect compact numeric, enum, boolean, dependency, and grouped parameter states.
- Browser testing surfaced no page-level error state or blocking console error during these flows.

final result: passed

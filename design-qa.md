# Training modal workflow design QA

- Source visual truth: `/private/var/folders/80/bg0v0z7n3jd43rr470zsfjmr0000gn/T/codex-clipboard-31e9aaf3-61db-45d7-8224-4d77fbffb047.png`
- Rendered desktop implementation: `/private/tmp/training-modal-desktop-final.png`
- Rendered mobile implementation: `/private/tmp/training-modal-mobile.png`
- Source pixels: 1666 × 999
- Desktop capture: browser CSS viewport 1280 × 720; screenshot 1265 × 712; device scale factor 1
- Mobile capture: browser CSS viewport 390 × 844; screenshot 375 × 812; device scale factor 1
- State: training-node registration dialog open; no form submission or remote mutation performed

## Comparison scope

The source screenshot documents the prior inline Worker deployment flow. The requested target intentionally changes spatial hierarchy rather than cloning that state pixel-for-pixel: credentials and host-key confirmation move into a modal, while progress and results use a separate compact operation modal. Source and implementation were reviewed together in one comparison input; differences caused by the intentional state change were not treated as fidelity defects.

## Full-view comparison evidence

- The modal creates one clear foreground task and removes the prior split attention between the left progress card and right inline credential form.
- Page geometry remains stable behind the overlay; opening and closing the dialog does not move the training-node cards.
- Desktop dialog width (576 CSS px) supports the two-column server fields without excessive line length.
- Mobile layout collapses to one column and scrolls inside the dialog while retaining the close control and footer actions.

## Focused-region evidence

- Typography: existing console font, weights, labels, and helper-copy hierarchy are preserved; no unexpected wrapping was found at desktop or mobile widths.
- Spacing/layout: field groups use consistent gaps; footer is visually separated and remains reachable at 390 px width.
- Colors/tokens: the implementation reuses existing panel, border, cyan focus, disabled, and muted-text tokens. No new decorative palette was introduced.
- Image quality/assets: this workflow contains no content imagery. Existing Lucide icons remain sharp and consistent.
- Copy/content: the dialog states what will happen, that SSH credentials are temporary, and that host fingerprint confirmation follows registration.
- Accessibility/interaction: semantic dialog title and description are present; initial focus lands on “节点名称”; Escape and the explicit close button close the modal; focus-visible is retained; loading uses `aria-busy`; reduced-motion classes remain supported.

## Findings and comparison history

1. Initial responsive pass — P2: the floating DataPilot launcher could show through the translucent mobile dialog footer and visually overlap its cancel action.
   - Fix: raised shared Dialog/AlertDialog layers above floating controls and suppress the DataPilot launcher while `body[data-scroll-locked]` is active, with a reduced-motion override.
   - Post-fix evidence: `/private/tmp/training-modal-mobile.png`; footer actions are unobstructed and the modal remains scrollable.

No actionable P0, P1, or P2 findings remain. No focused-region issue required another implementation iteration.

## Primary interactions tested

- Open registration dialog from “登记新节点”.
- Initial keyboard focus and visible focus ring.
- Escape closes the dialog and restores access to the page trigger.
- Desktop and mobile responsive layouts.
- Browser console errors checked: none.
- Unit coverage verifies manual close during loading and automatic close after success.

## Residual test gap

The browser pass did not submit SSH credentials or mutate the registered training node. Deployment, removal, error, retry, and success transitions are covered with mocked API integration tests instead.

final result: passed

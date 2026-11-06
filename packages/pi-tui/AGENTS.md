# pi-tui

Vendored fork of `earendil-works/pi` terminal UI primitives. Keep the fork small so upstream synchronization stays manageable.

## Changes

Put product behavior in `apps/coderai/src/tui` through composition, subclassing, or existing host callbacks. Change this package only for a library defect or an extension point the app cannot reach.

Library changes must preserve default behavior, use additive optional arguments or callbacks, and include a regression test that fails without the change. Do not rewrite a rendering strategy for an application feature.

## Validation

From the repository root, run `bun run typecheck` and `bun run test`. Input-buffer and terminal-safety regressions belong in `test/terminal-safety.test.mjs`; app rendering and real-terminal interactions are covered by the other root UI tests. The standalone executable is checked with `bun run test:native`.

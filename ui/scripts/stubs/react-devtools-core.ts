/**
 * Compile-time stub for `react-devtools-core`.
 *
 * Ink only touches this module when `process.env.DEV === 'true'`
 * (see `ink/build/reconciler.js`), but Bun's `--compile` traces every
 * static import in the graph and fails to resolve the real package at
 * runtime because standalone binaries have no `node_modules`.
 *
 * Shipping a no-op shim keeps the DEV-guarded code path valid without
 * pulling `react-devtools-core` (and its `ws` dependency tree) into the
 * binary.
 */

function connectToDevTools(): void {
  // Intentionally empty: end users never run the binary with DEV=true.
}

export default { connectToDevTools };
export { connectToDevTools };

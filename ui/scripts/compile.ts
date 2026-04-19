#!/usr/bin/env bun
/**
 * Compile the Ink UI into a standalone Bun binary.
 *
 * Reads two env vars (both optional):
 *   BUN_TARGET  — one of bun-darwin-arm64, bun-darwin-x64, bun-linux-x64,
 *                  bun-linux-arm64, bun-windows-x64, or just "bun" for host.
 *   PLATFORM    — slug used in the output filename. Inferred from BUN_TARGET
 *                  when omitted; falls back to "host".
 *
 * This is the single compile invocation shared by `make ui-compile` and the
 * GitHub Actions matrix defined in `.github/workflows/release.yml`.
 *
 * Why `Bun.build()` instead of `bun build --compile`?
 *   We need a resolver plugin to replace `react-devtools-core` with a
 *   no-op stub — Ink has a static import of it in `devtools.js`, and
 *   Bun's `--compile` traces the full graph at bundle time. The CLI
 *   doesn't accept plugins, so we use the programmatic API.
 */

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

function hostBunTarget(): string {
  // Bun.build's `compile.target` requires an explicit `bun-<os>-<arch>`
  // slug; plain "bun" is only accepted by the CLI form. Derive the host
  // target so `BUN_TARGET=bun` (our default) still works.
  const arch = process.arch === "arm64" ? "arm64" : "x64";
  if (process.platform === "darwin") return `bun-darwin-${arch}`;
  if (process.platform === "linux") return `bun-linux-${arch}`;
  if (process.platform === "win32") return "bun-windows-x64";
  throw new Error(`Unsupported host platform: ${process.platform}`);
}

const RAW_BUN_TARGET = process.env.BUN_TARGET ?? "bun";
const BUN_TARGET = RAW_BUN_TARGET === "bun" ? hostBunTarget() : RAW_BUN_TARGET;

function platformFromTarget(target: string): string {
  if (target === "bun") return "host";
  return target.startsWith("bun-") ? target.slice(4) : target;
}

const PLATFORM =
  process.env.PLATFORM ??
  (RAW_BUN_TARGET === "bun" ? "host" : platformFromTarget(BUN_TARGET));
const isWindows = PLATFORM.startsWith("windows");
const exeSuffix = isWindows ? ".exe" : "";

const outFile =
  PLATFORM === "host"
    ? `dist/coderai-ui${exeSuffix}`
    : `dist/coderai-ui-${PLATFORM}${exeSuffix}`;

const outAbs = resolve(process.cwd(), outFile);
mkdirSync(dirname(outAbs), { recursive: true });

const devtoolsStub = resolve(
  process.cwd(),
  "scripts/stubs/react-devtools-core.ts",
);

console.log(
  `[compile] Bun.build entrypoint=src/cli.tsx target=${BUN_TARGET} outfile=${outFile}`,
);

const result = await Bun.build({
  entrypoints: ["src/cli.tsx"],
  compile: {
    target: BUN_TARGET,
    outfile: outFile,
  },
  external: ["yoga-wasm-web"],
  plugins: [
    {
      name: "react-devtools-core-stub",
      setup(build) {
        build.onResolve({ filter: /^react-devtools-core$/ }, () => ({
          path: devtoolsStub,
        }));
      },
    },
  ],
});

if (!result.success) {
  console.error("[compile] Bun.build failed:");
  for (const log of result.logs) {
    console.error(log);
  }
  process.exit(1);
}

if (!existsSync(outAbs)) {
  console.error(`[compile] expected output missing: ${outAbs}`);
  process.exit(1);
}

// Ad-hoc code-sign Darwin outputs. Apple Silicon kernels SIGKILL unsigned
// Mach-O binaries that allocate JIT memory (which Bun-compiled binaries do),
// so an ad-hoc signature is mandatory for the binary to run locally.
// Only sign when we're on macOS producing a darwin artifact; cross-builds
// from Linux/Windows leave this to the release pipeline.
const producesDarwinArtifact =
  PLATFORM.startsWith("darwin") ||
  (PLATFORM === "host" && process.platform === "darwin");
if (producesDarwinArtifact && process.platform === "darwin") {
  // Bun's compiled Mach-O sometimes carries a partial/dummy signature
  // that `codesign --sign` refuses with "invalid or unsupported format
  // for signature". Stripping first normalizes the binary.
  spawnSync("codesign", ["--remove-signature", outAbs], {
    stdio: ["ignore", "ignore", "ignore"],
  });

  const signArgs = ["--force", "--sign", "-", "--timestamp=none", outAbs];
  console.log(`[compile] codesign ${signArgs.join(" ")}`);
  const signed = spawnSync("codesign", signArgs, { stdio: "inherit" });
  if (signed.status !== 0) {
    console.error(
      `[compile] codesign exited with status ${signed.status ?? "?"}; ` +
        "the binary may be SIGKILL'd by macOS on launch.",
    );
    process.exit(signed.status ?? 1);
  }
}

console.log(`[compile] wrote ${outFile}`);

# CoderAI benchmark: macOS arm64, 2026-10-29

The saved runs compare the preserved Node SEA with the Bun standalone executable built from the working tree at commit 06eccf464772a4ccd98d47f66d2890e874a2372d. Machine: Apple M3, 8 GiB RAM, macOS 27.2; Node v26.9.0, Bun 1.4.4. Executable hashes, exact sizes, per-run samples and summary statistics are in [the raw results](results/macos-arm64-2026-10-29.json). The worktree was dirty; the commit alone does not identify the tested Bun binary.

## Standalone executable workloads

Numbers are median (10th–90th percentile), in milliseconds. Change is Bun relative to Node, using medians; negative is faster. Wall time includes startup except cancellation, measured from SIGTERM after the shell PID appears to process exit.

| Workload | Node | Bun | Change |
| --- | ---: | ---: | ---: |
| Startup: --help | 299.5 (290.7–443.7) | 217.3 (212.6–305.1) | -27.4% |
| Simple headless turn | 470.9 (448.5–1363.7) | 324.4 (310.3–1216.3) | -31.1% |
| 66 KB prompt | 449.4 (432.8–473.8) | 314.9 (306.6–1253.1) | -29.9% |
| Resume and turn | 462.2 (446.6–469.6) | 316.6 (309.6–333.9) | -31.5% |
| List sessions | 326.9 (323.1–349.0) | 246.0 (240.1–251.0) | -24.7% |
| Export session | 416.1 (393.7–430.5) | 302.4 (287.8–343.5) | -27.3% |
| Fork session | 444.8 (433.4–456.9) | 317.7 (306.9–353.2) | -28.6% |
| Import session | 414.5 (405.1–424.4) | 301.9 (297.7–317.0) | -27.2% |
| Seven local tools | 532.8 (520.8–548.0) | 398.1 (384.4–440.6) | -25.3% |
| Seven tools, 300-file project | 589.6 (584.5–610.1) | 434.0 (425.4–470.1) | -26.4% |
| Image compression, 2200 × 1200 | 701.6 (692.3–726.4) | 533.8 (523.7–568.9) | -23.9% |
| Local MCP tool turn | 461.2 (458.6–476.9) | 344.8 (335.1–360.0) | -25.2% |
| Shell cancellation after SIGTERM | 29.1 (25.6–32.2) | 20.0 (19.8–20.8) | -31.1% |

A few startup/simple-turn samples exceeded one second for both runtimes, so the tails are materially noisier than the medians. Raw results retain all samples, min, max and median absolute deviation. First stdout in the JSON is the first output byte (often a session event), not the first model token.

## Memory and size

Peak RSS is reported by macOS /usr/bin/time for the measured invocation, in KiB; child-process accounting depends on OS semantics and it is not a heap-only measurement. Values are median (10th–90th percentile). Idle is a separate snapshot taken just after a PTY reaches idle, not stabilized long-term memory.

| Workload | Node | Bun | Change |
| --- | ---: | ---: | ---: |
| Startup: --help | 258648.0 (258275.2–259835.2) | 154008.0 (153761.6–154582.4) | -40.5% |
| Simple headless turn | 296528.0 (296065.6–297329.6) | 202448.0 (201572.8–202945.6) | -31.7% |
| Seven local tools | 310304.0 (310052.8–310648.0) | 237944.0 (236593.6–239236.8) | -23.3% |
| Seven tools, 300-file project | 331096.0 (329844.8–331475.2) | 287312.0 (286142.4–290260.8) | -13.2% |
| Image compression, 2200 × 1200 | 410432.0 (410102.4–411238.4) | 317144.0 (311950.4–319686.4) | -22.7% |
| Local MCP tool turn | 306208.0 (305816.0–306544.0) | 216168.0 (215809.6–216976.0) | -29.4% |
| Interactive idle RSS | 288072.0 (287478.4–288726.4) | 189656.0 (189348.8–190451.2) | -34.2% |

Executable size: 162,444,928 bytes (Node) and 121,952,738 bytes (Bun); Bun is 24.9% smaller. CPU user and system times are in the raw results; macOS time reports them at 10 ms resolution and CPU may exceed wall time for multithreaded work.

## Interactive terminal

A fresh PTY and home were used per run, with trust accepted automatically. The turn measures Enter to appearance of the deterministic provider's answer, and checks clean exit and restoration of terminal flags and cursor. 12 measured runs, two warmups, alternating order. Median (10th–90th percentile), ms. [Raw terminal results](results/terminal-macos-arm64-2026-10-29.json).

| Workload | Node | Bun | Change |
| --- | ---: | ---: | ---: |
| Startup through trust to idle | 413.7 (408.6–417.1) | 290.8 (289.7–293.1) | -29.7% |
| Enter to displayed answer | 52.6 (50.9–54.9) | 32.8 (32.2–33.5) | -37.8% |

## MiniDb worker microbenchmark

This is a **separate installed-runtime microbenchmark**, not a standalone-executable measurement. It bundles the same MiniDb source and worker, inserts 10,000 documents, builds a text index and verifies actual search results plus a worker build with no fallback. Eight measured runs, two warmups, alternating order. Median (10th–90th percentile). [Raw worker results](results/worker-macos-arm64-2026-10-29.json).

| Workload | Node | Bun | Change |
| --- | ---: | ---: | ---: |
| Process wall time (ms) | 221.3 (208.1–234.5) | 129.8 (119.0–186.7) | -41.4% |
| Index construction (ms) | 112.2 (107.9–121.0) | 66.5 (62.2–78.4) | -40.7% |
| Peak RSS (KiB) | 177432.0 (175200.0–179435.2) | 122240.0 (121028.8–125540.8) | -31.1% |

## Reproduce

Install project dependencies with the pinned pnpm, build the Node baseline before changing source, preserve it outside build directories, and build Bun with `pnpm build:bun`. On macOS with Python 3, Node, Bun, `rg` and `ps` on PATH, from the repository root:

```sh
BENCH_RUNS=12 BENCH_WARMUPS=2 node benchmarks/intensive.mjs /absolute/path/to/node-coderai /absolute/path/to/bun-coderai benchmarks/results/my-run.json
BENCH_RUNS=8 BENCH_WARMUPS=2 WORKER_DOCS=10000 node benchmarks/worker.mjs benchmarks/results/my-worker.json
BENCH_RUNS=12 BENCH_WARMUPS=2 node benchmarks/terminal.mjs /absolute/path/to/node-coderai /absolute/path/to/bun-coderai benchmarks/results/my-terminal.json
```

The standalone suite alternates order, creates separate fresh homes and equivalent project contents for each iteration, and uses a local deterministic OpenAI-compatible endpoint. It validates successful final events, expected provider responses, tool-result counts and file contents, import/export, the image payload, MCP result, cancellation exit and shell cleanup. It does **not** compare every event, content byte or filesystem side effect between runtimes. The 300-file scenario is synthetic, and the image is a flat-color PNG; neither represents all real projects. Each invocation starts a process; measurements therefore combine runtime startup, application work, I/O and the fixture, rather than isolating Bun internals.

Not covered: Linux/Windows, cold boot or cold filesystem caches, battery/energy, live provider/network throughput, concurrent users, CPU saturation, large sessions, realistic high-entropy images, long-running memory growth, all provider/MCP transports, standalone MiniDb indexing, long-lived worker throughput, and all cancellation modes. These results do not characterize every aspect of either runtime. The baseline and Bun binaries are different builds, so an observed difference cannot by itself be attributed solely to the runtime.

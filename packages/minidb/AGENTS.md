# MiniDb Agent Guide

MiniDb provides CoderAI's embedded JSON persistence and full-text indexing. The standalone upstream server, benchmark runners, and server-specific guides are not part of this checkout.

## Persistence and lifecycle

Preserve snapshot/WAL compatibility, exclusive writer locking, read-only catch-up, and generation integrity checks. Generation publication must remain atomic; missing or invalid derived indexes must retain the recovery path. Worker threads share a host PID, so lock ownership must use the existing token/lifecycle handling.

Keep database opening, WAL replay, index recovery, and replica catch-up cooperative. Retain byte-bounded caches and sliced async operations so indexing cannot monopolize terminal input.

## Index maintenance

`src/text-index/`, `src/text-postings.ts`, and the text registry own tokenization and postings. Keep persisted tokenizer identities and index definitions aligned with their readers. The maintenance scheduler serializes heavy work and owns cancellation, deadlines, and shutdown.

Text builds use `src/worker/text-build-core.ts` through the configured worker runtime. CoderAI builds `dist/text-build-worker.mjs` and configures its absolute path at startup; the native executable extracts its bundled copy. Preserve worker protocol compatibility, bounded builds, integrity checks, and commit/rebase behavior. Do not silently replace a missing packaged worker with a different build path.

## Validation

From the repository root, run `bun run typecheck` and `bun run test`. `test/package.test.mjs` builds a real index through the packaged worker and checks missing-worker diagnostics. `bun run test:native` validates the worker embedded in the standalone executable.

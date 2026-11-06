# klient Agent Guide

CoderAI retains the in-process memory facade over `agent-core-v2`. IPC transport and upstream server/e2e tooling are not shipped in this checkout.

## Boundaries

- `src/core/facade/` and `src/core/klient.ts` expose the public `global`, session, and agent methods and event hubs. Do not expose engine service tokens or add a raw-service escape hatch.
- `src/contract/` owns Zod inputs, outputs, and event payloads. Keep them aligned with the engine and preserve serialized compatibility. Use the contract helpers for null/undefined and void result semantics.
- `src/transports/memory/` implements the `KlientChannel` interface from `src/core/channel.ts`. Preserve JSON round-tripping and scope resolution; facade callers must not depend on transport internals.
- The memory client does not own the engine scope passed by its host. Closing the client must preserve that lifecycle boundary.

## Validation

From the repository root, run `bun run typecheck` and `bun run test`. The root deterministic CLI tests exercise the SDK and memory facade together. No external server, Kimi home directory, pnpm command, or live credentials are required.

# CoderAI Refactoring Roadmap

## Completed (Phase 1)

| Change | Outcome |
|---|---|
| Merged `tui/timeline_append.py` → `tui/timeline_render.py` | ~25 LOC inlined, 1 file removed |
| Deleted `embeddings/base.py` + `factory.py` → `embeddings/openai.py` | 53 LOC removed |
| Moved `ui/display.py` → `cli/utils.py`, deleted `ui/` package | 1 dir removed |
| Default `skills_use_hasna` → `False` (`system/config.py:87`) | 1 line changed |
| Added `category` to 26 tools, deleted `_TOOL_CATEGORY_FALLBACK` from `bridge/tool_metadata.py` | 15 files edited |
| **Verification:** ruff: 0 · mypy: 0 errors · 1686 tests pass | |

## Completed (Phase 2)

| Change | Outcome |
|---|---|
| Merged `ContextManager` → `ContextController` | Pinned-file state + methods moved into `ContextController`. `inject_context` no longer takes a separate `context_manager` param. `agent.context_manager` → `agent.context_controller` everywhere. `context/context.py` reduced to docstring shim. |
| Collapsed `skills/`: merged `skill_registry.py` + `skill_loader.py` into `skill_manager.py` | `Skill`, `SkillRegistry`, loader functions, and `SkillManager` all in one file. `skill_registry.py` + `skill_loader.py` deleted (2 files removed). Sources import from `skill_manager.py`. |
| Added `edits` param to `SearchReplaceTool`, removed `MultiEditTool` | `EditChunk` lives in `filesystem/edit.py`. `SearchReplaceTool.execute()` handles batch edits via `edits: List[EditChunk]`. `multi_edit.py` deleted; use `search_replace` with `edits=` (tool count: 91 = 90 discovered + `manage_context`). |

## Completed (Phase 3)

| Change | Outcome |
|---|---|
| Moved `bridge/streaming.py` → `tui/streaming.py` | `BridgeStreamingHandler` lives in `tui/streaming.py`. `tui/session_setup.py` imports from `.streaming`. `bridge/streaming.py` → shim. |
| Folded `bridge/chat_reference.py` into `bridge/commands.py` | Reference text builders (`_build_models_text`, `_resolve_reference_text`, etc.) inlined into `commands.py` (the only consumer). `chat_reference.py` → shim. |
| Moved `bridge/controller.py` + `commands.py` + `serializers.py` → `tui/` | `UIBridge` now in `tui/controller.py`. Command handlers in `tui/commands.py`. Serializers in `tui/serializers.py`. All 3 bridge files → shims. External consumers (`tui/session_setup.py`, `tui/streaming.py`, `tui/slash.py`, 5 test files) updated. |
| Folded `bridge/tool_metadata.py` → `tools/base.py` + `tui/theme.py` | `tool_risk`, `tool_category`, `tool_risk_factors`, `preview_args_for_approval`, `arg_preview`, `result_preview`, `truncate_args`, `parse_skill_steps` → `tools/base.py`. `strip_rich_markup` → `tui/theme.py`. `tool_metadata.py` → shim. |
| **Verification:** 1679 tests pass (8 pre-existing failures) · bridge/ is now a shim-only package | |

---

## Completed (Phase 4)

| Change | Outcome |
|---|---|
| Extracted `AgentSession` from `Agent` | Session, tracker, token counters, cost_tracker, checkpoints, rewind moved to `core/agent_session.py` (303 LOC). `Agent` methods delegate to `AgentSession` static methods. |
| Extracted `AgentCapabilities` from `Agent` | Tools registry, persona management, skill injection, system prompt, approval rules, hooks manager moved to `core/agent_capabilities.py` (380 LOC). `Agent` methods delegate to `AgentCapabilities` static methods. |
| **Verification:** 10 pre-existing test failures (Phase 2+3 regressions) · 1410 tests pass | |

### 10. Remove `tools/__init__.py` re-export cemetery (deferred to next session)
- **File:** `tools/__init__.py:1-278`
- **Why:** Every tool class is manually re-exported. `discovery.py` already auto-discovers and registers tools. The explicit re-exports are redundant and drift.
- **Fix:** Delete all explicit tool imports except `Tool` and `ToolRegistry`. Add deprecation shim that warns for 1 release.
- **Effort:** S
- **Rollback/test:** Only 3 files import `ToolRegistry` from `coderAI.tools` (`agent.py`, `test_integration.py`, `test_coderAI.py`). Submodule-style imports (`from coderAI.tools import web`) are unaffected.

---

## Simplification Totals

| File/Module | Action | ~LOC removed |
|---|---|---|
| `bridge/controller.py` + `commands.py` + `serializers.py` | **DONE (P3)** — Folded into `tui/` | ~1,750 |
| `bridge/streaming.py` | **DONE (P3)** — Moved to `tui/` | ~270 |
| `bridge/chat_reference.py` | **DONE (P3)** — Inlined into `commands.py` | ~240 |
| `bridge/tool_metadata.py` | **DONE (P3)** — Moved to `tools/base.py` + `tui/theme.py` | ~250 |
| `ui/display.py` + `ui/__init__.py` | **DONE (P1)** | ~90 |
| `timeline_append.py` | **DONE (P1)** | ~25 |
| `embeddings/base.py` + `factory.py` | **DONE (P1)** | ~80 |
| `multi_edit.py` | **DONE (P2)** — stub removed; batch via `search_replace` | ~140 |
| `skills/` (consolidate 7→3) | **DONE (P2)** | ~300 |
| `context/` (merge Manager→Controller) | **DONE (P2)** | ~150 |
| `core/agent.py` (extract AgentSession + AgentCapabilities) | **DONE (P4)** — Agent: 1158→487 LOC | ~671 |
| `tools/__init__.py` | Delete re-exports (deferred) | ~250 |
| **Total** | | **~4,226 LOC** |

---

## Security Notes

- Shell command security (`terminal.py`) has multiple defense layers: string regex blocking, argv-level binary blocking, pipe-to-shell detection, env scrubbing, and confirmation gates. The string regex layer (`_BLOCKED_REGEXES`) is partially redundant with the argv-level checks — candidate for Phase 5 removal.
- `hasna_source.py` depends on a third-party `skills` CLI not distributed with CoderAI. Default changed to `False` in Phase 1; if the feature shows no usage after 2 releases, delete the file entirely.

import React from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";
import type {ToolCategory, ToolRisk} from "../protocol.js";
import {truncateText} from "../lib/format.js";
import {Rail, RiskBadge} from "./Primitives.js";

export interface ToolCardProps {
  name: string;
  category: ToolCategory;
  args: Record<string, unknown>;
  risk: ToolRisk;
  ok: boolean | null;
  preview: string | null;
  error: string | null;
  fullAvailable?: boolean;
}

/**
 * Tool execution card — rail-based.
 *
 * The redesign collapses successful runs with short previews to a
 * single line so a long transcript doesn't drown in bordered boxes:
 *
 *   ▌ ✓ read_file  ui/src/App.tsx            · fs
 *
 * Multi-line previews, errors, and in-flight runs expand:
 *
 *   ▌ ⚙ run_command  $ pytest -q             · shell
 *   ▌   ⠋ running…
 *
 *   ▌ ✗ write_file  config.yml               · fs
 *   ▌   permission denied
 */
export function ToolCard(props: ToolCardProps) {
  const color = theme.tool[props.category] ?? theme.tool.other;
  const status = props.ok === null ? "running" : props.ok ? "ok" : "error";
  const summary = summarizeArgs(props.name, props.args);
  const railColor = status === "error" ? theme.danger : color;
  const icon =
    status === "ok"
      ? theme.glyph.tick
      : status === "error"
        ? theme.glyph.cross
        : theme.glyph.cog;
  const iconColor =
    status === "ok"
      ? theme.success
      : status === "error"
        ? theme.danger
        : theme.warning;

  const previewLines = (props.preview ?? "").split("\n");
  const previewIsShort =
    props.preview !== null &&
    previewLines.length === 1 &&
    (props.preview ?? "").length <= 60 &&
    !props.fullAvailable;

  // Compact single-line form when the run succeeded and the preview
  // is either empty or trivially short.  This is the common case.
  const compact =
    status === "ok" && (props.preview === null || previewIsShort);

  return (
    <Rail color={railColor} gap={2} marginBottom={1}>
      <Box justifyContent="space-between">
        <Box>
          <Text color={iconColor} bold>
            {icon}
          </Text>
          <Text color={railColor} bold>
            {" "}
            {props.name}
          </Text>
          {summary ? (
            <Text color={theme.muted}>
              {"  "}
              {summary}
            </Text>
          ) : null}
          {compact && previewIsShort && props.preview ? (
            <Text color={theme.faint}>
              {"  "}
              {theme.glyph.arrowRun} {props.preview}
            </Text>
          ) : null}
        </Box>
        <Box>
          <RiskBadge risk={props.risk} />
          <Text color={theme.faint}>
            {"  "}
            {theme.glyph.dot} {props.category}
          </Text>
        </Box>
      </Box>

      {/* Running state */}
      {status === "running" ? (
        <Box marginTop={0}>
          <Text color={theme.accent}>
            <Spinner type="dots" />
          </Text>
          <Text color={theme.muted}> running…</Text>
        </Box>
      ) : null}

      {/* Multi-line or long preview */}
      {status === "ok" && !compact && props.preview ? (
        <Box marginTop={0} flexDirection="column">
          <Text color={theme.muted}>{props.preview}</Text>
          {props.fullAvailable ? (
            <Text color={theme.faint} italic>
              {theme.glyph.dot} output truncated — full result in session log
            </Text>
          ) : null}
        </Box>
      ) : null}

      {/* Error */}
      {status === "error" ? (
        <Box marginTop={0}>
          <Text color={theme.danger}>{props.error ?? "tool failed"}</Text>
        </Box>
      ) : null}
    </Rail>
  );
}

/**
 * Per-tool smart argument summary.  Picks the single most useful arg.
 */
function summarizeArgs(name: string, args: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) return "";
  const get = (k: string) => (args[k] ? String(args[k]) : null);

  switch (name) {
    case "read_file":
    case "write_file":
    case "apply_diff":
    case "search_replace":
      return get("path") || get("file_path") || "";
    case "glob_search":
      return get("pattern") || "";
    case "list_directory":
      return get("path") || ".";
    case "grep":
    case "text_search":
      return `"${get("pattern") || get("query") || ""}"`;
    case "run_command":
    case "run_background":
      return `$ ${truncateText(get("command") || "", 80)}`;
    case "web_search":
      return `"${get("query") || ""}"`;
    case "read_url":
    case "download_file":
      return get("url") || "";
    case "delegate_task":
      return `${get("agent_role") || get("persona") || "agent"}: ${truncateText(get("task_description") || get("task") || "", 60)}`;
    default: {
      const entries = Object.entries(args)
        .slice(0, 2)
        .map(([k, v]) => `${k}=${truncateText(String(v), 40)}`);
      return entries.join(" ");
    }
  }
}

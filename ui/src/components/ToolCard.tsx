import React from "react";
import {Box, Text, useStdout} from "ink";
import {theme} from "../theme.js";
import type {ToolCategory, ToolRisk} from "../protocol.js";
import {truncateSmart} from "../lib/format.js";
import {RiskBadge} from "./Primitives.js";
import {QuietSpinner} from "./QuietSpinner.js";

export interface ToolCardProps {
  name: string;
  category: ToolCategory;
  args: Record<string, unknown>;
  risk: ToolRisk;
  ok: boolean | null;
  preview: string | null;
  error: string | null;
  fullAvailable?: boolean;
  /** When true, expand multi-line previews and always show category. */
  verbose?: boolean;
}

/**
 * Tool execution — single-line by default.
 *
 *   ✓ read_file  ui/src/App.tsx                       12 lines
 *   ⚙ run_command  $ pytest -q                        running…
 *   ✗ write_file  config.yml                          permission denied
 *
 * Verbose mode reinstates rail chrome, full multi-line previews, and the
 * category label on the right. Risk badge appears only for med/high.
 */
export function ToolCard(props: ToolCardProps) {
  const {stdout} = useStdout();
  const columns = stdout?.columns ?? 100;
  const narrow = columns < theme.layout.narrowCols;

  const status = props.ok === null ? "running" : props.ok ? "ok" : "error";
  // run_command/run_background's `command` field is the highest-signal arg —
  // truncating it at 56 chars hides the dangerous tail of long pipelines.
  // Give it more room when it's the focus arg.
  const wideArg = props.name === "run_command" || props.name === "run_background";
  const summary = summarizeArgs(
    props.name,
    props.args,
    wideArg ? (narrow ? 60 : 100) : (narrow ? 32 : 56),
  );
  const categoryColor = theme.tool[props.category] ?? theme.tool.other;
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
  const nameColor = status === "error" ? theme.danger : theme.text;

  const previewOneLine = oneLinePreview(
    status,
    props.preview,
    props.error,
    narrow ? 36 : 64,
  );
  const showRisk = props.risk !== "low";

  // Single-line render unless verbose mode wants more chrome.
  if (!props.verbose) {
    return (
      <Box paddingX={2}>
        <Box flexGrow={1}>
          {/* Category bullet — restores the at-a-glance category cue we
              lost when the rail came off in non-verbose mode. One cell wide
              so it doesn't push the icon column. */}
          <Text color={categoryColor}>{theme.glyph.bullet} </Text>
          {status === "running" ? (
            <Text color={iconColor}>
              <QuietSpinner staticGlyph={theme.glyph.cog} />
            </Text>
          ) : (
            <Text color={iconColor} bold>
              {icon}
            </Text>
          )}
          <Text color={nameColor} bold>
            {" "}
            {props.name}
          </Text>
          {summary ? (
            <Text color={theme.muted}>{"  "}{summary}</Text>
          ) : null}
        </Box>
        <Box>
          {showRisk && !narrow ? (
            <Box marginRight={1}>
              <RiskBadge risk={props.risk} />
            </Box>
          ) : null}
          {previewOneLine ? (
            <Text color={status === "error" ? theme.danger : theme.faint}>
              {previewOneLine}
            </Text>
          ) : null}
        </Box>
      </Box>
    );
  }

  // Verbose: expanded card with multi-line preview.
  const previewLines = (props.preview ?? "").split("\n");
  return (
    <Box flexDirection="column" paddingX={2} marginBottom={1}>
      <Box>
        {status === "running" ? (
          <Text color={iconColor}>
            <QuietSpinner staticGlyph={theme.glyph.cog} />
          </Text>
        ) : (
          <Text color={iconColor} bold>
            {icon}
          </Text>
        )}
        <Text color={nameColor} bold>
          {" "}
          {props.name}
        </Text>
        {summary ? (
          <Text color={theme.muted}>{"  "}{summary}</Text>
        ) : null}
        {showRisk ? (
          <Text>
            {"  "}
            <RiskBadge risk={props.risk} />
          </Text>
        ) : null}
        <Text color={theme.faint}>
          {"  "}
          {theme.glyph.dot} {props.category}
        </Text>
      </Box>
      {status === "ok" && props.preview ? (
        <Box marginTop={0} flexDirection="column" paddingLeft={4}>
          {previewLines.map((ln, i) => (
            <Text key={i} color={theme.muted}>
              {ln}
            </Text>
          ))}
          {props.fullAvailable ? (
            <Text color={theme.faint} italic>
              + more lines hidden — /verbose to expand
            </Text>
          ) : null}
        </Box>
      ) : null}
      {status === "error" ? (
        <Box paddingLeft={4}>
          <Text color={theme.danger}>{props.error ?? "tool failed"}</Text>
        </Box>
      ) : null}
    </Box>
  );
}

function oneLinePreview(
  status: "running" | "ok" | "error",
  preview: string | null,
  error: string | null,
  maxLen: number,
): string {
  if (status === "running") return "";
  if (status === "error") return truncateSmart((error ?? "failed").split("\n")[0], maxLen);
  if (!preview) return "";
  const first = preview.split("\n")[0];
  return truncateSmart(first, maxLen);
}

/**
 * Per-tool smart argument summary.  Picks the single most useful arg.
 */
function summarizeArgs(
  name: string,
  args: Record<string, unknown>,
  maxLen: number = 80,
): string {
  if (!args || Object.keys(args).length === 0) return "";
  const get = (k: string) => (args[k] ? String(args[k]) : null);
  const trunc = (s: string) => truncateSmart(s, maxLen);

  switch (name) {
    case "read_file":
    case "write_file":
    case "apply_diff":
    case "search_replace":
      return trunc(get("path") || get("file_path") || "");
    case "glob_search":
      return trunc(get("pattern") || "");
    case "list_directory":
      return trunc(get("path") || ".");
    case "grep":
    case "text_search":
      return `"${trunc(get("pattern") || get("query") || "")}"`;
    case "run_command":
    case "run_background":
      return `$ ${trunc(get("command") || "")}`;
    case "web_search":
      return `"${trunc(get("query") || "")}"`;
    case "read_url":
    case "download_file":
      return trunc(get("url") || "");
    case "delegate_task":
      return `${get("agent_role") || get("persona") || "agent"}: ${trunc(get("task_description") || get("task") || "")}`;
    default: {
      const entries = Object.entries(args)
        .slice(0, 2)
        .map(([k, v]) => `${k}=${truncateSmart(String(v), Math.min(40, maxLen))}`);
      return entries.join(" ");
    }
  }
}

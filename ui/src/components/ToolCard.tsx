import React from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";
import type {ToolCategory, ToolRisk} from "../protocol.js";

export interface ToolCardProps {
  name: string;
  category: ToolCategory;
  args: Record<string, unknown>;
  risk: ToolRisk;
  ok: boolean | null;
  preview: string | null;
  error: string | null;
  /**
   * Hints whether the Python side has more output than fit in `preview`.
   */
  fullAvailable?: boolean;
}

export function ToolCard(props: ToolCardProps) {
  const color = theme.tool[props.category] ?? theme.tool.other;
  const status = props.ok === null ? "running" : props.ok ? "ok" : "error";
  const summary = summarizeArgs(props.name, props.args);
  const gutterColor = status === "error" ? theme.danger : color;

  return (
    <Box paddingLeft={1}>
      <Text color={gutterColor}>│ </Text>
      <Box flexDirection="column">
        <Box>
          <Text color={gutterColor} bold>
            {iconFor(status)} {props.name}
          </Text>
          {props.ok === null ? (
            <Text color={theme.accent}>
              {" "}
              <Spinner type="dots" />
            </Text>
          ) : null}
          <Text color={theme.muted}>  {props.category}  </Text>
          <RiskBadge risk={props.risk} />
        </Box>
        {summary ? <Text color={theme.muted}>{summary}</Text> : null}
        {props.ok === true && props.preview ? (
          <Box marginTop={1} paddingLeft={1}>
            <Text color={theme.muted}>{props.preview}</Text>
          </Box>
        ) : null}
        {props.fullAvailable ? (
          <Text color={theme.muted} italic>
            (output truncated — full result kept in session log)
          </Text>
        ) : null}
        {props.ok === false ? (
          <Text color={theme.danger}>✗ {props.error ?? "tool failed"}</Text>
        ) : null}
      </Box>
    </Box>
  );
}

function iconFor(status: "running" | "ok" | "error"): string {
  return status === "ok" ? "✓" : status === "error" ? "✗" : "⚙";
}

function RiskBadge({risk}: {risk: ToolRisk}) {
  const color = theme.risk[risk];
  const label = risk === "high" ? " HIGH " : risk === "medium" ? " MED " : " LOW ";
  return (
    <Text backgroundColor={color} color="black" bold>
      {label}
    </Text>
  );
}

/**
 * Per-tool smart argument summary. Picks the single most useful arg.
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
      return `$ ${truncate(get("command") || "", 80)}`;
    case "web_search":
      return `"${get("query") || ""}"`;
    case "read_url":
    case "download_file":
      return get("url") || "";
    case "delegate_task":
      return `${get("persona") || "agent"}: ${truncate(get("task") || "", 60)}`;
    default: {
      const entries = Object.entries(args)
        .slice(0, 2)
        .map(([k, v]) => `${k}=${truncate(String(v), 40)}`);
      return entries.join(" ");
    }
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + "…" : s;
}

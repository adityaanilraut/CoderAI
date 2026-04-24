import React from "react";
import {Box, Text, useInput} from "ink";
import {theme} from "../theme.js";
import type {ToolRisk} from "../protocol.js";
import {truncateText} from "../lib/format.js";
import {ActionPill, RiskBadge} from "./Primitives.js";

export interface ApprovalPromptProps {
  tool: string;
  args: Record<string, unknown>;
  risk: ToolRisk;
  decided: "pending" | "approved" | "denied";
  active: boolean;
  onDecide: (approve: boolean) => void;
}

/**
 * Inline y/n dialog for high-risk tool calls.
 *
 * This is the ONE component that keeps a loud double border in the
 * redesign — the rest of the UI was de-chromed specifically so this
 * dialog pops when it matters.
 *
 *   ╔═══════════════════════════════════════════════╗
 *   ║  ⚠ APPROVAL REQUIRED                ⚠ high    ║
 *   ║                                               ║
 *   ║  run_command                                  ║
 *   ║  command=rm -rf build/                        ║
 *   ║                                               ║
 *   ║  [ Accept y ]   [ Deny n ]   ↵ confirm        ║
 *   ╚═══════════════════════════════════════════════╝
 */
export function ApprovalPrompt({
  tool,
  args,
  risk,
  decided,
  active,
  onDecide,
}: ApprovalPromptProps) {
  const [focus, setFocus] = React.useState(0);

  useInput(
    (input, key) => {
      if (input === "y" || input === "Y") onDecide(true);
      else if (input === "n" || input === "N") onDecide(false);
      else if (key.leftArrow || key.rightArrow || key.tab) {
        setFocus((f) => (f === 0 ? 1 : 0));
      } else if (key.return) {
        onDecide(focus === 0);
      }
    },
    {isActive: active && decided === "pending"},
  );

  // Never truncate the "primary" arg — a long rm -rf path should never be
  // hidden below the fold. Other args are still clipped to keep the dialog
  // compact.
  const PRIMARY_KEYS = ["command", "file_path", "path", "url", "source", "destination"];
  const primaryKey = PRIMARY_KEYS.find((k) => k in args);
  const primaryValue = primaryKey ? String(args[primaryKey] ?? "") : "";
  const secondary = Object.entries(args)
    .filter(([k]) => k !== primaryKey)
    .slice(0, 3)
    .map(([k, v]) => `${k}=${truncateText(String(v), 60)}`)
    .join("  ");

  const borderColor =
    decided === "approved"
      ? theme.success
      : decided === "denied"
        ? theme.danger
        : theme.warning;

  return (
    <Box
      flexDirection="column"
      borderStyle="double"
      borderColor={borderColor}
      paddingX={2}
      paddingY={0}
      marginBottom={1}
      marginTop={1}
    >
      {/* Header — loud amber bar */}
      <Box justifyContent="space-between">
        <Text color={theme.warning} bold>
          {theme.glyph.warn} APPROVAL REQUIRED
        </Text>
        <RiskBadge risk={risk} />
      </Box>

      {/* Tool + args */}
      <Box marginTop={1} flexDirection="column">
        <Text bold color={theme.text}>
          {tool}
        </Text>
        {primaryKey ? (
          <Text color={theme.text}>
            <Text color={theme.muted}>{primaryKey}=</Text>
            {primaryValue}
          </Text>
        ) : null}
        {secondary ? <Text color={theme.muted}>{secondary}</Text> : null}
      </Box>

      {/* Decision row */}
      {decided === "pending" ? (
        <Box marginTop={1}>
          <ActionPill
            label="Accept [y]"
            selected={focus === 0}
            color={theme.success}
          />
          <Text>   </Text>
          <ActionPill
            label="Deny [n]"
            selected={focus === 1}
            color={theme.danger}
          />
          <Box marginLeft={3}>
            <Text color={theme.faint}>
              ←→ navigate
              {theme.glyph.separator}↵ confirm
            </Text>
          </Box>
        </Box>
      ) : (
        <Box marginTop={1}>
          <Text
            color={decided === "approved" ? theme.success : theme.danger}
            bold
          >
            {decided === "approved"
              ? `${theme.glyph.tick} approved — executing…`
              : `${theme.glyph.cross} denied — skipped`}
          </Text>
        </Box>
      )}
    </Box>
  );
}

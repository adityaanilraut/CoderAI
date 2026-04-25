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
  onDecide: (approve: boolean, always?: boolean) => void;
}

/**
 * Inline y/n dialog for high-risk tool calls.
 *
 * This is the ONE component that keeps a loud double border in the
 * redesign — the rest of the UI was de-chromed specifically so this
 * dialog pops when it matters. Wording is sentence-case; the border
 * carries the urgency, not the typography.
 *
 *   ╔═══════════════════════════════════════════════╗
 *   ║  Run? rm -rf build/                  high     ║
 *   ║                                               ║
 *   ║  [ Allow y ]   [ Deny n ]   ↵ confirm         ║
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
      if (input === "y" || input === "Y") onDecide(true, false);
      else if (input === "a" || input === "A") onDecide(true, true);
      else if (input === "n" || input === "N") onDecide(false, false);
      else if (key.leftArrow) {
        setFocus((f) => (f > 0 ? f - 1 : 2));
      } else if (key.rightArrow || key.tab) {
        setFocus((f) => (f < 2 ? f + 1 : 0));
      } else if (key.return) {
        if (focus === 0) onDecide(true, false);
        else if (focus === 1) onDecide(true, true);
        else onDecide(false, false);
      }
    },
    {isActive: active && decided === "pending"},
  );

  // Show the primary arg in full but hard-wrap it at a fixed width so a
  // pathological value (a long `bash -lc 'curl ... | sh'`) can't blow the
  // dialog open or wrap mid-token in a way that hides the dangerous part.
  const PRIMARY_KEYS = ["command", "file_path", "path", "url", "source", "destination"];
  const primaryKey = PRIMARY_KEYS.find((k) => k in args);
  const PRIMARY_HARD_CAP = 240;
  const rawPrimary = primaryKey ? String(args[primaryKey] ?? "") : "";
  const primaryValue =
    rawPrimary.length > PRIMARY_HARD_CAP
      ? rawPrimary.slice(0, PRIMARY_HARD_CAP - 1) + "…"
      : rawPrimary;
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
      {/* Header — sentence-case label, risk on the right. The border is the loud part. */}
      <Box justifyContent="space-between">
        <Text color={theme.warning} bold>
          {theme.glyph.warn} Run?
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
        <Box flexDirection="column" marginTop={1}>
          <Box>
            <ActionPill
              label="Allow once [y]"
              selected={focus === 0}
              color={theme.success}
            />
            <Text>   </Text>
            <ActionPill
              label="Enable YOLO mode [a]"
              selected={focus === 1}
              color={theme.warning}
            />
            <Text>   </Text>
            <ActionPill
              label="Deny [n]"
              selected={focus === 2}
              color={theme.danger}
            />
            <Box marginLeft={3}>
              <Text color={theme.faint}>
                ←→ navigate
                {theme.glyph.separator}↵ confirm
              </Text>
            </Box>
          </Box>
          {focus === 1 ? (
            <Box marginTop={1}>
              <Text color={theme.warning}>
                {theme.glyph.warn} YOLO auto-approves <Text bold>all</Text> tools, not just this one.
              </Text>
            </Box>
          ) : null}
        </Box>
      ) : (
        <Box marginTop={1}>
          <Text
            color={decided === "approved" ? theme.success : theme.danger}
            bold
          >
            {decided === "approved"
              ? `${theme.glyph.tick} allowed`
              : `${theme.glyph.cross} denied`}
          </Text>
        </Box>
      )}
    </Box>
  );
}

import React from "react";
import {Box, Text, useInput} from "ink";
import {theme} from "../theme.js";
import type {ToolRisk} from "../protocol.js";

export interface ApprovalPromptProps {
  tool: string;
  args: Record<string, unknown>;
  risk: ToolRisk;
  decided: "pending" | "approved" | "denied";
  active: boolean;
  onDecide: (approve: boolean) => void;
}

/**
 * Inline y/n dialog for high-risk tool calls. Python's IPC server blocks on
 * `_approval_waiters` until a matching `tool_approval_resp` arrives, so this
 * component is the only way the user can unblock a pending high-risk tool.
 */
export function ApprovalPrompt({
  tool,
  args,
  risk,
  decided,
  active,
  onDecide,
}: ApprovalPromptProps) {
  useInput(
    (input) => {
      if (input === "y" || input === "Y") onDecide(true);
      else if (input === "n" || input === "N") onDecide(false);
    },
    {isActive: active && decided === "pending"},
  );

  const borderColor =
    decided === "approved"
      ? theme.success
      : decided === "denied"
        ? theme.danger
        : theme.warning;

  const summary = Object.entries(args)
    .slice(0, 3)
    .map(([k, v]) => `${k}=${truncate(String(v), 60)}`)
    .join(" ");

  return (
    <Box
      borderStyle="double"
      borderColor={borderColor}
      paddingX={1}
      flexDirection="column"
      marginBottom={1}
    >
      <Box>
        <Text color={theme.warning} bold>
          ⚠ Approval required
        </Text>
        <Text color={theme.muted}>  ·  risk: </Text>
        <Text color={theme.risk[risk]} bold>
          {risk}
        </Text>
      </Box>
      <Box marginTop={0}>
        <Text>
          <Text bold>{tool}</Text>
          {summary ? (
            <Text color={theme.muted}>  {summary}</Text>
          ) : null}
        </Text>
      </Box>
      {decided === "pending" ? (
        <Box marginTop={0}>
          <Text color={theme.accent}>
            Press <Text bold>y</Text> to approve ·{" "}
            <Text bold>n</Text> to deny
          </Text>
        </Box>
      ) : (
        <Box marginTop={0}>
          <Text color={decided === "approved" ? theme.success : theme.danger}>
            {decided === "approved" ? "✓ approved" : "✗ denied"}
          </Text>
        </Box>
      )}
    </Box>
  );
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

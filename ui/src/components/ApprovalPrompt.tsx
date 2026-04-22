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
  // 0 = Approve, 1 = Deny
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
      <Box justifyContent="space-between">
        <Text color={theme.warning} bold>
          ⚠ Approval required
        </Text>
        <Text backgroundColor={theme.risk[risk]} color="black" bold>
          {" " + risk.toUpperCase() + " "}
        </Text>
      </Box>
      <Box marginTop={1} flexDirection="column">
        <Text bold color={theme.text}>{tool}</Text>
        {summary ? (
          <Text color={theme.muted}>{summary}</Text>
        ) : null}
      </Box>

      {decided === "pending" ? (
        <Box marginTop={1}>
          {/* Approve Button */}
          <Box paddingX={1}>
            <Text
              backgroundColor={focus === 0 ? theme.success : undefined}
              color={focus === 0 ? "black" : theme.success}
              bold={focus === 0}
            >
              {" Accept [y] "}
            </Text>
          </Box>

          <Box marginX={2}>
            <Text color={theme.muted}>|</Text>
          </Box>

          {/* Deny Button */}
          <Box paddingX={1}>
            <Text
              backgroundColor={focus === 1 ? theme.danger : undefined}
              color={focus === 1 ? "black" : theme.danger}
              bold={focus === 1}
            >
              {" Deny [n] "}
            </Text>
          </Box>

          <Box marginX={2}>
            <Text color={theme.muted}>
              use arrows/tab to navigate, enter to confirm
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
              ? "✓ Approved and executing..." 
              : "✗ Denied and skipped"}
          </Text>
        </Box>
      )}
    </Box>
  );
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

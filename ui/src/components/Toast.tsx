import React from "react";
import {Box, Text} from "ink";
import {theme} from "../theme.js";
import {Rail} from "./Primitives.js";

export interface ToastProps {
  level: "info" | "warning" | "success";
  message: string;
}

/**
 * Ephemeral system notice — left-rail only, no panel chrome.
 *
 *   ▌ ℹ model changed to claude-sonnet-4-6
 *   ▌ ✓ context compacted (−42%)
 *   ▌ ⚠ YOLO mode enabled
 */
export function Toast({level, message}: ToastProps) {
  const color =
    level === "success"
      ? theme.success
      : level === "warning"
        ? theme.warning
        : theme.info;
  const icon =
    level === "success"
      ? theme.glyph.tick
      : level === "warning"
        ? theme.glyph.warn
        : "ℹ";
  const lines = message.split("\n");

  return (
    <Rail color={color} gap={2} marginBottom={1}>
      {lines.map((line, i) => (
        <Box key={i}>
          {i === 0 ? (
            <Text color={color} bold>
              {icon}
              {"  "}
            </Text>
          ) : (
            <Text>{"   "}</Text>
          )}
          <Text color={color}>{line}</Text>
        </Box>
      ))}
    </Rail>
  );
}

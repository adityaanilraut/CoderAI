import React from "react";
import {Box, Text} from "ink";
import {theme} from "../theme.js";

export function Toast({
  level,
  message,
}: {
  level: "info" | "warning" | "success";
  message: string;
}) {
  const color =
    level === "success"
      ? theme.success
      : level === "warning"
        ? theme.warning
        : theme.info;
  const icon =
    level === "success" ? "✓" : level === "warning" ? "⚠" : "ℹ";
  const lines = message.split("\n");
  return (
    <Box marginBottom={0} flexDirection="column">
      {lines.map((line, i) => (
        <Text key={i} color={color}>
          {i === 0 ? `${icon} ` : "   "}
          {line}
        </Text>
      ))}
    </Box>
  );
}

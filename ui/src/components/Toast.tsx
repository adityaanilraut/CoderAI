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
  return (
    <Box marginBottom={0}>
      <Text color={color}>
        {icon} {message}
      </Text>
    </Box>
  );
}

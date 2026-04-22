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
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={color}
      paddingX={1}
      marginBottom={1}
    >
      {lines.map((line, i) => (
        <Text key={i} color={color}>
          {i === 0 ? `${icon} ` : "   "}
          {line}
        </Text>
      ))}
    </Box>
  );
}

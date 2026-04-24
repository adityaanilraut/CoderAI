import React from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";
import type {AgentInfo, AgentStatus} from "../protocol.js";
import {formatTokenCount, truncateText} from "../lib/format.js";
import {Rail, MessageHeader} from "./Primitives.js";

const FINISHED: AgentStatus[] = ["done", "error", "cancelled"];

/**
 * Inline card for a sub-agent — rail-based.
 *
 *   ▌ ⚙ code-reviewer  (reviewer)              3.4s
 *   ▌ 4.2k tok · $0.014 · read_file
 *   ▌ review the auth changes for potential regressions
 */
export interface AgentCardProps {
  agent: AgentInfo;
}

export function AgentCard({agent}: AgentCardProps) {
  const finished = FINISHED.includes(agent.status);
  const railColor = railColorFor(agent.status);
  const fmt = (n: number) => formatTokenCount(n);

  return (
    <Rail color={railColor} gap={2} marginBottom={1}>
      <MessageHeader
        label={renderLabel(agent)}
        labelColor={finished ? theme.muted : theme.text}
        annotation={agent.role ? `(${agent.role})` : undefined}
        right={
          <Text color={theme.faint}>
            {(agent.elapsedMs / 1000).toFixed(1)}s
          </Text>
        }
      />
      <Box>
        <Text color={theme.muted}>
          {fmt(agent.tokens)} tok{"  "}
          {theme.glyph.dot} ${agent.costUsd.toFixed(4)}
          {!finished && agent.tool ? (
            <Text color={theme.warning}>
              {"  "}
              {theme.glyph.dot} {agent.tool}
            </Text>
          ) : null}
        </Text>
      </Box>
      {agent.task ? (
        <Box marginTop={1}>
          <Text color={theme.faint} italic>
            {truncateText(agent.task, 160)}
          </Text>
        </Box>
      ) : null}
    </Rail>
  );
}

function renderLabel(agent: AgentInfo): string {
  const glyph = glyphFor(agent.status);
  return `${glyph} ${agent.name}`;
}

function glyphFor(status: AgentStatus): string {
  switch (status) {
    case "thinking":
      return theme.glyph.pulse;
    case "tool_call":
      return theme.glyph.cog;
    case "waiting_for_user":
      return theme.glyph.wait;
    case "done":
      return theme.glyph.tick;
    case "error":
      return theme.glyph.cross;
    case "cancelled":
      return "⊘";
    default:
      return theme.glyph.dot;
  }
}

function railColorFor(status: AgentStatus): string {
  switch (status) {
    case "thinking":
    case "tool_call":
      return theme.role.assistant;
    case "waiting_for_user":
      return theme.info;
    case "done":
      return theme.success;
    case "error":
      return theme.danger;
    case "cancelled":
      return theme.muted;
    default:
      return theme.muted;
  }
}

/**
 * Legacy export kept for any outside callsite that still imports it.
 * The new `AgentCard` composes its own status glyph inline so this is
 * no longer used internally.
 */
export interface StatusBadgeProps {
  status: AgentStatus;
}

export function StatusBadge({status}: StatusBadgeProps) {
  switch (status) {
    case "thinking":
      return (
        <Text color={theme.accent}>
          <Spinner type="dots" />
        </Text>
      );
    case "tool_call":
      return <Text color={theme.warning}>{theme.glyph.cog}</Text>;
    case "waiting_for_user":
      return <Text color={theme.info}>{theme.glyph.wait}</Text>;
    case "done":
      return <Text color={theme.success}>{theme.glyph.tick}</Text>;
    case "error":
      return <Text color={theme.danger}>{theme.glyph.cross}</Text>;
    case "cancelled":
      return <Text color={theme.muted}>⊘</Text>;
    default:
      return <Text color={theme.muted}>{theme.glyph.dot}</Text>;
  }
}

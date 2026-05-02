import React, {useEffect, useState} from "react";
import {Box, Text} from "ink";
import {theme} from "../theme.js";
import type {AgentInfo, AgentStatus} from "../protocol.js";
import {formatTokenCount, formatCost, truncateSmart} from "../lib/format.js";

const FINISHED: AgentStatus[] = ["done", "error", "cancelled"];

/**
 * Sticky panel above the prompt that renders the live agent activity as a
 * tree using `parentId`.  The scrolling transcript is "what happened";
 * this panel is "what's running right now".
 *
 *   ─ Agents ─────────────────────────────────────────────────────────
 *     ◆ main · opus-4.7              42k tok   $0.18    14s
 *     ├─ ⚙ code-reviewer · sonnet    8.2k     $0.03    grep "useState"
 *     └─ ✓ test-runner · haiku       1.4k     $0.00    done
 */
export interface AgentTreeProps {
  agents: Record<string, AgentInfo>;
  /** Hide finished children that have been done for longer than this. */
  finishedGraceMs?: number;
  /**
   * Map of agent id → ms since epoch when it most recently flipped to a
   * terminal status. Used together with `finishedGraceMs` to fade out
   * completed children. The root agent never auto-collapses.
   */
  finishedAt?: Record<string, number>;
  /**
   * Total width available — used to clamp the rule line. Falls back to a
   * sensible default when not provided.
   */
  width?: number;
}

interface TreeNode {
  agent: AgentInfo;
  depth: number;
  isLast: boolean;
}

export function AgentTree({
  agents,
  finishedGraceMs = 5000,
  finishedAt = {},
  width = 100,
}: AgentTreeProps) {
  // Drive grace-window filtering off a 1Hz tick rather than recomputing on
  // every parent render. Without this, the panel rebuilt its visibility set
  // on every status patch / stream delta — finished children could flicker
  // in or out at frame boundaries depending on whether a re-render happened
  // to land on the wrong side of `now - flippedAt > graceMs`. The tick
  // pauses when nothing is finished, so idle sessions stay quiet.
  const [now, setNow] = useState(() => Date.now());
  const hasPending = Object.keys(finishedAt).length > 0;
  useEffect(() => {
    if (!hasPending) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [hasPending]);

  const visible = filterAndBuildTree(agents, finishedAt, finishedGraceMs, now);
  if (visible.length === 0) return null;

  const ruleWidth = Math.max(20, Math.min(width - 2, 80));
  const rule = "─".repeat(ruleWidth);
  // Title takes the leading "─ Agents " (9 chars). Pad the trailing rule
  // so the line is exactly `ruleWidth` wide regardless of how short the
  // remainder is — prevents an empty title row when `width` is very small.
  const titleTrailing = "─".repeat(Math.max(0, ruleWidth - 9));

  return (
    <Box flexDirection="column" marginTop={1} marginBottom={1} paddingX={2}>
      <Text color={theme.faint}>─ Agents {titleTrailing}</Text>
      {visible.map((node) => (
        <AgentRow key={node.agent.id} node={node} width={width} />
      ))}
      <Text color={theme.faint}>{rule}</Text>
    </Box>
  );
}

interface AgentRowProps {
  node: TreeNode;
  width: number;
}

function AgentRow({node, width}: AgentRowProps) {
  const {agent, depth, isLast} = node;
  const finished = FINISHED.includes(agent.status);
  const glyph = glyphFor(agent.status);
  const glyphColor = colorFor(agent.status);
  const branch = renderBranch(depth, isLast);
  const detail = renderDetail(agent, finished);

  // Right-truncate the persona/model/detail strings so a long sub-agent
  // task can't push the elapsed/cost columns off the right edge on a
  // narrow terminal. Budget rough widths from the row's available space.
  const nameMax = width < theme.layout.narrowCols ? 14 : 28;
  const modelMax = width < theme.layout.narrowCols ? 12 : 24;
  const detailMax = width < theme.layout.narrowCols ? 18 : 60;

  return (
    <Box>
      <Text color={theme.faint}>{branch}</Text>
      <Text color={glyphColor} bold>
        {glyph}
      </Text>
      <Text color={finished ? theme.muted : theme.text}>
        {" "}
        {truncateSmart(agent.name, nameMax)}
      </Text>
      <Text color={theme.faint}>
        {"  "}
        {theme.glyph.dot} {truncateSmart(agent.model || "—", modelMax)}
      </Text>
      <Text color={theme.muted}>
        {"  "}
        {formatTokenCount(agent.tokens)} tok
      </Text>
      <Text color={theme.muted}>
        {"  "}{formatCost(agent.costUsd)}
      </Text>
      {finished ? null : (
        <Text color={theme.faint}>
          {"  "}
          {(agent.elapsedMs / 1000).toFixed(1)}s
        </Text>
      )}
      {detail ? (
        <Text color={detail.color}>
          {"  "}
          {truncateSmart(detail.text, detailMax)}
        </Text>
      ) : null}
    </Box>
  );
}

function renderBranch(depth: number, isLast: boolean): string {
  if (depth === 0) return "  ";
  // Single level of indentation — keep it shallow so deep agent trees still
  // fit. The ASCII branch chars come straight from box-drawing so the rail
  // reads as a tree even when colors are stripped.
  const prefix = "  ".repeat(depth);
  return prefix + (isLast ? "└─ " : "├─ ");
}

function renderDetail(
  agent: AgentInfo,
  finished: boolean,
): {text: string; color: string} | null {
  if (agent.status === "error") {
    return {text: "error", color: theme.danger};
  }
  if (agent.status === "cancelled") {
    return {text: "cancelled", color: theme.muted};
  }
  if (agent.status === "done") {
    return {text: "done", color: theme.success};
  }
  if (agent.status === "waiting_for_user") {
    return {text: "waiting on approval", color: theme.info};
  }
  if (finished) return null;
  if (agent.tool) {
    return {
      text: truncateSmart(agent.tool, 60),
      color: theme.warning,
    };
  }
  if (agent.task) {
    return {
      text: truncateSmart(agent.task, 60),
      color: theme.faint,
    };
  }
  return null;
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
      return theme.glyph.cancelled;
    default:
      return theme.glyph.diamond;
  }
}

function colorFor(status: AgentStatus): string {
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
      return theme.accent;
  }
}

/**
 * Build a flat depth-tagged list (parents before children) suitable for
 * sequential row rendering. Roots come first; finished children are
 * filtered out once they're past the grace window.
 *
 * The "main" agent is always at the root and never collapses, even if
 * the tracker reports it as `done` between turns.
 */
function filterAndBuildTree(
  agents: Record<string, AgentInfo>,
  finishedAt: Record<string, number>,
  graceMs: number,
  now: number,
): TreeNode[] {
  const all = Object.values(agents);
  if (all.length === 0) return [];

  const isStale = (a: AgentInfo): boolean => {
    if (!FINISHED.includes(a.status)) return false;
    // Root agents never get culled by the grace window — the user wants to
    // see the main agent's totals even when it's idle between turns.
    if (!a.parentId) return false;
    const flippedAt = finishedAt[a.id];
    if (!flippedAt) return false;
    return now - flippedAt > graceMs;
  };

  const visible = all.filter((a) => !isStale(a));
  const byParent = new Map<string | null, AgentInfo[]>();
  for (const a of visible) {
    const key = a.parentId ?? null;
    const list = byParent.get(key) ?? [];
    list.push(a);
    byParent.set(key, list);
  }

  const flat: TreeNode[] = [];
  const walk = (parentId: string | null, depth: number) => {
    const children = byParent.get(parentId) ?? [];
    children.sort((a, b) => a.elapsedMs - b.elapsedMs);
    children.forEach((agent, i) => {
      flat.push({
        agent,
        depth,
        isLast: i === children.length - 1,
      });
      walk(agent.id, depth + 1);
    });
  };
  walk(null, 0);
  return flat;
}

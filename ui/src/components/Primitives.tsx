import React from "react";
import {Box, Text} from "ink";
import {theme} from "../theme.js";
import type {ToolRisk} from "../protocol.js";

/* -------------------------------------------------------------------------- */
/*  RAIL — the backbone of the chat rhythm.                                   */
/*                                                                            */
/*  Replaces full-border panels with a single colored left edge. Created with */
/*  Ink's per-side border props so it renders as a clean pipe that spans the  */
/*  block's vertical height regardless of content.                            */
/* -------------------------------------------------------------------------- */

export interface RailProps {
  /** Rail colour — usually a semantic token from the theme. */
  color: string;
  /** Left padding between the rail and the content.  Default 2. */
  gap?: number;
  /** Extra right padding inside the content column. */
  paddingRight?: number;
  marginBottom?: number;
  marginTop?: number;
  children: React.ReactNode;
}

/**
 * `Rail` — a single vertical bar on the left of a content block.
 *
 * This replaces almost every full-border box in the UI.  A rail
 * establishes identity (tool category, error, approval) with ~10% of
 * the visual weight of a box and creates a consistent left margin
 * down the transcript so messages read as a timeline.
 */
export function Rail({
  color,
  gap = 2,
  paddingRight = 0,
  marginBottom = 1,
  marginTop = 0,
  children,
}: RailProps) {
  return (
    <Box
      borderStyle="single"
      borderLeft
      borderTop={false}
      borderBottom={false}
      borderRight={false}
      borderColor={color}
      paddingLeft={gap}
      paddingRight={paddingRight}
      marginBottom={marginBottom}
      marginTop={marginTop}
      flexDirection="column"
    >
      {children}
    </Box>
  );
}

/* -------------------------------------------------------------------------- */
/*  MESSAGE ROW — the header strip of a chat message.                         */
/*                                                                            */
/*  Renders: "[label]   [rightMeta]"  on a single line with optional items.   */
/* -------------------------------------------------------------------------- */

export interface MessageHeaderProps {
  /** Primary label text — usually a role name or tool name. */
  label: string;
  /** Label colour — defaults to primary text. */
  labelColor?: string;
  /** Small muted annotation displayed inline after the label. */
  annotation?: string;
  /** Right-aligned metadata — e.g. risk badge, elapsed, category. */
  right?: React.ReactNode;
}

export function MessageHeader({
  label,
  labelColor = theme.text,
  annotation,
  right,
}: MessageHeaderProps) {
  return (
    <Box justifyContent="space-between">
      <Box>
        <Text color={labelColor} bold>
          {label}
        </Text>
        {annotation ? (
          <Text color={theme.muted}>
            {"  "}
            {annotation}
          </Text>
        ) : null}
      </Box>
      {right ?? null}
    </Box>
  );
}

/* -------------------------------------------------------------------------- */
/*  BADGES                                                                    */
/* -------------------------------------------------------------------------- */

export interface RiskBadgeProps {
  risk: ToolRisk;
}

/**
 * Compact inline risk badge — colored text, no background chrome so it
 * doesn't fight with the rail of the block it sits inside.
 *
 * Honors "earned colors": low risk skips the warn glyph (the eye should
 * not register `⚠` for safe operations), medium and high keep it.
 */
export function RiskBadge({risk}: RiskBadgeProps) {
  const color = theme.risk[risk];
  const label = risk === "high" ? "high" : risk === "medium" ? "med" : "low";
  return (
    <Text color={color}>
      {risk === "low" ? "" : `${theme.glyph.warn} `}
      {label}
    </Text>
  );
}

export interface ActionPillProps {
  label: string;
  selected?: boolean;
  color: string;
}

/**
 * Action pill — inverted colour when selected so it reads like a
 * focused button (used in ApprovalPrompt).
 */
export function ActionPill({label, selected = false, color}: ActionPillProps) {
  return (
    <Text
      backgroundColor={selected ? color : undefined}
      color={selected ? "black" : color}
      bold={selected}
    >
      {` ${label} `}
    </Text>
  );
}

/* -------------------------------------------------------------------------- */
/*  DOT — colored status indicator.                                           */
/* -------------------------------------------------------------------------- */

export interface DotProps {
  color: string;
  glyph?: string;
}

export function Dot({color, glyph = theme.glyph.bullet}: DotProps) {
  return <Text color={color}>{glyph}</Text>;
}

/* -------------------------------------------------------------------------- */
/*  KBD — small key hint pill.                                                */
/* -------------------------------------------------------------------------- */

export interface KbdProps {
  label: string;
}

export function Kbd({label}: KbdProps) {
  return (
    <Text color={theme.accent} bold>
      {label}
    </Text>
  );
}


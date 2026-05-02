import React from "react";
import {Text} from "ink";
import Spinner from "ink-spinner";
import {motionEnabled} from "../lib/motion.js";
import {theme} from "../theme.js";

/**
 * Spinner that respects reduced-motion. When animation is disabled (CI,
 * dumb terminals, CODERAI_NO_ANIMATION=1) renders a single static glyph
 * instead of an animated frame loop. Color is the caller's responsibility
 * so it matches its surrounding context (warn, accent, etc).
 */
export interface QuietSpinnerProps {
  /** Override the static fallback glyph. Defaults to theme.glyph.pulse. */
  staticGlyph?: string;
}

export function QuietSpinner({staticGlyph = theme.glyph.pulse}: QuietSpinnerProps) {
  if (!motionEnabled) {
    return <Text>{staticGlyph}</Text>;
  }
  return <Spinner type="dots" />;
}

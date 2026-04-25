import React, {useRef, useState} from "react";
import {Box, Text, useInput} from "ink";
import TextInput from "ink-text-input";
import {theme} from "../theme.js";

export interface PromptProps {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /**
   * When true, Ctrl+C has been tapped once and is armed to exit on the
   * next tap. We surface that as a transient line below the input so the
   * user knows what the next keystroke will do.
   */
  exitHint?: boolean;
}

const MAX_HISTORY = 100;

/**
 * Input prompt.
 *
 *   ❯ type a message…
 *     Enter send · / help · Esc stop
 *
 * Redesign: one hint line, no `│` pipes.  Busy state dims the caret
 * rather than swapping the glyph so the eye has a stable anchor.
 */
export function Prompt({onSubmit, disabled, placeholder, exitHint}: PromptProps) {
  const [value, setValue] = useState("");

  const history = useRef<string[]>([]);
  const cursor = useRef<number>(0);
  const draftRef = useRef<string>("");

  const submit = (text: string) => {
    if (disabled) return;
    const trimmed = text.trim();
    if (!trimmed) return;

    const hist = history.current;
    if (hist[hist.length - 1] !== trimmed) {
      hist.push(trimmed);
      if (hist.length > MAX_HISTORY) hist.shift();
    }
    cursor.current = hist.length;
    draftRef.current = "";

    setValue("");
    onSubmit(trimmed);
  };

  useInput(
    (_input, key) => {
      if (!key.upArrow && !key.downArrow) return;

      const hist = history.current;
      if (hist.length === 0) return;

      if (key.upArrow) {
        if (cursor.current === hist.length) {
          draftRef.current = value;
        }
        cursor.current = Math.max(0, cursor.current - 1);
        setValue(hist[cursor.current] ?? "");
      } else if (key.downArrow) {
        cursor.current = Math.min(hist.length, cursor.current + 1);
        setValue(
          cursor.current === hist.length
            ? draftRef.current
            : (hist[cursor.current] ?? ""),
        );
      }
    },
    {isActive: !disabled},
  );

  return (
    <Box flexDirection="column" paddingX={theme.spacing.sm}>
      <Box>
        <Text color={disabled ? theme.faint : theme.accent} bold>
          {theme.glyph.caret}{" "}
        </Text>
        <TextInput
          value={value}
          onChange={(next) => {
            if (disabled) return;
            if (history.current.length > 0) {
              cursor.current = history.current.length;
            }
            draftRef.current = next;
            setValue(next);
          }}
          onSubmit={submit}
          placeholder={
            placeholder ??
            (disabled
              ? "waiting for agent…"
              : "ask anything, or type / for commands")
          }
          focus={!disabled}
          showCursor={!disabled}
        />
      </Box>
      {exitHint ? (
        <Box marginTop={theme.spacing.sm}>
          <Text color={theme.warning}>
            {theme.glyph.warn} ^C again to exit
          </Text>
        </Box>
      ) : history.current.length === 0 && !disabled ? (
        <Box marginTop={theme.spacing.sm}>
          <Text color={theme.faint}>
            <Text color={theme.muted}>/</Text> commands
            {theme.glyph.separator}
            <Text color={theme.muted}>^R</Text> reasoning
            {theme.glyph.separator}
            <Text color={theme.muted}>esc/^C</Text> cancel
            {theme.glyph.separator}
            <Text color={theme.muted}>^C^C</Text> quit
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}

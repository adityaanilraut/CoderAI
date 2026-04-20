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

export function Prompt({onSubmit, disabled, placeholder, exitHint}: PromptProps) {
  const [value, setValue] = useState("");

  // History is a ref so it survives re-renders without triggering them.
  // `cursor === history.length` means "no history entry selected"; the
  // user is editing their own draft which we stash in `draftRef`.
  const history = useRef<string[]>([]);
  const cursor = useRef<number>(0);
  const draftRef = useRef<string>("");

  const submit = (text: string) => {
    if (disabled) return;
    const trimmed = text.trim();
    if (!trimmed) return;

    // Push into history (deduped with previous entry) and reset cursor.
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

  // Handle Up/Down arrows for history recall BEFORE ink-text-input sees
  // them. In a single-line input, vertical arrow keys are a no-op inside
  // TextInput, so we just steal them for history navigation.
  useInput(
    (_input, key) => {
      if (!key.upArrow && !key.downArrow) return;

      const hist = history.current;
      if (hist.length === 0) return;

      if (key.upArrow) {
        if (cursor.current === hist.length) {
          // Entering history: stash whatever the user was typing.
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
    <Box flexDirection="column">
      <Box>
        <Text color={disabled ? theme.muted : theme.accent} bold>
          {"❯ "}
        </Text>
        <TextInput
          value={value}
          onChange={(next) => {
            if (disabled) return;
            // Typing invalidates history navigation: snap back to "draft".
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
              : "Ask anything, or type / for commands")
          }
          focus={!disabled}
          showCursor={!disabled}
        />
      </Box>
      <Box marginTop={0}>
        {exitHint ? (
          <Text color={theme.warning}>
            Press Ctrl+C again to exit, or type a message to continue
          </Text>
        ) : (
          <Text color={theme.muted}>
            Enter to send · ↑/↓ history · Esc to interrupt · Ctrl+C twice to
            quit · /help for commands
          </Text>
        )}
      </Box>
    </Box>
  );
}

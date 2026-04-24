import React, {useState} from "react";
import {Box, Text, useInput} from "ink";
import {theme} from "../theme.js";
import {Rail, MessageHeader, Kbd} from "./Primitives.js";

export interface ErrorPanelProps {
  category: "provider" | "tool" | "internal";
  message: string;
  hint?: string;
  details?: string;
  /**
   * When true, the component listens for its detail-toggle shortcut.
   * The parent should set this only for the most recently-emitted
   * error so multiple panels do not compete for the same keys.
   */
  canExpand?: boolean;
  /** When true, require Ctrl+D so the compose box can stay focused safely. */
  promptActive?: boolean;
}

/**
 * Friendly error block.
 *
 *   ▌ ⚠ Provider error                  provider
 *   ▌ missing ANTHROPIC_API_KEY
 *   ▌ → run `coderAI setup` or set the env var
 *   ▌ Ctrl+D to show details
 *
 * Replaces raw Python tracebacks with a short summary and an optional
 * expandable details view keyed on `d`/`Ctrl+D`.
 */
export function ErrorPanel({
  category,
  message,
  hint,
  details,
  canExpand = false,
  promptActive = false,
}: ErrorPanelProps) {
  const [expanded, setExpanded] = useState(false);

  useInput(
    (input, key) => {
      const wantsToggle = promptActive
        ? key.ctrl && (input === "d" || input === "D")
        : input === "d" || input === "D" || (key.ctrl && input === "d");
      if (wantsToggle) setExpanded((e) => !e);
    },
    {isActive: canExpand && Boolean(details)},
  );

  const title =
    category === "provider"
      ? "Provider error"
      : category === "tool"
        ? "Tool error"
        : "Internal error";

  const resolvedHint =
    hint ??
    inferHint(message, category) ??
    "Check stderr for more detail or try again.";

  return (
    <Rail color={theme.danger} gap={2} marginBottom={1} marginTop={1}>
      <MessageHeader
        label={`${theme.glyph.warn} ${title}`}
        labelColor={theme.danger}
        right={<Text color={theme.faint}>{category}</Text>}
      />
      <Box marginTop={1}>
        <Text color={theme.text}>{message}</Text>
      </Box>
      <Box>
        <Text color={theme.warning}>
          {theme.glyph.arrowRun} {resolvedHint}
        </Text>
      </Box>
      {details ? (
        <>
          {canExpand ? (
            <Box marginTop={1}>
              <Text color={theme.faint}>
                <Kbd label={promptActive ? "Ctrl+D" : "d"} />
                <Text color={theme.faint}>
                  {" "}
                  to {expanded ? "hide" : "show"} details
                </Text>
              </Text>
            </Box>
          ) : null}
          {expanded ? (
            <Box flexDirection="column" marginTop={1}>
              <Text color={theme.muted}>{details}</Text>
            </Box>
          ) : null}
        </>
      ) : null}
    </Rail>
  );
}

function inferHint(message: string, category: string): string | null {
  const lower = message.toLowerCase();
  if (category === "provider") {
    if (lower.includes("localhost:1234") || lower.includes("lmstudio"))
      return "Start LM Studio: open the app → Developer → Start Server.";
    if (lower.includes("localhost:11434") || lower.includes("ollama"))
      return "Start Ollama: run `ollama serve` in another terminal.";
    if (lower.includes("anthropic") && lower.includes("key"))
      return "Set ANTHROPIC_API_KEY, or run `coderAI config set anthropic_api_key <KEY>`.";
    if (lower.includes("openai") && lower.includes("key"))
      return "Set OPENAI_API_KEY, or run `coderAI config set openai_api_key <KEY>`.";
    if (lower.includes("groq") && lower.includes("key"))
      return "Set GROQ_API_KEY, or run `coderAI config set groq_api_key <KEY>`.";
    if (lower.includes("deepseek") && lower.includes("key"))
      return "Set DEEPSEEK_API_KEY, or run `coderAI config set deepseek_api_key <KEY>`.";
    if (
      lower.includes("api key") ||
      lower.includes("401") ||
      lower.includes("unauthorized") ||
      lower.includes("authentication")
    )
      return "Missing/invalid API key — run `coderAI setup` or `coderAI config set <provider>_api_key <KEY>`.";
    if (
      lower.includes("rate limit") ||
      lower.includes("429") ||
      lower.includes("too many requests")
    )
      return "Rate limited — wait a few seconds and retry, or switch models with /model <name>.";
    if (lower.includes("context") && lower.includes("length"))
      return "Context window exceeded. Try /compact to summarize, or /clear to reset.";
    if (
      lower.includes("quota") ||
      lower.includes("insufficient") ||
      lower.includes("billing")
    )
      return "Provider reports quota/billing exhausted. Top up credits or switch providers.";
    if (lower.includes("timeout") || lower.includes("timed out"))
      return "Request timed out. Try again; if it persists, check your network and /model.";
    if (
      lower.includes("cannot connect") ||
      lower.includes("connection refused") ||
      lower.includes("econnrefused") ||
      lower.includes("getaddrinfo")
    )
      return "Network/service unreachable. Check the endpoint URL, DNS, and firewall.";
    if (lower.includes("ssl") || lower.includes("certificate"))
      return "TLS handshake failed. Check your system clock and corporate proxy/CA certs.";
  }
  if (category === "tool") {
    if (
      lower.includes("permission denied") ||
      lower.includes("eacces") ||
      lower.includes("eperm")
    )
      return "The tool lacks filesystem permissions. Check file ownership/mode.";
    if (lower.includes("not found") || lower.includes("enoent"))
      return "Target path or command wasn't found. Double-check the argument.";
    if (lower.includes("timeout") || lower.includes("timed out"))
      return "Tool timed out. For long shells try run_background, or raise timeout in args.";
    if (lower.includes("cancelled") || lower.includes("cancel"))
      return "Cancelled — press Esc again or send a new message to continue.";
  }
  return null;
}

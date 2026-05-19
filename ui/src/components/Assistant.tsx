import React, { useMemo } from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";
import { hyperlink, linkifyFileRefs, type TextSegment } from "../lib/hyperlink.js";

interface ContentChunk {
  kind: "text" | "code";
  text: string;
  lang?: string;
}

/**
 * Inline span produced by the lightweight markdown pass. We don't aim for
 * CommonMark — just the formatting the model actually emits in
 * conversational answers: backtick-wrapped code, **bold**, *italic*. Inside
 * a `code` span we never run linkify or further formatting (matching how
 * other markdown renderers behave).
 */
type InlineSpan =
  | { kind: "text"; text: string; bold?: boolean; italic?: boolean }
  | { kind: "code"; text: string };

export interface AssistantProps {
  content: string;
  streaming: boolean;
  reasoning: string;
  showReasoning?: boolean;
  /** When true, this is the latest assistant turn — show the collapsed
   *  reasoning hint so the user knows reasoning is available without
   *  cluttering older turns. */
  isLatest?: boolean;
  /** Working directory used to resolve relative file paths in OSC-8 links. */
  cwd?: string;
}

/**
 * Assistant message — chrome-free.
 *
 * The model talking is signaled by the absence of `❯`. No header row,
 * no rail, no role label. Reasoning is buffered and only rendered when
 * verbose mode is on (or when the user runs `/think`, which
 * surfaces it as an explicit toast — see useAgent.revealReasoning).
 */
export function Assistant({
  content,
  streaming,
  reasoning,
  showReasoning,
  isLatest = false,
  cwd,
}: AssistantProps) {
  const trimmedReasoning = reasoning.trim();
  const hasContent = Boolean(content);
  // Reasoning visibility:
  //   - verbose mode (showReasoning=true): always inline-expanded
  //   - latest turn + has reasoning: a single-line hint mentions it; user
  //     opens via `/think` slash command (no keystroke needed — keys would
  //     conflict with composing the next message)
  //   - older turns: hidden
  const reasoningAvailable = Boolean(trimmedReasoning);

  // Parse content into text/code chunks (``` fences).
  const chunks = useMemo(() => parseCodeFences(content), [content]);

  // Two-pass tokenizer for text chunks:
  //   1. Pull out inline markdown spans (`code`, **bold**, *italic*).
  //   2. Within each non-code text span, run linkifyFileRefs so file:line
  //      references inside narrative prose still become clickable.
  const linkedChunks = useMemo(
    () =>
      chunks.map((chunk) => {
        if (chunk.kind !== "text") return chunk;
        const inline = parseInlineMarkdown(chunk.text);
        const decorated = inline.map((span) =>
          span.kind === "code"
            ? span
            : {
                ...span,
                links: linkifyFileRefs(span.text, cwd),
              },
        );
        return { ...chunk, inline: decorated };
      }),
    [chunks, cwd],
  );

  if (!hasContent && !reasoningAvailable && !streaming) return null;

  return (
    <Box flexDirection="column" marginTop={1} marginBottom={1} paddingX={2}>
      {reasoningAvailable && showReasoning ? (
        <Box marginBottom={hasContent || streaming ? 1 : 0}>
          <Text color={theme.faint} italic>
            {trimmedReasoning}
          </Text>
        </Box>
      ) : reasoningAvailable && isLatest && !streaming ? (
        <Box marginBottom={hasContent ? 1 : 0}>
          <Text color={theme.faint}>
            ▸ reasoning ({trimmedReasoning.length.toLocaleString()} chars){" "}
            <Text color={theme.muted}>· /think to view</Text>
          </Text>
        </Box>
      ) : null}
      {hasContent ? (
        <Box flexDirection="column">
          {linkedChunks.map((chunk, ci) =>
            chunk.kind === "code" ? (
              <Box key={ci} flexDirection="column" marginY={1}>
                {chunk.lang ? (
                  <Text color={theme.codeBlock.label}>
                    {theme.glyph.dot} {chunk.lang}
                  </Text>
                ) : null}
                <Box paddingX={1} paddingY={0}>
                  <Text backgroundColor={theme.codeBlock.bg} color={theme.textSoft}>
                    {chunk.text}
                  </Text>
                </Box>
              </Box>
            ) : (
              <Text key={ci}>
                {(chunk as ContentChunk & {
                  inline: Array<
                    | { kind: "code"; text: string }
                    | { kind: "text"; text: string; bold?: boolean; italic?: boolean; links: TextSegment[] }
                  >;
                }).inline.map((span, si) => {
                  if (span.kind === "code") {
                    return (
                      <Text
                        key={si}
                        backgroundColor={theme.codeBlock.bg}
                        color={theme.textSoft}
                      >
                        {span.text}
                      </Text>
                    );
                  }
                  return (
                    <Text key={si}>
                      {span.links.map((seg, li) =>
                        seg.href ? (
                          <Text
                            key={li}
                            color={theme.link}
                            bold={span.bold}
                            italic={span.italic}
                          >
                            {hyperlink(seg.href, seg.text)}
                          </Text>
                        ) : (
                          <Text
                            key={li}
                            color={theme.text}
                            bold={span.bold}
                            italic={span.italic}
                          >
                            {seg.text}
                          </Text>
                        ),
                      )}
                    </Text>
                  );
                })}
              </Text>
            ),
          )}
          {streaming ? <Text color={theme.faint}> ▋</Text> : null}
        </Box>
      ) : streaming ? (
        <Text color={theme.faint}>…</Text>
      ) : null}
    </Box>
  );
}

export interface UserBubbleProps {
  text: string;
}

/**
 * User message echoed back into the transcript. Uses `sentCaret` (›) to
 * distinguish a *sent* message from the *editable* prompt below, which uses
 * `caret` (❯). Without this differentiation a glanced page can't separate
 * "I said this" from "now editing".
 */
export function UserBubble({ text }: UserBubbleProps) {
  const lines = text.split("\n");
  return (
    <Box flexDirection="column" marginTop={1} marginBottom={1} paddingX={1}>
      {lines.map((line, i) => (
        <Box key={i}>
          {i === 0 ? (
            <Text color={theme.role.user} bold>
              {theme.glyph.sentCaret}{" "}
            </Text>
          ) : (
            <Text>{"  "}</Text>
          )}
          <Text color={theme.textSoft}>{line}</Text>
        </Box>
      ))}
    </Box>
  );
}

/**
 * Greedy left-to-right inline markdown tokenizer.
 *
 * Supported syntax (in match order — `code` wins over bold/italic so a
 * snippet like `` `foo **bar** baz` `` renders as one code span):
 *
 *   `code`       → monospaced code span
 *   **bold**     → bold
 *   __bold__     → bold
 *   *italic*     → italic
 *   _italic_     → italic
 *
 * Unmatched markers are emitted verbatim so prose like "use *args" doesn't
 * silently italicize the trailing text.
 */
function parseInlineMarkdown(input: string): InlineSpan[] {
  const out: InlineSpan[] = [];
  let buf = "";
  const flushText = (bold?: boolean, italic?: boolean) => {
    if (buf.length === 0) return;
    out.push({ kind: "text", text: buf, bold, italic });
    buf = "";
  };

  let i = 0;
  while (i < input.length) {
    const ch = input[i];

    // Inline code: backtick-delimited; consume up to the next backtick.
    if (ch === "`") {
      const end = input.indexOf("`", i + 1);
      if (end !== -1) {
        flushText();
        out.push({ kind: "code", text: input.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }

    // Bold: ** or __ . Require non-space immediately after the opener and
    // before the closer so "use **args" doesn't accidentally bold half the
    // sentence — same rule CommonMark uses for emphasis runs.
    if (
      (ch === "*" && input[i + 1] === "*") ||
      (ch === "_" && input[i + 1] === "_")
    ) {
      const delim = ch + ch;
      const start = i + 2;
      if (start < input.length && input[start] !== " " && input[start] !== "\n") {
        const end = input.indexOf(delim, start);
        if (end > start && input[end - 1] !== " " && input[end - 1] !== "\n") {
          flushText();
          out.push({ kind: "text", text: input.slice(start, end), bold: true });
          i = end + 2;
          continue;
        }
      }
    }

    // Italic: * or _. Same non-space rule, plus avoid matching inside a word
    // ("snake_case" must not italicize "case_" until the next underscore).
    if ((ch === "*" || ch === "_") && input[i + 1] !== ch) {
      const before = i === 0 ? " " : input[i - 1];
      const start = i + 1;
      const startsWord = ch === "_" ? !/\w/.test(before) : true;
      if (
        startsWord &&
        start < input.length &&
        input[start] !== " " &&
        input[start] !== "\n"
      ) {
        // Walk to the matching closer; skip over backslash escapes.
        let end = -1;
        for (let j = start; j < input.length; j++) {
          if (input[j] === "\n") break;
          if (input[j] === ch) {
            const after = j + 1 < input.length ? input[j + 1] : " ";
            const endsWord = ch === "_" ? !/\w/.test(after) : true;
            if (input[j - 1] !== " " && endsWord) {
              end = j;
              break;
            }
          }
        }
        if (end > start) {
          flushText();
          out.push({ kind: "text", text: input.slice(start, end), italic: true });
          i = end + 1;
          continue;
        }
      }
    }

    buf += ch;
    i++;
  }
  flushText();
  return out;
}

function parseCodeFences(text: string): ContentChunk[] {
  const chunks: ContentChunk[] = [];
  const lines = text.split("\n");
  let i = 0;
  let textBuf: string[] = [];

  function pushText() {
    if (textBuf.length > 0) {
      chunks.push({ kind: "text", text: textBuf.join("\n") });
      textBuf = [];
    }
  }

  while (i < lines.length) {
    const line = lines[i];
    if (line.trimStart().startsWith("```")) {
      pushText();
      const lang = line.trimStart().slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      chunks.push({
        kind: "code",
        lang: lang || undefined,
        text: codeLines.join("\n"),
      });
      i++; // skip closing ```
    } else {
      textBuf.push(line);
      i++;
    }
  }
  pushText();
  return chunks;
}

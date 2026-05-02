import React, { useMemo } from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";
import { hyperlink, linkifyFileRefs } from "../lib/hyperlink.js";

interface CodeBlock {
  lang: string;
  code: string;
}

interface ContentChunk {
  kind: "text" | "code";
  text: string;
  lang?: string;
}

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

  // Tokenize each text chunk for file:line hyperlinks.
  const linkedChunks = useMemo(
    () =>
      chunks.map((chunk) =>
        chunk.kind === "text"
          ? { ...chunk, links: linkifyFileRefs(chunk.text, cwd) }
          : chunk,
      ),
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
                {(chunk as ContentChunk & { links: import("../lib/hyperlink.js").TextSegment[] }).links.map(
                  (seg, si) =>
                    seg.href ? (
                      <Text key={si} color={theme.link}>
                        {hyperlink(seg.href, seg.text)}
                      </Text>
                    ) : (
                      <Text key={si} color={theme.text}>
                        {seg.text}
                      </Text>
                    ),
                )}
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

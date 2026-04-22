import type {TimelineItem} from "./agentStateTypes.js";

const MAX_TIMELINE = 500;
const TRIM_TO = 400;

export function appendCapped(
  prev: TimelineItem[],
  item: TimelineItem,
): TimelineItem[] {
  if (prev.length < MAX_TIMELINE) return [...prev, item];
  const dropped = prev.length - TRIM_TO + 1;
  const marker: TimelineItem = {
    kind: "toast",
    id: `trim_${Date.now().toString(36)}`,
    level: "info",
    message: `… ${dropped} earlier timeline entries trimmed for performance …`,
  };
  return [marker, ...prev.slice(-TRIM_TO + 1), item];
}

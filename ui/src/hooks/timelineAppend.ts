import type {TimelineItem} from "./agentStateTypes.js";

const MAX_TIMELINE = 500;
const TRIM_TO = 400;

/**
 * Append `item` to `prev`, trimming the head when the timeline grows past
 * `MAX_TIMELINE`. The result is always at most `TRIM_TO` items.
 *
 * The trim leaves a single `toast` marker at the top of the kept tail so the
 * user can tell that earlier history was dropped on purpose. The marker uses
 * `nextId()` (when supplied) so its id collides with neither the rest of the
 * timeline nor a second trim in the same millisecond.
 */
export function appendCapped(
  prev: TimelineItem[],
  item: TimelineItem,
  nextId?: () => string,
): TimelineItem[] {
  if (prev.length < MAX_TIMELINE) return [...prev, item];
  const tailKeep = TRIM_TO - 2; // marker + new item account for the other two
  const dropped = prev.length - tailKeep;
  const marker: TimelineItem = {
    kind: "toast",
    id: nextId ? nextId() : `trim_${Date.now().toString(36)}`,
    level: "warning",
    message: `… ${dropped} earlier entries trimmed · keeping most recent ${TRIM_TO} …`,
  };
  return [marker, ...prev.slice(-tailKeep), item];
}

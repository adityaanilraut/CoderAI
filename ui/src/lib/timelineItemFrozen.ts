import type {TimelineItem} from "../hooks/agentStateTypes.js";

/**
 * Whether a timeline item will never change again — safe to pass to Ink `<Static>`.
 * When adding a new `TimelineItem` kind, update this switch and run
 * `tests/test_ui_timeline_frozen.py` (case coverage; kinds match `agentStateTypes`) — TypeScript will also
 * error here if a kind is not handled.
 */
export function isTimelineItemFrozen(item: TimelineItem): boolean {
  switch (item.kind) {
    case "user":
      return true;
    case "assistant":
      return !item.streaming;
    case "tool":
      return item.ok !== null;
    case "diff":
      return true;
    case "error":
      return true;
    case "toast":
      return true;
    case "approval":
      return item.decided !== "pending";
  }
}

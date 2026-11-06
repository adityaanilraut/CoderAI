import type { Event, ToolInputDisplay } from '@coderai/sdk';
import type { TerminalView } from './view.js';

type ActivityView = Pick<TerminalView, 'setActivity' | 'setRetry' | 'dialogOpen'>;
type Tool = { label: string; detail: string };

function toolDetail(display?: ToolInputDisplay): string {
  switch (display?.kind) {
    case 'command': return `$ ${display.command}`;
    case 'file_io': case 'diff': return display.path;
    case 'search': return display.query;
    case 'url_fetch': return display.url;
    case 'agent_call': return display.agent_name;
    case 'skill_call': return display.skill_name;
    default: return '';
  }
}

export class TurnActivity {
  private readonly tools = new Map<string, Tool>();
  private retrying = false;
  private cancelled = false;

  constructor(private readonly view: ActivityView) {}

  private show(label: string, detail = ''): void {
    this.retrying = false;
    if (!this.cancelled && !this.view.dialogOpen) this.view.setActivity(label, detail);
  }

  cancel(): void { this.cancelled = true; }

  waiting(): void {
    const tool = [...this.tools.values()].at(-1);
    if (tool) this.show(tool.label, tool.detail);
    else this.show('Working · Waiting for model');
  }

  handle(event: Event): void {
    if (event.agentId !== 'main') return;
    switch (event.type) {
      case 'turn.started': this.tools.clear(); this.cancelled = false; this.waiting(); break;
      case 'turn.ended': this.tools.clear(); this.retrying = false; break;
      // Keep the retry attempt and provider reason visible until there is new output.
      case 'turn.step.started': if (!this.retrying) this.waiting(); break;
      case 'turn.step.completed': if (this.retrying) this.waiting(); break;
      case 'assistant.delta': this.show('Writing response'); break;
      case 'thinking.delta': this.show('Thinking'); break;
      case 'tool.call.started': {
        const tool = { label: event.description || `Running ${event.name}`, detail: toolDetail(event.display) };
        this.tools.set(event.toolCallId, tool); this.show(tool.label, tool.detail); break;
      }
      case 'tool.progress': {
        const tool = this.tools.get(event.toolCallId);
        if (tool) {
          tool.detail = event.update.text || tool.detail;
          this.show(tool.label, tool.detail);
        }
        break;
      }
      case 'tool.result': this.tools.delete(event.toolCallId); this.waiting(); break;
      case 'turn.step.retrying':
        this.retrying = true;
        if (!this.cancelled && !this.view.dialogOpen) this.view.setRetry(event);
        break;
      case 'compaction.started': this.show('Compacting context'); break;
      case 'compaction.completed': case 'compaction.cancelled': case 'compaction.blocked': this.waiting(); break;
    }
  }
}

/**
 * Spawns the Python agent as a child process and bridges the NDJSON protocol
 * to a small typed EventEmitter-like API for React hooks to consume.
 */

import {spawn, type ChildProcessWithoutNullStreams} from "node:child_process";
import {EventEmitter} from "node:events";
import readline from "node:readline";
import {
  AGENT_EVENT_NAMES,
  type AgentEvent,
  type ReasoningEffort,
  type UIEnvelope,
} from "../protocol.js";

const DEBUG_IPC =
  process.env.CODERAI_DEBUG === "1" || process.env.CODERAI_DEBUG === "true";

export interface AgentClientOptions {
  /** Python executable, defaults to `python3`. */
  python?: string;
  /** Args passed to the Python interpreter to start the IPC server. */
  args?: string[];
  /** Working directory for the child. */
  cwd?: string;
  /** Extra env vars. */
  env?: NodeJS.ProcessEnv;
}

type EventName = AgentEvent["event"] | "raw" | "stderr" | "exit";

export class AgentClient extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private rl: readline.Interface | null = null;
  private cmdSeq = 0;

  constructor(private readonly opts: AgentClientOptions = {}) {
    super();
    this.setMaxListeners(64);
  }

  start(): void {
    if (this.child) return;

    const python = this.opts.python ?? process.env.CODERAI_PYTHON ?? "python3";
    const args = this.opts.args ?? [
      "-u",
      "-m",
      "coderAI.ipc.entry",
    ];
    this.child = spawn(python, args, {
      cwd: this.opts.cwd ?? process.cwd(),
      env: {...process.env, ...this.opts.env, PYTHONUNBUFFERED: "1"},
      stdio: ["pipe", "pipe", "pipe"],
    });

    this.rl = readline.createInterface({
      input: this.child.stdout,
      crlfDelay: Infinity,
    });

    this.rl.on("line", (line) => this._handleLine(line));

    this.child.stderr.setEncoding("utf8");
    this.child.stderr.on("data", (chunk: string) => {
      this.emit("stderr", chunk);
    });

    this.child.on("exit", (code, signal) => {
      this.emit("exit", {code, signal});
    });
  }

  private _handleLine(line: string): void {
    if (!line.trim()) return;
    let msg: UIEnvelope | null = null;
    try {
      msg = JSON.parse(line) as UIEnvelope;
    } catch {
      if (DEBUG_IPC) {
        process.stderr.write(
          `[coderai-ui] stdout line is not JSON (ignored as IPC event): ${line.slice(0, 220)}${line.length > 220 ? "…" : ""}\n`,
        );
      }
      this.emit("raw", line);
      return;
    }
    if (!msg || msg.kind !== "event") {
      if (DEBUG_IPC) {
        process.stderr.write(
          `[coderai-ui] line is not a UI event (kind !== \"event\"), dropped: ${line.slice(0, 220)}${line.length > 220 ? "…" : ""}\n`,
        );
      }
      return;
    }
    const en = (msg as UIEnvelope & {event: string}).event;
    if (DEBUG_IPC && !AGENT_EVENT_NAMES.includes(en)) {
      process.stderr.write(
        `[coderai-ui] unknown event name ${JSON.stringify(en)} (listeners may still handle it)\n`,
      );
    }
    this.emit(msg.event, msg);
    this.emit("raw", msg);
  }

  send(cmd: Record<string, unknown> & {cmd: string}): string {
    if (!this.child) throw new Error("AgentClient not started");
    const id =
      (cmd.id as string | undefined) ??
      `c_${++this.cmdSeq}_${Date.now().toString(36)}`;
    const envelope = {v: 1, kind: "cmd", id, ...cmd};
    this.child.stdin.write(JSON.stringify(envelope) + "\n");
    return id;
  }

  sendMessage(text: string): string {
    return this.send({cmd: "send_message", text});
  }

  cancel(agentId?: string): string {
    return this.send({cmd: "cancel", agentId});
  }

  setModel(model: string): string {
    return this.send({cmd: "set_model", model});
  }

  toggleAutoApprove(): string {
    return this.send({cmd: "toggle_auto_approve"});
  }

  setReasoning(effort: ReasoningEffort): string {
    return this.send({cmd: "set_reasoning", effort});
  }

  approveTool(toolId: string, approve: boolean): string {
    return this.send({cmd: "tool_approval_resp", toolId, approve});
  }

  getState(): string {
    return this.send({cmd: "get_state"});
  }

  getPlan(): string {
    return this.send({cmd: "get_plan"});
  }

  reference(topic: string): string {
    return this.send({cmd: "reference", topic});
  }

  setDefaultModel(model: string): string {
    return this.send({cmd: "set_default_model", model});
  }

  compactContext(): string {
    return this.send({cmd: "compact_context"});
  }

  clearContext(): string {
    return this.send({cmd: "clear_context"});
  }

  exit(): string {
    return this.send({cmd: "exit"});
  }

  async stop(): Promise<void> {
    if (!this.child) return;
    // Register the exit listener BEFORE sending the exit command so we cannot
    // miss the event if the child exits very quickly after receiving it.
    const exitPromise = new Promise<void>((resolve) => {
      const timeout = setTimeout(() => {
        this.child?.kill("SIGKILL");
        resolve();
      }, 2000);
      this.child!.once("exit", () => {
        clearTimeout(timeout);
        resolve();
      });
    });
    try {
      this.exit();
    } catch {
      // Child may have already exited; best effort.
    }
    await exitPromise;
    this.child = null;
    this.rl?.close();
    this.rl = null;
  }

  override on<E extends EventName>(
    event: E,
    listener: (payload: any) => void,
  ): this {
    return super.on(event, listener);
  }

  override once<E extends EventName>(
    event: E,
    listener: (payload: any) => void,
  ): this {
    return super.once(event, listener);
  }
}

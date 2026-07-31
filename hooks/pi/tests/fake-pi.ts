import extension from "../index.ts";

/**
 * Minimal Pi ExtensionAPI stand-in: captures registered event handlers and
 * lets tests replay synthetic event sequences.
 */
export class FakePi {
  handlers = new Map<string, Array<(event: any, ctx: any) => unknown>>();
  commands: string[] = [];
  ctx: any;

  constructor(sessionFile?: string) {
    this.ctx = sessionFile
      ? { sessionManager: { getSessionFile: () => sessionFile } }
      : {};
  }

  on(eventName: string, handler: (event: any, ctx: any) => unknown) {
    const list = this.handlers.get(eventName) ?? [];
    list.push(handler);
    this.handlers.set(eventName, list);
  }

  registerCommand(name: string, _spec: unknown) {
    this.commands.push(name);
  }

  async emit(eventName: string, event: Record<string, unknown> = {}, ctx: any = this.ctx) {
    for (const handler of this.handlers.get(eventName) ?? []) {
      await handler(event, ctx);
    }
  }
}

export async function loadExtension(sessionFile?: string): Promise<FakePi> {
  const pi = new FakePi(sessionFile);
  await extension(pi);
  return pi;
}

/** Replay a plain run: agent start -> one turn with generation -> agent end. */
export async function replayBasicRun(
  pi: FakePi,
  options: { prompt?: string; toolFails?: boolean; withTool?: boolean } = {},
) {
  await pi.emit("session_start");
  await pi.emit("before_agent_start", { prompt: options.prompt ?? "hello" });
  await pi.emit("agent_start", { prompt: options.prompt ?? "hello" });
  await pi.emit("turn_start", { turnIndex: 0 });
  await pi.emit("before_provider_request", {
    requestId: "req-1",
    request: { messages: [{ role: "user", content: options.prompt ?? "hello" }], temperature: 0.2 },
    model: "test-model",
  });
  await pi.emit("message_update", {
    requestId: "req-1",
    message: { role: "assistant", content: [{ type: "text", text: "partial" }] },
  });
  if (options.withTool !== false) {
    await pi.emit("tool_execution_start", { toolCallId: "tc-1", toolName: "read_file", input: { path: "a.txt" } });
    await pi.emit("tool_result", {
      toolCallId: "tc-1",
      ...(options.toolFails
        ? { isError: true, error: "boom failed" }
        : { content: [{ type: "text", text: "file contents" }] }),
    });
  }
  await pi.emit("message_end", {
    requestId: "req-1",
    message: {
      role: "assistant",
      content: [{ type: "text", text: "final answer" }],
      usage: { input: 10, output: 5, cacheRead: 2 },
      model: "test-model",
      stopReason: "end_turn",
    },
  });
  await pi.emit("turn_end", {
    message: { role: "assistant", content: [{ type: "text", text: "final answer" }] },
  });
  await pi.emit("agent_end", {
    messages: [
      { role: "user", content: "hello" },
      { role: "assistant", content: [{ type: "text", text: "final answer" }] },
    ],
  });
}

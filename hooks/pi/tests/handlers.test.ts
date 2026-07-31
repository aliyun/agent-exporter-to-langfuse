import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearAllSessionStates } from "../src/state.ts";
import { loadExtension, replayBasicRun, type FakePi } from "./fake-pi.ts";

interface CapturedRequest {
  url: string;
  body: Record<string, unknown>;
}

function collectSpans(otlp: Record<string, unknown>): any[] {
  return (otlp.resourceSpans as any[]).flatMap((rs) =>
    (rs.scopeSpans as any[]).flatMap((ss) => ss.spans as any[]),
  );
}

function attrValue(span: any, key: string): string | undefined {
  const attr = (span.attributes as any[]).find((a) => a.key === key);
  return attr?.value?.stringValue;
}

describe("lifecycle handlers", () => {
  let requests: CapturedRequest[];
  const savedEnv = { ...process.env };

  beforeEach(() => {
    clearAllSessionStates();
    requests = [];
    // Sandbox HOME so a locally installed pi.env with real credentials never leaks in.
    process.env.HOME = mkdtempSync(join(tmpdir(), "pi-home-"));
    process.env.LANGSTASH_ENABLED = "true";
    process.env.LANGSTASH_URL = "http://127.0.0.1:15288";
    vi.stubGlobal("fetch", async (url: string, options: any) => {
      requests.push({ url: String(url), body: JSON.parse(options.body) });
      return { ok: true, status: 202 };
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(process.env)) {
      if (!(key in savedEnv)) delete process.env[key];
    }
    Object.assign(process.env, savedEnv);
  });

  async function runAndGetTrace(
    fn: (pi: FakePi) => Promise<void>,
    sessionFile?: string,
  ): Promise<Record<string, unknown>> {
    const pi = await loadExtension(sessionFile);
    await fn(pi);
    expect(requests.length, "agent_end 后必须恰好投递一条 trace 到 /ingest").toBe(1);
    expect(requests[0].url).toContain("/ingest");
    return requests[0].body;
  }

  it("registers no interactive commands", async () => {
    const pi = await loadExtension();
    expect(pi.commands, "hook 不得注册任何 /langfuse-* 命令").toEqual([]);
  });

  it("produces a full trace with TTFT generation for a normal run", async () => {
    const otlp = await runAndGetTrace((pi) => replayBasicRun(pi), "/tmp/sessions/s-normal.jsonl");
    const spans = collectSpans(otlp);

    const roots = spans.filter((s) => !s.parentSpanId);
    expect(roots, "必须含且仅含一个无 parentSpanId 的 root span").toHaveLength(1);
    const root = roots[0];
    expect(root.name).toBe("pi-agent");
    expect(attrValue(root, "langfuse.trace.name")).toBe("pi-agent");
    expect(attrValue(root, "session.id"), "session.id 必须来自 Pi session 文件名").toBe("s-normal");
    expect(attrValue(root, "langfuse.trace.tags")).toContain("pi");
    expect(attrValue(root, "user.id")).toBeTruthy();
    expect(attrValue(root, "langfuse.observation.input"), "root 必须携带 prompt 输入").toContain("hello");
    expect(attrValue(root, "langfuse.observation.output"), "root 必须携带最终输出").toContain("final answer");

    const turn = spans.find((s) => s.name === "turn");
    expect(turn, "turn span 必须存在").toBeTruthy();
    expect(turn.parentSpanId, "turn 必须挂在 root 下").toBe(root.spanId);

    const generation = spans.find((s) => attrValue(s, "langfuse.observation.type") === "generation");
    expect(generation, "generation span 必须存在").toBeTruthy();
    expect(generation.parentSpanId, "generation 必须挂在 turn 下").toBe(turn.spanId);
    expect(
      attrValue(generation, "langfuse.observation.completion_start_time"),
      "generation 必须记录 TTFT",
    ).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(attrValue(generation, "langfuse.observation.model.name")).toBe("test-model");

    const usage = JSON.parse(attrValue(generation, "langfuse.observation.usage_details")!);
    expect(usage, "usage_details 必须含标准键").toEqual({
      input: 10,
      output: 5,
      cache_read_input_tokens: 2,
      cache_creation_input_tokens: 0,
    });

    const tool = spans.find((s) => attrValue(s, "langfuse.observation.type") === "tool");
    expect(tool, "tool span 必须存在").toBeTruthy();
    expect(tool.parentSpanId, "tool 必须挂在 turn 下").toBe(turn.spanId);
    expect(tool.name).toBe("read_file");
  });

  it("marks the generation ERROR on provider 4xx response", async () => {
    const otlp = await runAndGetTrace(async (pi) => {
      await pi.emit("session_start");
      await pi.emit("before_agent_start", { prompt: "hi" });
      await pi.emit("turn_start", {});
      await pi.emit("before_provider_request", { requestId: "req-err", request: { messages: [] } });
      await pi.emit("after_provider_response", { requestId: "req-err", status: 429, error: "rate limited" });
      await pi.emit("turn_end", {});
      await pi.emit("agent_end", {});
    });
    const spans = collectSpans(otlp);
    const generation = spans.find((s) => attrValue(s, "langfuse.observation.type") === "generation");
    expect(attrValue(generation, "langfuse.observation.level"), "4xx 响应必须使 generation 记为 ERROR").toBe("ERROR");
    expect(attrValue(generation, "langfuse.observation.status_message")).toContain("rate limited");
  });

  it("marks failed tools ERROR with message and duration", async () => {
    const otlp = await runAndGetTrace((pi) => replayBasicRun(pi, { toolFails: true }));
    const spans = collectSpans(otlp);
    const tool = spans.find((s) => attrValue(s, "langfuse.observation.type") === "tool");
    expect(attrValue(tool, "langfuse.observation.level"), "失败工具必须记为 ERROR").toBe("ERROR");
    expect(attrValue(tool, "langfuse.observation.status_message")).toContain("boom failed");
    const metadata = JSON.parse(attrValue(tool, "langfuse.observation.metadata")!);
    expect(metadata.durationMs, "失败工具必须记录执行时长").toBeGreaterThanOrEqual(0);
    expect(metadata.isError).toBe(true);
  });

  it("synthesizes a fallback generation for a turn without provider events", async () => {
    const otlp = await runAndGetTrace(async (pi) => {
      await pi.emit("session_start");
      await pi.emit("before_agent_start", { prompt: "quick" });
      await pi.emit("turn_start", {});
      await pi.emit("turn_end", {
        message: {
          role: "assistant",
          content: [{ type: "text", text: "direct answer" }],
          usage: { input: 3, output: 2 },
        },
      });
      await pi.emit("agent_end", {});
    });
    const spans = collectSpans(otlp);
    const generation = spans.find((s) => attrValue(s, "langfuse.observation.type") === "generation");
    expect(generation, "无 provider 事件的 turn 必须合成 fallback generation").toBeTruthy();
    const metadata = JSON.parse(attrValue(generation, "langfuse.observation.metadata")!);
    expect(metadata.sourceEvent).toBe("turn_end");
    expect(attrValue(generation, "langfuse.observation.output")).toContain("direct answer");
  });

  it("records a session_compact marker span", async () => {
    const otlp = await runAndGetTrace(async (pi) => {
      await pi.emit("session_start");
      await pi.emit("before_agent_start", { prompt: "compact-run" });
      await pi.emit("session_compact", { reason: "context full" });
      await pi.emit("agent_end", {});
    });
    const spans = collectSpans(otlp);
    expect(
      spans.some((s) => s.name === "session_compact"),
      "session_compact 必须生成标记 span",
    ).toBe(true);
  });

  it("is idempotent for duplicated start/end event pairs", async () => {
    const otlp = await runAndGetTrace(async (pi) => {
      await pi.emit("session_start");
      await pi.emit("before_agent_start", { prompt: "dup" });
      await pi.emit("agent_start", { prompt: "dup" });
      await pi.emit("turn_start", {});
      await pi.emit("tool_execution_start", { toolCallId: "tc-dup", toolName: "bash" });
      await pi.emit("tool_call", { toolCallId: "tc-dup", toolName: "bash" });
      await pi.emit("tool_result", { toolCallId: "tc-dup", content: "ok" });
      await pi.emit("tool_execution_end", { toolCallId: "tc-dup", content: "ok" });
      await pi.emit("turn_end", {});
      await pi.emit("agent_end", {});
    });
    const spans = collectSpans(otlp);
    expect(spans.filter((s) => !s.parentSpanId), "重复 agent start 事件不得产生第二个 root").toHaveLength(1);
    expect(
      spans.filter((s) => attrValue(s, "langfuse.observation.type") === "tool"),
      "重复 tool 事件对不得产生重复观测",
    ).toHaveLength(1);
  });

  it("keeps two concurrent sessions in separate traces", async () => {
    const pi = await loadExtension();
    const ctxA = { sessionManager: { getSessionFile: () => "/tmp/a-session.jsonl" } };
    const ctxB = { sessionManager: { getSessionFile: () => "/tmp/b-session.jsonl" } };

    await pi.emit("session_start", {}, ctxA);
    await pi.emit("session_start", {}, ctxB);
    await pi.emit("before_agent_start", { prompt: "from A" }, ctxA);
    await pi.emit("before_agent_start", { prompt: "from B" }, ctxB);
    await pi.emit("tool_execution_start", { toolCallId: "a-1", toolName: "toolA" }, ctxA);
    await pi.emit("tool_result", { toolCallId: "a-1", content: "done" }, ctxA);
    await pi.emit("agent_end", {}, ctxA);
    await pi.emit("agent_end", {}, ctxB);

    expect(requests, "两个 session 必须各产出一条独立 trace").toHaveLength(2);
    const [traceA, traceB] = requests.map((r) => collectSpans(r.body));
    const rootA = traceA.find((s) => !s.parentSpanId);
    const rootB = traceB.find((s) => !s.parentSpanId);
    expect(attrValue(rootA, "session.id")).toBe("a-session");
    expect(attrValue(rootB, "session.id")).toBe("b-session");
    expect(rootA.traceId, "两个 session 的 traceId 必须不同").not.toBe(rootB.traceId);

    const metaA = JSON.parse(attrValue(rootA, "langfuse.observation.metadata")!);
    const metaB = JSON.parse(attrValue(rootB, "langfuse.observation.metadata")!);
    expect(metaA.tool_call_count, "session A 的工具计数只统计自己").toBe(1);
    expect(metaB.tool_call_count, "session B 不得被 session A 的计数污染").toBe(0);
  });

  it("swallows handler errors and keeps processing later events", async () => {
    const poisoned: Record<string, unknown> = {};
    Object.defineProperty(poisoned, "request", {
      get() {
        throw new Error("poisoned event field");
      },
      enumerable: true,
    });

    const otlp = await runAndGetTrace(async (pi) => {
      await pi.emit("session_start");
      await pi.emit("before_agent_start", { prompt: "resilient" });
      await expect(
        pi.emit("before_provider_request", poisoned),
        "处理器内部异常不得向 Pi 抛出",
      ).resolves.not.toThrow();
      await pi.emit("tool_execution_start", { toolCallId: "tc-after", toolName: "bash" });
      await pi.emit("tool_result", { toolCallId: "tc-after", content: "still works" });
      await pi.emit("agent_end", {});
    });
    const spans = collectSpans(otlp);
    expect(
      spans.some((s) => attrValue(s, "langfuse.observation.type") === "tool"),
      "异常事件之后的事件仍必须被处理",
    ).toBe(true);
  });
});

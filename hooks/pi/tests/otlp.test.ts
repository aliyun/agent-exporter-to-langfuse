import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { emitRun } from "../src/emit.ts";
import { buildOtlpJson } from "../src/otlp.ts";
import { clearAllSessionStates } from "../src/state.ts";
import { loadExtension, replayBasicRun } from "./fake-pi.ts";

const HERE = dirname(fileURLToPath(import.meta.url));

function collectSpans(otlp: Record<string, unknown>): any[] {
  return (otlp.resourceSpans as any[]).flatMap((rs) =>
    (rs.scopeSpans as any[]).flatMap((ss) => ss.spans as any[]),
  );
}

describe("OTLP JSON structure", () => {
  let captured: Record<string, unknown> | null;
  const savedEnv = { ...process.env };

  beforeEach(() => {
    clearAllSessionStates();
    captured = null;
    // Sandbox HOME so a locally installed pi.env with real credentials never leaks in.
    process.env.HOME = mkdtempSync(join(tmpdir(), "pi-home-"));
    process.env.LANGSTASH_ENABLED = "true";
    process.env.LANGSTASH_URL = "http://127.0.0.1:15288";
    vi.stubGlobal("fetch", async (_url: string, options: any) => {
      captured = JSON.parse(options.body);
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

  it("satisfies the exporter validate_otlp structural contract", async () => {
    const pi = await loadExtension("/tmp/sessions/otlp-structure.jsonl");
    await replayBasicRun(pi);
    expect(captured, "agent_end 必须触发一次投递").toBeTruthy();

    const otlp = captured!;
    expect(Array.isArray(otlp.resourceSpans)).toBe(true);
    const scope = (otlp.resourceSpans as any[])[0].scopeSpans[0].scope;
    expect(scope.name).toBe("agent-exporter-to-langfuse");

    const spans = collectSpans(otlp);
    expect(spans.length).toBeGreaterThanOrEqual(4);

    const roots = spans.filter((s) => !s.parentSpanId);
    expect(roots, "必须含且仅含一个 root span").toHaveLength(1);

    const spanIds = new Set(spans.map((s) => s.spanId));
    for (const span of spans) {
      expect(span.traceId, `span ${span.name} 的 traceId 必须是 32 位 hex`).toMatch(/^[0-9a-f]{32}$/);
      expect(span.spanId, `span ${span.name} 的 spanId 必须是 16 位 hex`).toMatch(/^[0-9a-f]{16}$/);
      expect(span.startTimeUnixNano, "startTimeUnixNano 必须是纯数字字符串").toMatch(/^\d+$/);
      expect(span.endTimeUnixNano, "endTimeUnixNano 必须是纯数字字符串").toMatch(/^\d+$/);
      expect(
        BigInt(span.endTimeUnixNano) >= BigInt(span.startTimeUnixNano),
        `span ${span.name} 的 endTime 必须 >= startTime`,
      ).toBe(true);
      expect(Array.isArray(span.attributes)).toBe(true);
      for (const attr of span.attributes) {
        expect(attr, "attribute 必须是含 key/value 的 KeyValue").toHaveProperty("key");
        expect(attr).toHaveProperty("value");
      }
      if (span.parentSpanId) {
        expect(spanIds.has(span.parentSpanId), `span ${span.name} 的 parentSpanId 必须指向本 trace 内 span`).toBe(true);
      }
      expect(span.spanId).not.toBe(span.parentSpanId);
    }

    // Persist a sample payload for the cross-language validate_otlp() check.
    const artifactDir = join(HERE, "artifacts");
    mkdirSync(artifactDir, { recursive: true });
    writeFileSync(join(artifactDir, "sample-otlp.json"), JSON.stringify(otlp, null, 2));
  });

  it("redacts fake credentials and truncates overlong tool input in the delivered trace", async () => {
    const pi = await loadExtension("/tmp/sessions/otlp-redact.jsonl");
    await pi.emit("session_start");
    await pi.emit("before_agent_start", { prompt: "redact-run" });
    await pi.emit("tool_execution_start", {
      toolCallId: "tc-secret",
      toolName: "bash",
      input: {
        command: "curl -H 'Authorization: Bearer abcdefghijklmnop123456' https://x",
        key: "sk-lf-veryfakesecret000",
        blob: "y".repeat(60_000),
      },
    });
    await pi.emit("tool_result", { toolCallId: "tc-secret", content: "done" });
    await pi.emit("agent_end", {});

    const serialized = JSON.stringify(captured);
    expect(serialized, "伪造凭据不得出现在投递内容中").not.toContain("sk-lf-veryfakesecret000");
    expect(serialized, "Bearer token 不得出现在投递内容中").not.toContain("abcdefghijklmnop123456");
    expect(serialized).toContain("[REDACTED_SECRET]");
    expect(serialized, "超长字符串必须被截断").toContain("[truncated]");
    expect(serialized).not.toContain("y".repeat(30_000));
  });

  it("emitRun calls deliverTrace with the built payload via injected fetchFn (Tier 1)", async () => {
    const calls: Array<{ url: string; body: string }> = [];
    const run = {
      traceId: "a".repeat(32),
      root: undefined as any,
      spans: [
        {
          spanId: "b".repeat(16),
          name: "pi-agent",
          type: "agent" as const,
          startTimeMs: 1000,
          endTimeMs: 2000,
        },
      ],
      activeGenerations: new Map(),
      generationOrder: [],
      activeTools: new Map(),
      generationSeq: 0,
      cwd: "/tmp",
      emitted: false,
    };
    run.root = run.spans[0];

    const result = await emitRun(run as any, {
      sessionId: "emit-session",
      fetchFn: async (url, options) => {
        calls.push({ url, body: options.body });
        return { ok: true, status: 202 };
      },
    });

    expect(result.delivered, "Tier 1 成功时 emitRun 必须报告 delivered").toBe(true);
    expect(calls, "注入的 fetchFn 必须收到恰好一次 Tier 1 请求").toHaveLength(1);
    expect(calls[0].url, "Tier 1 必须 POST 到本地 exporter 的 /ingest").toContain("/ingest");
    const body = JSON.parse(calls[0].body);
    expect(collectSpans(body)[0].traceId).toBe("a".repeat(32));

    const second = await emitRun(run as any, { fetchFn: async () => ({ ok: true, status: 202 }) });
    expect(second.delivered, "同一 run 不得二次投递").toBe(false);
    expect(calls).toHaveLength(1);
  });

  it("buildOtlpJson writes trace-level attributes on the root only", () => {
    const root = {
      spanId: "c".repeat(16),
      name: "pi-agent",
      type: "agent" as const,
      startTimeMs: 1000,
      endTimeMs: 3000,
      input: { prompt: "p" },
      output: "answer",
    };
    const child = {
      spanId: "d".repeat(16),
      parentSpanId: root.spanId,
      name: "turn",
      type: "span" as const,
      startTimeMs: 1100,
      endTimeMs: 2900,
    };
    const otlp = buildOtlpJson(
      {
        traceId: "e".repeat(32),
        root: root as any,
        spans: [root, child] as any,
        activeGenerations: new Map(),
        generationOrder: [],
        activeTools: new Map(),
        generationSeq: 0,
        cwd: "/tmp",
        emitted: false,
      },
      { sessionId: "sess", userId: "u1", tags: ["pi"] },
    );

    const spans = collectSpans(otlp);
    const rootSpan = spans.find((s) => !s.parentSpanId);
    const childSpan = spans.find((s) => s.parentSpanId);
    const keys = (s: any) => (s.attributes as any[]).map((a) => a.key);
    expect(keys(rootSpan)).toContain("langfuse.trace.name");
    expect(keys(rootSpan)).toContain("user.id");
    expect(keys(childSpan)).not.toContain("langfuse.trace.name");
    expect(keys(childSpan)).not.toContain("user.id");
  });
});

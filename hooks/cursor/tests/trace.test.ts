import { describe, expect, it, vi } from "vitest";
import type { Config } from "../src/config.js";
import type { StateRecord, Turn } from "../src/types.js";
import { buildOtlpJson, deliverTurn, type DeliverFn } from "../src/trace.js";

function makeConfig(overrides?: Partial<Config>): Config {
  return {
    enabled: true,
    base_url: "https://us.cloud.langfuse.com",
    max_chars: 800_000,
    debug: false,
    fail_on_error: false,
    langstash_enabled: true,
    langstash_url: "http://127.0.0.1:5288",
    langstash_timeout: 10,
    tags: ["cursor"],
    ...overrides,
  };
}

function makeTurn(overrides?: Partial<Turn>): Turn {
  return {
    conversationId: "conv-1",
    events: [],
    userInput: "hello",
    finalOutput: "hi there",
    model: "gpt-4",
    startTime: "2026-01-01T00:00:00.000Z",
    endTime: "2026-01-01T00:01:00.000Z",
    userEmail: "user@test.com",
    cursorStatus: "completed",
    ...overrides,
  };
}

type OtlpAttribute = {
  key: string;
  value:
    | { stringValue: string }
    | { intValue: string }
    | { boolValue: boolean }
    | { doubleValue: number };
};

type OtlpSpan = {
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  name: string;
  startTimeUnixNano: string;
  endTimeUnixNano: string;
  attributes: OtlpAttribute[];
};

function getSpans(result: Record<string, unknown>): OtlpSpan[] {
  const rs = result.resourceSpans as Array<{
    scopeSpans: Array<{ scope: { name: string }; spans: OtlpSpan[] }>;
  }>;
  return rs[0].scopeSpans[0].spans;
}

function getAttr(span: OtlpSpan, key: string): OtlpAttribute | undefined {
  return span.attributes.find((a) => a.key === key);
}

function getStringAttr(span: OtlpSpan, key: string): string | undefined {
  const attr = getAttr(span, key);
  if (!attr) return undefined;
  return (attr.value as { stringValue: string }).stringValue;
}

describe("buildOtlpJson", () => {
  it("returns correct OTLP JSON structure with resourceSpans/scopeSpans", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig(), "Cursor - Turn 1");
    expect(result.resourceSpans, "resourceSpans should exist").toBeDefined();
    const rs = result.resourceSpans as Array<{
      scopeSpans: Array<{ scope: { name: string }; spans: unknown[] }>;
    }>;
    expect(rs, "should have 1 resourceSpan").toHaveLength(1);
    expect(rs[0].scopeSpans, "should have 1 scopeSpan").toHaveLength(1);
    expect(rs[0].scopeSpans[0].scope.name, "scope name should be agent-exporter-to-langfuse").toBe("agent-exporter-to-langfuse");
  });

  it("creates root span named 'Cursor - Turn N' with session.id and trace.name", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig(), "Cursor - Turn 1");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId);
    expect(root, "root span should exist (no parentSpanId)").toBeDefined();
    expect(root!.name, "root span name should be 'Cursor - Turn 1'").toBe("Cursor - Turn 1");
    expect(getStringAttr(root!, "langfuse.trace.name"), "trace.name attr").toBe("Cursor - Turn 1");
    expect(getStringAttr(root!, "session.id"), "session.id attr should be conversation_id").toBe("conv-1");
  });

  it("sets user.id from user_email when present", () => {
    const result = buildOtlpJson(makeTurn({ userEmail: "alice@test.com" }), makeConfig(), "trace");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId)!;
    expect(getStringAttr(root, "user.id"), "user.id should come from user_email").toBe("alice@test.com");
  });

  it("falls back to config.user_id when user_email absent", () => {
    const result = buildOtlpJson(makeTurn({ userEmail: undefined }), makeConfig({ user_id: "bob" }), "trace");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId)!;
    expect(getStringAttr(root, "user.id"), "user.id should fall back to config.user_id").toBe("bob");
  });

  it("includes tags as JSON array string", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig({ tags: ["cursor", "prod"] }), "trace");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId)!;
    expect(getStringAttr(root, "langfuse.trace.tags"), "tags should be JSON array string").toBe(JSON.stringify(["cursor", "prod"]));
  });

  it("sets langfuse.observation.input/output on root span", () => {
    const result = buildOtlpJson(makeTurn({ userInput: "what is 2+2?", finalOutput: "4" }), makeConfig(), "trace");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId)!;
    const input = getStringAttr(root, "langfuse.observation.input");
    const output = getStringAttr(root, "langfuse.observation.output");
    expect(input, "root input should exist").toBeDefined();
    expect(JSON.parse(input!), "root input should contain prompt").toHaveProperty("content", "what is 2+2?");
    expect(output, "root output should exist").toBeDefined();
    expect(JSON.parse(output!), "root output should contain response").toHaveProperty("content", "4");
  });

  it("creates generation span with type=generation and model.name", () => {
    const result = buildOtlpJson(makeTurn({ model: "gpt-4o" }), makeConfig(), "trace");
    const spans = getSpans(result);
    const gen = spans.find((s) => getStringAttr(s, "langfuse.observation.type") === "generation");
    expect(gen, "generation span should exist").toBeDefined();
    expect(getStringAttr(gen!, "langfuse.observation.model.name"), "model.name should be gpt-4o").toBe("gpt-4o");
  });

  it("generation span parentSpanId points to root span", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig(), "trace");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId)!;
    const gen = spans.find((s) => getStringAttr(s, "langfuse.observation.type") === "generation")!;
    expect(gen.parentSpanId, "generation parentSpanId should be root spanId").toBe(root.spanId);
  });

  it("all spans share the same traceId (32-hex)", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig(), "trace");
    const spans = getSpans(result);
    const traceIds = new Set(spans.map((s) => s.traceId));
    expect(traceIds.size, "all spans should share one traceId").toBe(1);
    const traceId = spans[0].traceId;
    expect(traceId, "traceId should be 32-char hex").toMatch(/^[0-9a-f]{32}$/);
  });

  it("spanId is 16-hex", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig(), "trace");
    const spans = getSpans(result);
    for (const span of spans) {
      expect(span.spanId, `spanId should be 16-char hex (got: ${span.spanId})`).toMatch(/^[0-9a-f]{16}$/);
    }
  });

  it("timestamps are string nanoseconds", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig(), "trace");
    const spans = getSpans(result);
    for (const span of spans) {
      expect(typeof span.startTimeUnixNano, "startTimeUnixNano should be string").toBe("string");
      expect(typeof span.endTimeUnixNano, "endTimeUnixNano should be string").toBe("string");
      expect(() => BigInt(span.startTimeUnixNano), "startTimeUnixNano should be valid BigInt").not.toThrow();
      expect(() => BigInt(span.endTimeUnixNano), "endTimeUnixNano should be valid BigInt").not.toThrow();
    }
  });

  it("does not contain gen_ai.* attributes", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig(), "trace");
    const spans = getSpans(result);
    for (const span of spans) {
      for (const attr of span.attributes) {
        expect(attr.key, `should not have gen_ai.* attribute: ${attr.key}`).not.toMatch(/^gen_ai\./);
      }
    }
  });

  it("does not set langfuse.observation.usage_details", () => {
    const result = buildOtlpJson(makeTurn(), makeConfig(), "trace");
    const spans = getSpans(result);
    for (const span of spans) {
      expect(getAttr(span, "langfuse.observation.usage_details"), "should not have usage_details (Cursor has no token usage)").toBeUndefined();
    }
  });

  it("creates tool spans for paired shell before/after events", () => {
    const events: StateRecord[] = [
      { hook_event_name: "beforeShellExecution", generation_id: "g1", timestamp: "2026-01-01T00:00:01.000Z", conversation_id: "c1", command: "ls", cwd: "/tmp" },
      { hook_event_name: "afterShellExecution", generation_id: "g1", timestamp: "2026-01-01T00:00:02.000Z", conversation_id: "c1", command: "ls", output: "file.txt", duration: 50 },
    ];
    const turn = makeTurn({ events });
    const result = buildOtlpJson(turn, makeConfig(), "trace");
    const spans = getSpans(result);
    const toolSpans = spans.filter((s) => getStringAttr(s, "langfuse.observation.type") === "tool");
    expect(toolSpans, "paired before/after should produce 1 tool span").toHaveLength(1);
    expect(toolSpans[0].name, "tool span name should be command").toBe("ls");
    const input = getStringAttr(toolSpans[0], "langfuse.observation.input");
    expect(JSON.parse(input!), "tool input should have command").toHaveProperty("command", "ls");
  });

  it("unpaired before produces tool span with empty output", () => {
    const events: StateRecord[] = [
      { hook_event_name: "beforeShellExecution", generation_id: "g1", timestamp: "2026-01-01T00:00:01.000Z", conversation_id: "c1", command: "ls", cwd: "/tmp" },
    ];
    const turn = makeTurn({ events });
    const result = buildOtlpJson(turn, makeConfig(), "trace");
    const spans = getSpans(result);
    const toolSpans = spans.filter((s) => getStringAttr(s, "langfuse.observation.type") === "tool");
    expect(toolSpans, "unpaired before should still produce 1 tool span").toHaveLength(1);
    expect(getStringAttr(toolSpans[0], "langfuse.observation.output"), "unpaired before output should be empty or absent").toBeFalsy();
  });

  it("creates separate tool spans for file_read and file_edit", () => {
    const events: StateRecord[] = [
      { hook_event_name: "beforeReadFile", generation_id: "g1", timestamp: "2026-01-01T00:00:01.000Z", conversation_id: "c1", file_path: "/tmp/a", content: "content-a" },
      { hook_event_name: "afterFileEdit", generation_id: "g1", timestamp: "2026-01-01T00:00:02.000Z", conversation_id: "c1", file_path: "/tmp/b", edits: [{ old: "x", new: "y" }] },
    ];
    const turn = makeTurn({ events });
    const result = buildOtlpJson(turn, makeConfig(), "trace");
    const spans = getSpans(result);
    const toolSpans = spans.filter((s) => getStringAttr(s, "langfuse.observation.type") === "tool");
    expect(toolSpans, "should have 2 tool spans for file events").toHaveLength(2);
    expect(toolSpans.some((s) => s.name === "file_read"), "should have file_read span").toBe(true);
    expect(toolSpans.some((s) => s.name === "file_edit"), "should have file_edit span").toBe(true);
  });

  it("tool span parentSpanId points to generation span", () => {
    const events: StateRecord[] = [
      { hook_event_name: "beforeShellExecution", generation_id: "g1", timestamp: "2026-01-01T00:00:01.000Z", conversation_id: "c1", command: "ls" },
      { hook_event_name: "afterShellExecution", generation_id: "g1", timestamp: "2026-01-01T00:00:02.000Z", conversation_id: "c1", output: "ok" },
    ];
    const turn = makeTurn({ events });
    const result = buildOtlpJson(turn, makeConfig(), "trace");
    const spans = getSpans(result);
    const gen = spans.find((s) => getStringAttr(s, "langfuse.observation.type") === "generation")!;
    const tool = spans.find((s) => getStringAttr(s, "langfuse.observation.type") === "tool")!;
    expect(tool.parentSpanId, "tool parentSpanId should be generation spanId").toBe(gen.spanId);
  });

  it("sets cursor_status metadata on root span", () => {
    const result = buildOtlpJson(makeTurn({ cursorStatus: "completed" }), makeConfig(), "trace");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId)!;
    expect(getStringAttr(root, "langfuse.observation.metadata.cursor_status"), "cursor_status should be completed").toBe("completed");
  });

  it("sets cursor_status=unknown for recovery", () => {
    const result = buildOtlpJson(makeTurn({ cursorStatus: "unknown" }), makeConfig(), "trace");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId)!;
    expect(getStringAttr(root, "langfuse.observation.metadata.cursor_status"), "cursor_status should be unknown for recovery").toBe("unknown");
  });
});

describe("deliverTurn", () => {
  it("calls deliverFn with OTLP JSON and returns true on success", async () => {
    let calledOtlp: Record<string, unknown> | null = null;
    const mockDeliver: DeliverFn = async (otlp) => {
      calledOtlp = otlp;
      return true;
    };
    const delivered = await deliverTurn(makeTurn(), makeConfig(), "Cursor - Turn 1", mockDeliver);
    expect(delivered, "deliverTurn should return true on success").toBe(true);
    expect(calledOtlp, "deliverFn should be called with OTLP JSON").not.toBeNull();
    expect(calledOtlp!.resourceSpans, "OTLP JSON should have resourceSpans").toBeDefined();
  });

  it("returns false when deliverFn returns false", async () => {
    const mockDeliver: DeliverFn = async () => false;
    const delivered = await deliverTurn(makeTurn(), makeConfig(), "trace", mockDeliver);
    expect(delivered, "deliverTurn should return false when delivery fails").toBe(false);
  });

  it("2-turn session produces 2 deliverTrace calls with independent traceId", async () => {
    const calls: Record<string, unknown>[] = [];
    const mockDeliver: DeliverFn = async (otlp) => {
      calls.push(otlp);
      return true;
    };

    const turn1 = makeTurn({ startTime: "2026-01-01T00:00:00.000Z", endTime: "2026-01-01T00:01:00.000Z" });
    const turn2 = makeTurn({ startTime: "2026-01-01T00:02:00.000Z", endTime: "2026-01-01T00:03:00.000Z" });

    await deliverTurn(turn1, makeConfig(), "Cursor - Turn 1", mockDeliver);
    await deliverTurn(turn2, makeConfig(), "Cursor - Turn 2", mockDeliver);

    expect(calls, "should have 2 deliverTrace calls").toHaveLength(2);

    const spans1 = getSpans(calls[0]);
    const spans2 = getSpans(calls[1]);
    const traceId1 = spans1[0].traceId;
    const traceId2 = spans2[0].traceId;
    expect(traceId1, "turn 1 traceId should be 32-hex").toMatch(/^[0-9a-f]{32}$/);
    expect(traceId2, "turn 2 traceId should be 32-hex").toMatch(/^[0-9a-f]{32}$/);
    expect(traceId1, "traceIds should be independent (different)").not.toBe(traceId2);

    const root1 = spans1.find((s) => !s.parentSpanId)!;
    const root2 = spans2.find((s) => !s.parentSpanId)!;
    expect(root1.name, "turn 1 root span name").toBe("Cursor - Turn 1");
    expect(root2.name, "turn 2 root span name").toBe("Cursor - Turn 2");

    const sid1 = getStringAttr(root1, "session.id");
    const sid2 = getStringAttr(root2, "session.id");
    expect(sid1, "turn 1 session.id").toBe("conv-1");
    expect(sid2, "turn 2 session.id").toBe("conv-1");
    expect(sid1, "both turns should have same session.id").toBe(sid2);
  });
});

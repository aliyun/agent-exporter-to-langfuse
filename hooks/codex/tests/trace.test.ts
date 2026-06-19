import { describe, expect, it } from "vitest";
import { buildOtlpJson } from "../src/trace.js";
import type { Config } from "../src/config.js";
import type { SessionMeta, Turn } from "../src/types.js";

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
    ...overrides,
  };
}

function makeSessionMeta(overrides?: Partial<SessionMeta>): SessionMeta {
  return {
    sessionId: "sess-1",
    threadId: "thread-1",
    isSubagent: false,
    ...overrides,
  };
}

function makeTurn(overrides?: Partial<Turn>): Turn {
  const now = Date.now();
  return {
    startTime: now,
    endTime: now + 1000,
    steps: [],
    subagentThreadIds: [],
    completed: true,
    aborted: false,
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
    const turn = makeTurn({
      turnId: "turn-1",
      model: "gpt-4o",
      userInput: "hello",
      finalOutput: "hi there",
      steps: [
        {
          startTime: 1000,
          endTime: 2000,
          text: "hi there",
          toolCalls: [],
        },
      ],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "test-trace");

    expect(result.resourceSpans).toBeDefined();
    const rs = result.resourceSpans as Array<{
      scopeSpans: Array<{ scope: { name: string }; spans: unknown[] }>;
    }>;
    expect(rs).toHaveLength(1);
    expect(rs[0].scopeSpans).toHaveLength(1);
    expect(rs[0].scopeSpans[0].scope.name).toBe("agent-exporter-to-langfuse");
  });

  it("creates root span with langfuse.trace.name and session.id", () => {
    const turn = makeTurn({
      turnId: "turn-1",
      model: "gpt-4o",
      userInput: "What is 2+2?",
      finalOutput: "4",
      steps: [{ startTime: 1000, endTime: 5000, text: "4", toolCalls: [] }],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "my-trace");
    const spans = getSpans(result);

    // Root span has no parentSpanId
    const root = spans.find((s) => !s.parentSpanId);
    expect(root).toBeDefined();
    expect(root!.name).toBe("my-trace");
    expect(getStringAttr(root!, "langfuse.trace.name")).toBe("my-trace");
    expect(getStringAttr(root!, "session.id")).toBe("sess-1");
  });

  it("creates a generation span per step with correct attributes", () => {
    const turn = makeTurn({
      model: "gpt-4o",
      steps: [
        { startTime: 1000, endTime: 2000, text: "step1", toolCalls: [] },
        { startTime: 2000, endTime: 3000, text: "step2", toolCalls: [] },
      ],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);

    const generations = spans.filter(
      (s) => getStringAttr(s, "langfuse.observation.type") === "generation",
    );
    expect(generations).toHaveLength(2);
    expect(generations[0].name).toBe("gpt-4o");
    expect(getStringAttr(generations[0], "langfuse.observation.model.name")).toBe("gpt-4o");
  });

  it("creates tool spans under generation spans", () => {
    const turn = makeTurn({
      model: "gpt-4o",
      steps: [
        {
          startTime: 1000,
          endTime: 3000,
          toolCalls: [
            {
              callId: "call-1",
              name: "shell",
              args: { cmd: "ls" },
              startTime: 1500,
              endTime: 2500,
              output: "file.txt",
            },
          ],
          text: "done",
        },
      ],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);

    const toolSpans = spans.filter(
      (s) => getStringAttr(s, "langfuse.observation.type") === "tool",
    );
    expect(toolSpans).toHaveLength(1);
    expect(toolSpans[0].name).toBe("shell");
    expect(getStringAttr(toolSpans[0], "langfuse.observation.input")).toBe(
      JSON.stringify({ cmd: "ls" }),
    );

    // Tool span's parentSpanId should be the generation span
    const genSpans = spans.filter(
      (s) => getStringAttr(s, "langfuse.observation.type") === "generation",
    );
    expect(genSpans).toHaveLength(1);
    expect(toolSpans[0].parentSpanId).toBe(genSpans[0].spanId);
  });

  it("generation span's parentSpanId points to root span", () => {
    const turn = makeTurn({
      model: "gpt-4o",
      steps: [{ startTime: 1000, endTime: 2000, text: "reply", toolCalls: [] }],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);

    const root = spans.find((s) => !s.parentSpanId);
    const gen = spans.find(
      (s) => getStringAttr(s, "langfuse.observation.type") === "generation",
    );
    expect(gen).toBeDefined();
    expect(gen!.parentSpanId).toBe(root!.spanId);
  });

  it("includes user.id when set in config", () => {
    const turn = makeTurn({ steps: [] });
    const result = buildOtlpJson(
      turn,
      makeSessionMeta(),
      makeConfig({ user_id: "test-user" }),
      "trace",
    );
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId);
    expect(getStringAttr(root!, "user.id")).toBe("test-user");
  });

  it("omits user.id when not set in config", () => {
    const turn = makeTurn({ steps: [] });
    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId);
    expect(getAttr(root!, "user.id")).toBeUndefined();
  });

  it("includes tags as JSON array string", () => {
    const turn = makeTurn({ steps: [] });
    const result = buildOtlpJson(
      turn,
      makeSessionMeta(),
      makeConfig({ tags: ["codex", "prod"] }),
      "trace",
    );
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId);
    expect(getStringAttr(root!, "langfuse.trace.tags")).toBe(JSON.stringify(["codex", "prod"]));
  });

  it("omits tags when config.tags is empty", () => {
    const turn = makeTurn({ steps: [] });
    const result = buildOtlpJson(
      turn,
      makeSessionMeta(),
      makeConfig({ tags: [] }),
      "trace",
    );
    const spans = getSpans(result);
    const root = spans.find((s) => !s.parentSpanId);
    expect(getAttr(root!, "langfuse.trace.tags")).toBeUndefined();
  });

  it("includes usage_details in generation span", () => {
    const turn = makeTurn({
      steps: [
        {
          startTime: 1000,
          endTime: 2000,
          text: "reply",
          toolCalls: [],
          usage: { input_tokens: 100, output_tokens: 50, cached_input_tokens: 20 },
        },
      ],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);
    const gen = spans.find(
      (s) => getStringAttr(s, "langfuse.observation.type") === "generation",
    );
    expect(gen).toBeDefined();
    const usageStr = getStringAttr(gen!, "langfuse.observation.usage_details");
    expect(usageStr).toBeDefined();
    const usage = JSON.parse(usageStr!);
    expect(usage.input).toBe(100);
    expect(usage.output).toBe(50);
    expect(usage.cache_read_input_tokens).toBe(20);
  });

  it("all spans share the same traceId", () => {
    const turn = makeTurn({
      model: "gpt-4o",
      steps: [
        {
          startTime: 1000,
          endTime: 3000,
          text: "done",
          toolCalls: [
            {
              callId: "call-1",
              name: "shell",
              args: {},
              startTime: 1500,
              endTime: 2500,
              output: "ok",
            },
          ],
        },
      ],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);

    const traceIds = new Set(spans.map((s) => s.traceId));
    expect(traceIds.size).toBe(1);
  });

  it("uses parent context traceId and sets root parentSpanId when parentContext provided", () => {
    const turn = makeTurn({
      model: "gpt-4o",
      steps: [{ startTime: 1000, endTime: 2000, text: "reply", toolCalls: [] }],
    });

    const parentContext = {
      traceId: "abcdef0123456789abcdef0123456789",
      spanId: "1234567890abcdef",
    };

    const result = buildOtlpJson(
      turn,
      makeSessionMeta(),
      makeConfig(),
      "subagent",
      parentContext,
    );
    const spans = getSpans(result);

    // All spans should use parent traceId
    for (const span of spans) {
      expect(span.traceId).toBe("abcdef0123456789abcdef0123456789");
    }

    // Root span should have parent's spanId as parentSpanId
    const root = spans.find((s) => s.name === "subagent");
    expect(root).toBeDefined();
    expect(root!.parentSpanId).toBe("1234567890abcdef");
  });

  it("has no tool spans when there are no tool calls", () => {
    const turn = makeTurn({
      steps: [{ startTime: 1000, endTime: 2000, text: "just text", toolCalls: [] }],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);
    const toolSpans = spans.filter(
      (s) => getStringAttr(s, "langfuse.observation.type") === "tool",
    );
    expect(toolSpans).toHaveLength(0);
  });

  it("timestamps are string nanoseconds", () => {
    const turn = makeTurn({
      startTime: 1000,
      endTime: 5000,
      steps: [{ startTime: 1000, endTime: 5000, text: "reply", toolCalls: [] }],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);

    for (const span of spans) {
      expect(typeof span.startTimeUnixNano).toBe("string");
      expect(typeof span.endTimeUnixNano).toBe("string");
      // Should be parseable as BigInt
      expect(() => BigInt(span.startTimeUnixNano)).not.toThrow();
      expect(() => BigInt(span.endTimeUnixNano)).not.toThrow();
    }
  });

  it("does not leak internal __parent_ attributes", () => {
    const turn = makeTurn({
      model: "gpt-4o",
      steps: [
        {
          startTime: 1000,
          endTime: 3000,
          text: "done",
          toolCalls: [
            {
              callId: "call-1",
              name: "shell",
              args: {},
              startTime: 1500,
              endTime: 2500,
              output: "ok",
            },
          ],
        },
      ],
    });

    const result = buildOtlpJson(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = getSpans(result);

    for (const span of spans) {
      for (const attr of span.attributes) {
        expect(attr.key).not.toMatch(/^__parent_/);
      }
    }
  });
});

import { describe, expect, it, vi } from "vitest";
import { buildTraceV2 } from "../src/langstash.js";
import type { Config } from "../src/config.js";
import type { SessionMeta, Turn } from "../src/types.js";

vi.mock("node:crypto", () => ({
  randomUUID: () => "00000000-0000-0000-0000-000000000000",
}));

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

describe("buildTraceV2", () => {
  it("returns correct top-level structure", () => {
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

    const result = buildTraceV2(turn, makeSessionMeta(), makeConfig(), "test-trace");

    expect(result.schema_version).toBe("2");
    expect(result.id).toBe("00000000-0000-0000-0000-000000000000");
    expect(result.source).toBe("codex");
    expect(result.session_id).toBe("sess-1");
    expect(result.trace).toBeDefined();
    expect(result.generations).toBeDefined();
    expect(result.spans).toBeDefined();
  });

  it("populates trace with name, start_time, end_time, input, output", () => {
    const turn = makeTurn({
      turnId: "turn-1",
      startTime: 1000,
      endTime: 5000,
      userInput: "What is 2+2?",
      finalOutput: "4",
      steps: [{ startTime: 1000, endTime: 5000, text: "4", toolCalls: [] }],
    });

    const result = buildTraceV2(turn, makeSessionMeta(), makeConfig(), "my-trace");
    const trace = result.trace as Record<string, unknown>;

    expect(trace.name).toBe("my-trace");
    expect(trace.start_time).toBe(new Date(1000).toISOString());
    expect(trace.end_time).toBe(new Date(5000).toISOString());
    expect(trace.input).toEqual({ role: "user", content: "What is 2+2?" });
    expect(trace.output).toEqual({ role: "assistant", content: "4" });
  });

  it("generates a generation entry per step", () => {
    const turn = makeTurn({
      model: "gpt-4o",
      steps: [
        { startTime: 1000, endTime: 2000, text: "step1", toolCalls: [] },
        { startTime: 2000, endTime: 3000, text: "step2", toolCalls: [] },
      ],
    });

    const result = buildTraceV2(turn, makeSessionMeta(), makeConfig(), "trace");
    const generations = result.generations as Record<string, unknown>[];

    expect(generations).toHaveLength(2);
    expect(generations[0].model).toBe("gpt-4o");
    expect((generations[0].metadata as Record<string, unknown>)["codex.step_index"]).toBe(0);
    expect((generations[1].metadata as Record<string, unknown>)["codex.step_index"]).toBe(1);
  });

  it("generates spans for tool calls", () => {
    const turn = makeTurn({
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

    const result = buildTraceV2(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = result.spans as Record<string, unknown>[];

    expect(spans).toHaveLength(1);
    expect(spans[0].name).toBe("shell");
    expect(spans[0].input).toEqual({ cmd: "ls" });
    expect(spans[0].output).toBe("file.txt");
    expect((spans[0].metadata as Record<string, unknown>)["codex.call_id"]).toBe("call-1");
  });

  it("does not include spans when there are no tool calls", () => {
    const turn = makeTurn({
      steps: [{ startTime: 1000, endTime: 2000, text: "just text", toolCalls: [] }],
    });

    const result = buildTraceV2(turn, makeSessionMeta(), makeConfig(), "trace");
    const spans = result.spans as unknown[];
    expect(spans).toHaveLength(0);
  });

  it("omits user_id when not set in config", () => {
    const turn = makeTurn({ steps: [] });
    const result = buildTraceV2(turn, makeSessionMeta(), makeConfig(), "trace");
    expect(result).not.toHaveProperty("user_id");
  });

  it("includes user_id when set in config", () => {
    const turn = makeTurn({ steps: [] });
    const result = buildTraceV2(
      turn,
      makeSessionMeta(),
      makeConfig({ user_id: "test-user" }),
      "trace",
    );
    expect(result.user_id).toBe("test-user");
  });

  it("includes tags when set in config", () => {
    const turn = makeTurn({ steps: [] });
    const result = buildTraceV2(
      turn,
      makeSessionMeta(),
      makeConfig({ tags: ["codex", "prod"] }),
      "trace",
    );
    expect(result.tags).toEqual(["codex", "prod"]);
  });

  it("omits tags when config.tags is empty", () => {
    const turn = makeTurn({ steps: [] });
    const result = buildTraceV2(
      turn,
      makeSessionMeta(),
      makeConfig({ tags: [] }),
      "trace",
    );
    expect(result).not.toHaveProperty("tags");
  });

  it("includes subagent metadata when session is a subagent", () => {
    const turn = makeTurn({
      steps: [{ startTime: 1000, endTime: 2000, toolCalls: [] }],
    });
    const meta = makeSessionMeta({
      isSubagent: true,
      parentThreadId: "parent-1",
      agentNickname: "researcher",
    });

    const result = buildTraceV2(turn, meta, makeConfig(), "trace");
    const traceMetadata = (result.trace as Record<string, unknown>).metadata as Record<
      string,
      unknown
    >;

    expect(traceMetadata["codex.is_subagent"]).toBe(true);
    expect(traceMetadata["codex.parent_thread_id"]).toBe("parent-1");
    expect(traceMetadata["codex.agent_nickname"]).toBe("researcher");
  });

  it("includes usage in generation when step has usage", () => {
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

    const result = buildTraceV2(turn, makeSessionMeta(), makeConfig(), "trace");
    const gen = (result.generations as Record<string, unknown>[])[0];
    expect(gen.usage).toEqual({
      input: 100,
      output: 50,
      cache_read_input_tokens: 20,
    });
  });

  it("includes tool call error in span metadata", () => {
    const turn = makeTurn({
      steps: [
        {
          startTime: 1000,
          endTime: 3000,
          toolCalls: [
            {
              callId: "call-err",
              name: "shell",
              args: { cmd: "fail" },
              startTime: 1500,
              endTime: 2500,
              error: "command not found",
            },
          ],
        },
      ],
    });

    const result = buildTraceV2(turn, makeSessionMeta(), makeConfig(), "trace");
    const span = (result.spans as Record<string, unknown>[])[0];
    const meta = span.metadata as Record<string, unknown>;
    expect(meta.error).toBe("command not found");
  });
});

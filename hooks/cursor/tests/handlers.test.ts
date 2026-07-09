import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Config } from "../src/config.js";
import type { CursorHookPayload } from "../src/types.js";
import { buildStateRecord, handleEvent } from "../src/handlers.js";
import { getStateFilePath, readStateRecords } from "../src/state.js";

let testHome: string;
let testCounter: number;

beforeEach(() => {
  testHome = mkdtempSync(join(tmpdir(), "cursor-hdl-"));
  vi.stubEnv("HOME", testHome);
  testCounter = 0;
});

afterEach(() => {
  rmSync(testHome, { recursive: true, force: true });
  vi.unstubAllEnvs();
});

function uniqueConvId(): string {
  return `conv-${testCounter++}`;
}

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

function makePayload(event: string, overrides?: Partial<CursorHookPayload>): CursorHookPayload {
  return {
    hook_event_name: event,
    conversation_id: "conv-1",
    generation_id: "g1",
    ...overrides,
  };
}

describe("buildStateRecord", () => {
  it("beforeSubmitPrompt records prompt/attachments/model", () => {
    const record = buildStateRecord(
      makePayload("beforeSubmitPrompt", { prompt: "hello", attachments: ["a.txt"], model: "gpt-4" }),
      makeConfig(),
    );
    expect(record.hook_event_name, "event name should be beforeSubmitPrompt").toBe("beforeSubmitPrompt");
    expect(record.prompt, "prompt field should be recorded").toBe("hello");
    expect(record.model, "model field should be recorded").toBe("gpt-4");
    expect(record.timestamp, "timestamp should be ISO 8601").toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });

  it("afterAgentResponse records text/model", () => {
    const record = buildStateRecord(
      makePayload("afterAgentResponse", { text: "response text", model: "gpt-4" }),
      makeConfig(),
    );
    expect(record.text, "text field should be recorded").toBe("response text");
    expect(record.model, "model field should be recorded").toBe("gpt-4");
  });

  it("afterAgentThought records text/duration_ms", () => {
    const record = buildStateRecord(
      makePayload("afterAgentThought", { text: "thinking...", duration_ms: 150 }),
      makeConfig(),
    );
    expect(record.text, "thought text should be recorded").toBe("thinking...");
    expect(record.duration_ms, "duration_ms should be recorded as number").toBe(150);
    expect(typeof record.duration_ms, "duration_ms should be a number").toBe("number");
  });

  it("beforeShellExecution records command/cwd", () => {
    const record = buildStateRecord(
      makePayload("beforeShellExecution", { command: "ls -la", cwd: "/tmp" }),
      makeConfig(),
    );
    expect(record.command, "command should be recorded").toBe("ls -la");
    expect(record.cwd, "cwd should be recorded").toBe("/tmp");
  });

  it("afterShellExecution records command/output/duration", () => {
    const record = buildStateRecord(
      makePayload("afterShellExecution", { command: "ls -la", output: "file.txt", duration: 100 }),
      makeConfig(),
    );
    expect(record.command, "command should be recorded").toBe("ls -la");
    expect(record.output, "output should be recorded").toBe("file.txt");
    expect(record.duration, "duration should be recorded as number").toBe(100);
    expect(typeof record.duration, "duration should be a number").toBe("number");
  });

  it("beforeMCPExecution records tool_name/tool_input/url/command", () => {
    const record = buildStateRecord(
      makePayload("beforeMCPExecution", { tool_name: "search", tool_input: { q: "test" }, url: "http://x", command: "search q=test" }),
      makeConfig(),
    );
    expect(record.tool_name, "tool_name should be recorded").toBe("search");
    expect(record.url, "url should be recorded").toBe("http://x");
  });

  it("afterMCPExecution records tool_name/tool_input/result_json/duration", () => {
    const record = buildStateRecord(
      makePayload("afterMCPExecution", { tool_name: "search", tool_input: { q: "test" }, result_json: '{"hit":1}', duration: 50 }),
      makeConfig(),
    );
    expect(record.tool_name, "tool_name should be recorded").toBe("search");
    expect(record.result_json, "result_json should be recorded").toBe('{"hit":1}');
    expect(record.duration, "duration should be recorded as number").toBe(50);
    expect(typeof record.duration, "duration should be a number").toBe("number");
  });

  it("beforeReadFile records file_path/content", () => {
    const record = buildStateRecord(
      makePayload("beforeReadFile", { file_path: "/tmp/a.txt", content: "file contents" }),
      makeConfig(),
    );
    expect(record.file_path, "file_path should be recorded").toBe("/tmp/a.txt");
    expect(record.content, "content should be recorded").toBe("file contents");
  });

  it("afterFileEdit records file_path/edits", () => {
    const record = buildStateRecord(
      makePayload("afterFileEdit", { file_path: "/tmp/a.txt", edits: [{ old: "a", new: "b" }] }),
      makeConfig(),
    );
    expect(record.file_path, "file_path should be recorded").toBe("/tmp/a.txt");
    expect(record.edits, "edits should be recorded").toBeDefined();
  });

  it("truncates large content fields with truncated:true and original_length", () => {
    const longContent = "x".repeat(1000);
    const record = buildStateRecord(
      makePayload("beforeReadFile", { file_path: "/tmp/a.txt", content: longContent }),
      makeConfig({ max_chars: 100 }),
    );
    expect((record.content as string).length, "content should be truncated to max_chars").toBe(100);
    expect(record.content_truncated, "truncated flag should be true").toBe(true);
    expect(record.content_original_length, "original_length should be recorded").toBe(1000);
  });

  it("truncates prompt field at LANGFUSE_MAX_CHARS", () => {
    const longPrompt = "y".repeat(2000);
    const record = buildStateRecord(
      makePayload("beforeSubmitPrompt", { prompt: longPrompt }),
      makeConfig({ max_chars: 500 }),
    );
    expect((record.prompt as string).length, "prompt should be truncated").toBe(500);
    expect(record.prompt_truncated, "prompt truncated flag should be true").toBe(true);
    expect(record.prompt_original_length, "prompt original_length should be recorded").toBe(2000);
  });
});

describe("handleEvent (end-to-end append)", () => {
  it("beforeSubmitPrompt → afterAgentResponse produces 2 JSONL records before stop", () => {
    const convId = uniqueConvId();
    handleEvent(makePayload("beforeSubmitPrompt", { conversation_id: convId, prompt: "hello", model: "gpt-4" }), makeConfig());
    handleEvent(makePayload("afterAgentResponse", { conversation_id: convId, text: "hi there", model: "gpt-4" }), makeConfig());

    const records = readStateRecords(getStateFilePath(convId));
    expect(records, "should have 2 records before stop").toHaveLength(2);
    expect(records[0].hook_event_name, "first record should be beforeSubmitPrompt").toBe("beforeSubmitPrompt");
    expect(records[1].hook_event_name, "second record should be afterAgentResponse").toBe("afterAgentResponse");
    expect(records[0].prompt, "first record should have prompt").toBe("hello");
    expect(records[1].text, "second record should have response text").toBe("hi there");
  });
});

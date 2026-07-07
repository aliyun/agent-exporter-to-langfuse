import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, utimesSync, mkdirSync, existsSync as nodeExistsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import type { Config } from "../src/config.js";
import type { CursorHookPayload } from "../src/types.js";
import { handleSessionStart } from "../src/recovery.js";
import { getStateFilePath, readStateRecords } from "../src/state.js";
import type { DeliverFn } from "../src/trace.js";

let testHome: string;

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

beforeEach(() => {
  testHome = mkdtempSync(join(tmpdir(), "cursor-rec-"));
  vi.stubEnv("HOME", testHome);
});

afterEach(() => {
  rmSync(testHome, { recursive: true, force: true });
  vi.unstubAllEnvs();
});

/** Write a state file with the given records and set its mtime to hoursAgo. */
function writeStateFile(conversationId: string, records: object[], hoursAgo: number): string {
  const path = getStateFilePath(conversationId);
  mkdirSync(dirname(path), { recursive: true });
  const lines = records.map((r) => JSON.stringify(r)).join("\n") + "\n";
  writeFileSync(path, lines);
  // Set mtime to hoursAgo hours ago
  const targetTime = new Date(Date.now() - hoursAgo * 3600 * 1000);
  utimesSync(path, targetTime, targetTime);
  return path;
}

function existsSync(path: string): boolean {
  return nodeExistsSync(path);
}

function makeSessionPayload(conversationId?: string): CursorHookPayload {
  return {
    hook_event_name: "sessionStart",
    conversation_id: conversationId,
  };
}

describe("handleSessionStart", () => {
  it("recovers 7h-old file with cursor_status=unknown and deletes it", async () => {
    const records = [
      { hook_event_name: "beforeSubmitPrompt", generation_id: "g1", timestamp: "2026-01-01T00:00:00.000Z", conversation_id: "orphan1", prompt: "hi" },
      { hook_event_name: "afterAgentResponse", generation_id: "g1", timestamp: "2026-01-01T00:01:00.000Z", conversation_id: "orphan1", text: "bye" },
    ];
    const path = writeStateFile("orphan1", records, 7);

    const delivered: Record<string, unknown>[] = [];
    const mockDeliver: DeliverFn = async (otlp) => {
      delivered.push(otlp);
      return true;
    };

    await handleSessionStart(makeSessionPayload("current-session"), makeConfig(), mockDeliver);

    expect(delivered, "should deliver 1 turn for the orphaned file").toHaveLength(1);
    // Verify cursor_status=unknown in the OTLP
    const spans = (delivered[0].resourceSpans as Array<{ scopeSpans: Array<{ spans: Array<{ attributes: Array<{ key: string; value: { stringValue: string } }> }> }> }>)[0].scopeSpans[0].spans;
    const root = spans.find((s) => !s.attributes.some((a) => a.key === "parentSpanId"));
    const cursorStatusAttr = root?.attributes.find((a) => a.key === "langfuse.observation.metadata.cursor_status");
    expect(cursorStatusAttr?.value.stringValue, "recovery should set cursor_status=unknown").toBe("unknown");

    expect(!existsSync(path), "orphaned file should be deleted after recovery").toBe(true);
  });

  it("does not recover 3h-old file (under 6h threshold)", async () => {
    const records = [
      { hook_event_name: "beforeSubmitPrompt", generation_id: "g1", timestamp: "2026-01-01T00:00:00.000Z", conversation_id: "orphan2", prompt: "hi" },
    ];
    const path = writeStateFile("orphan2", records, 3);

    const mockDeliver: DeliverFn = vi.fn(async () => true) as unknown as DeliverFn;

    await handleSessionStart(makeSessionPayload("current"), makeConfig(), mockDeliver);

    expect((mockDeliver as unknown as { mock: { calls: unknown[] } }).mock.calls, "should not call deliverTrace for 3h-old file").toHaveLength(0);
    expect(existsSync(path), "3h-old file should be retained (not recovered)").toBe(true);
  });

  it("retains file when deliverTrace fails", async () => {
    const records = [
      { hook_event_name: "beforeSubmitPrompt", generation_id: "g1", timestamp: "2026-01-01T00:00:00.000Z", conversation_id: "orphan3", prompt: "hi" },
    ];
    const path = writeStateFile("orphan3", records, 7);

    const mockDeliver: DeliverFn = async () => false;

    await handleSessionStart(makeSessionPayload("current"), makeConfig(), mockDeliver);

    expect(existsSync(path), "file should be retained when delivery fails").toBe(true);
  });

  it("skips current session's conversation_id file", async () => {
    const records = [
      { hook_event_name: "beforeSubmitPrompt", generation_id: "g1", timestamp: "2026-01-01T00:00:00.000Z", conversation_id: "current-session", prompt: "hi" },
    ];
    // Make it 7h old to ensure it would otherwise be recovered
    const path = writeStateFile("current-session", records, 7);

    const mockDeliver: DeliverFn = vi.fn(async () => true) as unknown as DeliverFn;

    await handleSessionStart(makeSessionPayload("current-session"), makeConfig(), mockDeliver);

    expect((mockDeliver as unknown as { mock: { calls: unknown[] } }).mock.calls, "should not recover current session's file").toHaveLength(0);
    expect(existsSync(path), "current session file should be retained").toBe(true);
  });

  it("does not throw on scan errors (fail-open)", async () => {
    // No sessions directory — should not throw
    await expect(
      handleSessionStart(makeSessionPayload("current"), makeConfig(), async () => true),
      "handleSessionStart should not throw on missing directory"
    ).resolves.toBeUndefined();
  });

  it("deletes empty state file during recovery", async () => {
    const path = writeStateFile("orphan-empty", [], 7);

    await handleSessionStart(makeSessionPayload("current"), makeConfig(), async () => true);

    expect(!existsSync(path), "empty file should be deleted").toBe(true);
  });
});

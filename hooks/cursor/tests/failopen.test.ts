import { describe, expect, it, beforeAll, beforeEach, afterEach, vi } from "vitest";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";

const BINARY = resolve(__dirname, "../dist/index.mjs");

let testHome: string;

beforeAll(() => {
  if (!existsSync(BINARY)) {
    throw new Error(`dist/index.mjs not found at ${BINARY}. Run "npm run build" before tests.`);
  }
});

beforeEach(() => {
  testHome = mkdtempSync(join(tmpdir(), "cursor-fail-"));
  vi.stubEnv("HOME", testHome);
});

afterEach(() => {
  rmSync(testHome, { recursive: true, force: true });
  vi.unstubAllEnvs();
});

interface RunResult {
  stdout: string;
  exitCode: number;
}

function runHook(stdin: string): RunResult {
  try {
    const stdout = execFileSync("node", [BINARY], {
      input: stdin,
      encoding: "utf-8",
      timeout: 10_000,
      env: { ...process.env, HOME: testHome, LANGSTASH_ENABLED: "false" },
    });
    return { stdout, exitCode: 0 };
  } catch (e) {
    const err = e as { stdout?: string; status?: number; stderr?: string };
    return {
      stdout: err.stdout ?? "",
      exitCode: err.status ?? 1,
    };
  }
}

describe("fail-open matrix", () => {
  describe("corrupted stdin", () => {
    it("exits 0 with stdout {continue,allow} for corrupted JSON", () => {
      const result = runHook("not valid json");
      expect(result.exitCode, `corrupted stdin should exit 0, got ${result.exitCode}`).toBe(0);
      expect(JSON.parse(result.stdout), "corrupted stdin stdout should be continue/allow").toEqual({
        continue: true,
        permission: "allow",
      });
    });

    it("exits 0 with stdout {continue,allow} for empty stdin", () => {
      const result = runHook("");
      expect(result.exitCode, `empty stdin should exit 0, got ${result.exitCode}`).toBe(0);
      expect(JSON.parse(result.stdout), "empty stdin stdout should be continue/allow").toEqual({
        continue: true,
        permission: "allow",
      });
    });
  });

  describe("stop hook", () => {
    it("stop with nonexistent conversation_id exits 0 with stdout {}", () => {
      const result = runHook(JSON.stringify({
        hook_event_name: "stop",
        conversation_id: "nonexistent-conv",
        status: "completed",
      }));
      expect(result.exitCode, `stop should exit 0, got ${result.exitCode}`).toBe(0);
      expect(JSON.parse(result.stdout), "stop stdout should be exactly {}").toEqual({});
    });

    it("stop with corrupted state file exits 0 with stdout {}", () => {
      // Create a corrupted state file
      const stateDir = join(testHome, ".agent-exporter-to-langfuse", "data", "cursor-sessions");
      const { mkdirSync } = require("node:fs");
      mkdirSync(stateDir, { recursive: true });
      writeFileSync(join(stateDir, "conv-corrupt.jsonl"), "NOT JSON\nALSO NOT JSON\n");

      const result = runHook(JSON.stringify({
        hook_event_name: "stop",
        conversation_id: "conv-corrupt",
        status: "completed",
      }));
      expect(result.exitCode, `stop with corrupted state should exit 0, got ${result.exitCode}`).toBe(0);
      expect(JSON.parse(result.stdout), "stop stdout should be exactly {} even with corrupted state").toEqual({});
    });
  });

  describe("event hooks", () => {
    it("beforeSubmitPrompt exits 0 with stdout {continue,allow}", () => {
      const result = runHook(JSON.stringify({
        hook_event_name: "beforeSubmitPrompt",
        conversation_id: "conv-1",
        prompt: "hello",
        model: "gpt-4",
      }));
      expect(result.exitCode, `event hook should exit 0, got ${result.exitCode}`).toBe(0);
      expect(JSON.parse(result.stdout), "event hook stdout should be continue/allow").toEqual({
        continue: true,
        permission: "allow",
      });
    });

    it("afterAgentResponse exits 0 with stdout {continue,allow}", () => {
      const result = runHook(JSON.stringify({
        hook_event_name: "afterAgentResponse",
        conversation_id: "conv-1",
        text: "response",
        model: "gpt-4",
      }));
      expect(result.exitCode, `event hook should exit 0, got ${result.exitCode}`).toBe(0);
      expect(JSON.parse(result.stdout), "event hook stdout should be continue/allow").toEqual({
        continue: true,
        permission: "allow",
      });
    });

    it("sessionStart exits 0 with stdout {continue,allow}", () => {
      const result = runHook(JSON.stringify({
        hook_event_name: "sessionStart",
        conversation_id: "conv-1",
      }));
      expect(result.exitCode, `sessionStart should exit 0, got ${result.exitCode}`).toBe(0);
      expect(JSON.parse(result.stdout), "sessionStart stdout should be continue/allow").toEqual({
        continue: true,
        permission: "allow",
      });
    });

    it("unknown event name exits 0 with stdout {continue,allow}", () => {
      const result = runHook(JSON.stringify({
        hook_event_name: "unknownEvent",
        conversation_id: "conv-1",
      }));
      expect(result.exitCode, `unknown event should exit 0, got ${result.exitCode}`).toBe(0);
      expect(JSON.parse(result.stdout), "unknown event stdout should be continue/allow").toEqual({
        continue: true,
        permission: "allow",
      });
    });
  });

  describe("stdout is valid JSON only", () => {
    it("stop stdout is exactly {} (no extra content)", () => {
      const result = runHook(JSON.stringify({
        hook_event_name: "stop",
        conversation_id: "no-such-conv",
      }));
      expect(result.stdout, "stop stdout should be exactly {}").toBe("{}");
    });

    it("event stdout is valid JSON with continue and permission", () => {
      const result = runHook(JSON.stringify({
        hook_event_name: "beforeSubmitPrompt",
        conversation_id: "c1",
      }));
      // stdout should be parseable JSON
      expect(() => JSON.parse(result.stdout), "stdout should be valid JSON").not.toThrow();
      const parsed = JSON.parse(result.stdout);
      expect(parsed.continue, "continue should be true").toBe(true);
      expect(parsed.permission, "permission should be allow").toBe("allow");
    });
  });
});

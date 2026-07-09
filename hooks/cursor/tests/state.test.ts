import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync, existsSync, readFileSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { appendStateRecord, deleteStateFile, getStateFilePath, getSessionsDir, readStateRecords } from "../src/state.js";

let testHome: string;

beforeEach(() => {
  testHome = mkdtempSync(join(tmpdir(), "cursor-state-"));
  vi.stubEnv("HOME", testHome);
});

afterEach(() => {
  rmSync(testHome, { recursive: true, force: true });
  vi.unstubAllEnvs();
});

describe("state", () => {
  describe("getStateFilePath", () => {
    it("returns path under cursor-sessions with conversation_id.jsonl", () => {
      const path = getStateFilePath("conv-123");
      expect(path, "state file path should contain conversation_id").toContain("conv-123.jsonl");
      expect(path, "state file path should be under cursor-sessions").toContain("cursor-sessions");
    });
  });

  describe("appendStateRecord + readStateRecords", () => {
    it("appends a single JSONL record and reads it back", () => {
      appendStateRecord({
        hook_event_name: "beforeSubmitPrompt",
        generation_id: "g1",
        timestamp: "2026-01-01T00:00:00.000Z",
        conversation_id: "conv-1",
        prompt: "hello",
      });

      const path = getStateFilePath("conv-1");
      const records = readStateRecords(path);
      expect(records, "should read back 1 record").toHaveLength(1);
      expect(records[0].hook_event_name, "record should have correct event name").toBe("beforeSubmitPrompt");
      expect(records[0].prompt, "record should have prompt field").toBe("hello");
    });

    it("appends multiple records for same conversation_id", () => {
      appendStateRecord({
        hook_event_name: "beforeSubmitPrompt",
        generation_id: "g1",
        timestamp: "2026-01-01T00:00:00.000Z",
        conversation_id: "conv-2",
      });
      appendStateRecord({
        hook_event_name: "afterAgentResponse",
        generation_id: "g1",
        timestamp: "2026-01-01T00:01:00.000Z",
        conversation_id: "conv-2",
      });

      const path = getStateFilePath("conv-2");
      const records = readStateRecords(path);
      expect(records, "should read back 2 records").toHaveLength(2);
      expect(records[0].hook_event_name, "first record should be beforeSubmitPrompt").toBe("beforeSubmitPrompt");
      expect(records[1].hook_event_name, "second record should be afterAgentResponse").toBe("afterAgentResponse");
    });

    it("concurrent append of same conversation_id produces no corrupted lines", () => {
      const convId = "conv-concurrent";
      const records = Array.from({ length: 50 }, (_, i) => ({
        hook_event_name: "afterAgentThought",
        generation_id: "g1",
        timestamp: new Date(2026, 0, 1, 0, 0, i).toISOString(),
        conversation_id: convId,
        text: `thought-${i}`,
      }));

      // Append all synchronously (simulates concurrent arrival)
      for (const r of records) {
        appendStateRecord(r);
      }

      const path = getStateFilePath(convId);
      const readBack = readStateRecords(path);
      expect(readBack, "all 50 records should be readable without parse errors").toHaveLength(50);
    });

    it("readStateRecords skips malformed lines", () => {
      const path = getStateFilePath("conv-bad");
      const dir = getSessionsDir();
      const { mkdirSync } = require("node:fs");
      mkdirSync(dir, { recursive: true });
      writeFileSync(path, '{"valid":true}\nNOT JSON\n{"also":true}\n');
      const records = readStateRecords(path);
      expect(records, "should read 2 valid records, skip 1 malformed").toHaveLength(2);
    });

    it("readStateRecords returns empty array for nonexistent file", () => {
      const records = readStateRecords(getStateFilePath("nonexistent"));
      expect(records, "nonexistent file should return empty array").toEqual([]);
    });
  });

  describe("deleteStateFile", () => {
    it("deletes an existing state file", () => {
      appendStateRecord({
        hook_event_name: "stop",
        generation_id: "g1",
        timestamp: "2026-01-01T00:00:00.000Z",
        conversation_id: "conv-del",
      });
      const path = getStateFilePath("conv-del");
      expect(existsSync(path), "file should exist before deletion").toBe(true);
      deleteStateFile(path);
      expect(existsSync(path), "file should be deleted").toBe(false);
    });

    it("does not throw for nonexistent file", () => {
      expect(() => deleteStateFile(getStateFilePath("never-existed")), "delete should not throw for missing file").not.toThrow();
    });
  });
});

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { getConfig } from "../src/config.js";

let testHome: string;

beforeEach(() => {
  testHome = mkdtempSync(join(tmpdir(), "cursor-cfg-"));
  // Clear all relevant env vars before each test
  vi.stubEnv("HOME", testHome);
  vi.stubEnv("LANGFUSE_MAX_CHARS", "");
  vi.stubEnv("LANGFUSE_TAGS", "");
  vi.stubEnv("LANGFUSE_USER_ID", "");
  vi.stubEnv("USER", "");
  vi.stubEnv("LOGNAME", "");
  vi.stubEnv("USERNAME", "");
  vi.stubEnv("LANGFUSE_CURSOR_FAIL_ON_ERROR", "");
});

afterEach(() => {
  rmSync(testHome, { recursive: true, force: true });
  vi.unstubAllEnvs();
});

describe("getConfig", () => {
  it("defaults max_chars to 800000", () => {
    const config = getConfig();
    expect(config.max_chars, "LANGFUSE_MAX_CHARS default should be 800000").toBe(800_000);
  });

  it("defaults tags to [\"cursor\"]", () => {
    const config = getConfig();
    expect(config.tags, "default tags should include cursor").toEqual(["cursor"]);
  });

  it("reads LANGFUSE_MAX_CHARS from env", () => {
    vi.stubEnv("LANGFUSE_MAX_CHARS", "1000");
    const config = getConfig();
    expect(config.max_chars, "LANGFUSE_MAX_CHARS=1000 should override default").toBe(1000);
  });

  it("reads LANGFUSE_TAGS from env and includes cursor", () => {
    vi.stubEnv("LANGFUSE_TAGS", "team:olap,env:prod");
    const config = getConfig();
    expect(config.tags, "LANGFUSE_TAGS should be parsed as array").toEqual(["team:olap", "env:prod"]);
  });

  it("resolves user_id from LANGFUSE_USER_ID", () => {
    vi.stubEnv("LANGFUSE_USER_ID", "alice");
    const config = getConfig();
    expect(config.user_id, "user_id should come from LANGFUSE_USER_ID").toBe("alice");
  });

  it("resolves user_id from USER env as fallback", () => {
    vi.stubEnv("USER", "bob");
    const config = getConfig();
    expect(config.user_id, "user_id should fall back to USER env").toBe("bob");
  });

  it("defaults langstash_enabled to false", () => {
    const config = getConfig();
    expect(config.langstash_enabled, "langstash_enabled default should be false").toBe(false);
  });

  it("defaults langstash_url to http://127.0.0.1:5288", () => {
    const config = getConfig();
    expect(config.langstash_url, "langstash_url default should be local").toBe("http://127.0.0.1:5288");
  });

  it("reads LANGFUSE_CURSOR_FAIL_ON_ERROR from env", () => {
    vi.stubEnv("LANGFUSE_CURSOR_FAIL_ON_ERROR", "true");
    const config = getConfig();
    expect(config.fail_on_error, "fail_on_error should be true when env is set").toBe(true);
  });
});

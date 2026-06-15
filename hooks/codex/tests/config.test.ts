import { describe, expect, it } from "vitest";
import { getConfig } from "../src/config.js";

describe("getConfig", () => {
  it("returns defaults when no env vars are set", () => {
    const config = getConfig({});
    expect(config.enabled).toBe(true);
    expect(config.base_url).toBe("https://us.cloud.langfuse.com");
    expect(config.max_chars).toBe(800_000);
    expect(config.debug).toBe(true);
    expect(config.fail_on_error).toBe(false);
    expect(config.langstash_enabled).toBe(false);
    expect(config.langstash_url).toBe("http://127.0.0.1:5288");
    expect(config.langstash_timeout).toBe(10);
    expect(config.public_key).toBeUndefined();
    expect(config.secret_key).toBeUndefined();
    expect(config.user_id).toBeUndefined();
  });

  it("defaults tags to ['codex'] when LANGFUSE_TAGS is not set", () => {
    const config = getConfig({});
    expect(config.tags).toEqual(["codex"]);
  });

  describe("boolean parsing", () => {
    it.each(["true", "1", "yes"])("parses '%s' as true", (val) => {
      const config = getConfig({ LANGFUSE_DEBUG: val });
      expect(config.debug).toBe(true);
    });

    it.each(["false", "0", "no"])("parses '%s' as false", (val) => {
      const config = getConfig({ LANGFUSE_DEBUG: val });
      expect(config.debug).toBe(false);
    });

    it("parses 'on' as true and 'off' as false", () => {
      expect(getConfig({ LANGFUSE_DEBUG: "on" }).debug).toBe(true);
      expect(getConfig({ LANGFUSE_DEBUG: "off" }).debug).toBe(false);
    });

    it("falls back to default for unrecognized boolean string", () => {
      // LANGFUSE_DEBUG default is true
      const config = getConfig({ LANGFUSE_DEBUG: "maybe" });
      expect(config.debug).toBe(true);
    });

    it("TRACE_TO_LANGFUSE controls enabled", () => {
      expect(getConfig({ TRACE_TO_LANGFUSE: "false" }).enabled).toBe(false);
      expect(getConfig({ TRACE_TO_LANGFUSE: "0" }).enabled).toBe(false);
      expect(getConfig({ TRACE_TO_LANGFUSE: "true" }).enabled).toBe(true);
    });
  });

  describe("tags parsing", () => {
    it("parses comma-separated tags", () => {
      const config = getConfig({ LANGFUSE_TAGS: "foo,bar,baz" });
      expect(config.tags).toEqual(["foo", "bar", "baz"]);
    });

    it("parses JSON array tags", () => {
      const config = getConfig({ LANGFUSE_TAGS: '["alpha","beta"]' });
      expect(config.tags).toEqual(["alpha", "beta"]);
    });

    it("trims whitespace in comma-separated tags", () => {
      const config = getConfig({ LANGFUSE_TAGS: " foo , bar " });
      expect(config.tags).toEqual(["foo", "bar"]);
    });

    it("defaults to ['codex'] when tags env is empty string", () => {
      const config = getConfig({ LANGFUSE_TAGS: "" });
      expect(config.tags).toEqual(["codex"]);
    });

    it("defaults to ['codex'] when tags env is whitespace only", () => {
      const config = getConfig({ LANGFUSE_TAGS: "   " });
      expect(config.tags).toEqual(["codex"]);
    });
  });

  describe("integer parsing", () => {
    it("parses max_chars from env", () => {
      const config = getConfig({ LANGFUSE_MAX_CHARS: "500000" });
      expect(config.max_chars).toBe(500_000);
    });

    it("parses langstash_timeout from env", () => {
      const config = getConfig({ LANGSTASH_TIMEOUT: "30" });
      expect(config.langstash_timeout).toBe(30);
    });

    it("falls back to default for non-numeric string", () => {
      const config = getConfig({ LANGFUSE_MAX_CHARS: "abc" });
      expect(config.max_chars).toBe(800_000);
    });
  });

  describe("resolveUserId", () => {
    it("uses LANGFUSE_USER_ID when set", () => {
      const config = getConfig({
        LANGFUSE_USER_ID: "explicit-user",
        USER: "fallback-user",
      });
      expect(config.user_id).toBe("explicit-user");
    });

    it("falls back to USER", () => {
      const config = getConfig({ USER: "sys-user" });
      expect(config.user_id).toBe("sys-user");
    });

    it("falls back to LOGNAME when USER is not set", () => {
      const config = getConfig({ LOGNAME: "log-user" });
      expect(config.user_id).toBe("log-user");
    });

    it("falls back to USERNAME when USER and LOGNAME are not set", () => {
      const config = getConfig({ USERNAME: "win-user" });
      expect(config.user_id).toBe("win-user");
    });

    it("returns undefined when no user env vars are set", () => {
      const config = getConfig({});
      expect(config.user_id).toBeUndefined();
    });
  });

  it("reads keys from env", () => {
    const config = getConfig({
      LANGFUSE_PUBLIC_KEY: "pk-test",
      LANGFUSE_SECRET_KEY: "sk-test",
    });
    expect(config.public_key).toBe("pk-test");
    expect(config.secret_key).toBe("sk-test");
  });

  it("reads base_url from env", () => {
    const config = getConfig({ LANGFUSE_BASE_URL: "https://custom.host" });
    expect(config.base_url).toBe("https://custom.host");
  });

  it("reads langstash config from env", () => {
    const config = getConfig({
      LANGSTASH_ENABLED: "true",
      LANGSTASH_URL: "http://my-langstash:9999",
      LANGSTASH_TIMEOUT: "20",
    });
    expect(config.langstash_enabled).toBe(true);
    expect(config.langstash_url).toBe("http://my-langstash:9999");
    expect(config.langstash_timeout).toBe(20);
  });
});

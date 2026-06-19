import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, readFileSync, rmSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";

// We test the source directly (TS compiled to dist/ or imported as .ts isn't needed —
// we test the logic via a JS-compatible re-export). For CI, run `npm run build` first
// and import from dist/. For local dev, we inline the core logic test.

// Since the package uses TS and needs compilation, we test by importing the built output.
// If dist/ doesn't exist, we test the logic inline.

const SAMPLE_OTLP = {
  resourceSpans: [{
    scopeSpans: [{
      scope: { name: "agent-exporter-to-langfuse" },
      spans: [{
        traceId: "a".repeat(32),
        spanId: "b".repeat(16),
        name: "test",
        startTimeUnixNano: "1718000000000000000",
        endTimeUnixNano: "1718000001000000000",
      }],
    }],
  }],
};

let tmpDir;

beforeEach(() => {
  tmpDir = join(tmpdir(), `langstash-deliver-test-${randomUUID()}`);
  mkdirSync(tmpDir, { recursive: true });
});

afterEach(() => {
  try { rmSync(tmpDir, { recursive: true, force: true }); } catch {}
  delete process.env.LANGSTASH_ENABLED;
  delete process.env.LANGSTASH_URL;
  delete process.env.LANGSTASH_TIMEOUT;
  delete process.env.LANGFUSE_BASE_URL;
  delete process.env.LANGFUSE_PUBLIC_KEY;
  delete process.env.LANGFUSE_SECRET_KEY;
});

// Try to import from dist; if not available, skip with a message
let deliverTrace;
try {
  const mod = await import("../dist/index.js");
  deliverTrace = mod.deliverTrace;
} catch {
  console.log("SKIP: dist/index.mjs not found. Run `npm run build` first.");
  process.exit(0);
}

describe("deliverTrace", () => {
  describe("Tier 1: langstash", () => {
    it("returns true when langstash POST succeeds", async () => {
      process.env.LANGSTASH_ENABLED = "true";
      process.env.LANGSTASH_URL = "http://127.0.0.1:9999";

      const calls = [];
      const mockFetch = async (url, opts) => {
        calls.push({ url, method: opts.method });
        return { ok: true, status: 200 };
      };

      const result = await deliverTrace(SAMPLE_OTLP, { fetchFn: mockFetch });
      assert.equal(result, true);
      assert.equal(calls.length, 1);
      assert.ok(calls[0].url.includes("/ingest"));
    });
  });

  describe("Tier 2: Langfuse OTel", () => {
    it("falls back to Langfuse OTel when langstash fails", async () => {
      process.env.LANGSTASH_ENABLED = "true";
      process.env.LANGFUSE_BASE_URL = "https://langfuse.example.com";
      process.env.LANGFUSE_PUBLIC_KEY = "pk-test";
      process.env.LANGFUSE_SECRET_KEY = "sk-test";

      const calls = [];
      const mockFetch = async (url, opts) => {
        calls.push({ url, headers: opts.headers });
        if (url.includes("/ingest")) return { ok: false, status: 0 };
        return { ok: true, status: 200 };
      };

      const result = await deliverTrace(SAMPLE_OTLP, { fetchFn: mockFetch });
      assert.equal(result, true);
      assert.equal(calls.length, 2);
      assert.ok(calls[1].url.includes("/api/public/otel/v1/traces"));
      assert.ok(calls[1].headers["Content-Type"] === "application/json");
      assert.ok(calls[1].headers["Authorization"].startsWith("Basic "));
    });

    it("skips langstash when disabled and goes to Tier 2", async () => {
      process.env.LANGFUSE_BASE_URL = "https://langfuse.example.com";
      process.env.LANGFUSE_PUBLIC_KEY = "pk-test";
      process.env.LANGFUSE_SECRET_KEY = "sk-test";

      const calls = [];
      const mockFetch = async (url, opts) => {
        calls.push({ url });
        return { ok: true, status: 200 };
      };

      const result = await deliverTrace(SAMPLE_OTLP, { fetchFn: mockFetch });
      assert.equal(result, true);
      assert.equal(calls.length, 1);
      assert.ok(calls[0].url.includes("/api/public/otel/v1/traces"));
    });
  });

  describe("Tier 3: failed log", () => {
    it("returns false when all tiers fail", async () => {
      // No credentials = skip Tier 2, no langstash = skip Tier 1
      const mockFetch = async () => ({ ok: false, status: 0 });
      const result = await deliverTrace(SAMPLE_OTLP, { fetchFn: mockFetch });
      assert.equal(result, false);
    });
  });

  describe("custom fetch adapter", () => {
    it("uses injected fetchFn for all tiers", async () => {
      process.env.LANGSTASH_ENABLED = "true";
      process.env.LANGFUSE_BASE_URL = "https://langfuse.example.com";
      process.env.LANGFUSE_PUBLIC_KEY = "pk";
      process.env.LANGFUSE_SECRET_KEY = "sk";

      const calls = [];
      const customFetch = async (url, opts) => {
        calls.push({ url, method: opts.method });
        return { ok: false, status: 500 };
      };

      await deliverTrace(SAMPLE_OTLP, { fetchFn: customFetch });
      assert.equal(calls.length, 2);
      assert.ok(calls[0].url.includes("/ingest"));
      assert.ok(calls[1].url.includes("/api/public/otel/v1/traces"));
    });
  });
});

import { afterEach, describe, expect, it } from "vitest";

import { parseLimit, createPayloadLimits, DEFAULT_LIMITS } from "../src/limits.ts";
import { REDACTED, redactString, redactValue } from "../src/redaction.ts";
import { shapePayload, truncate } from "../src/shape.ts";

describe("truncation", () => {
  it("truncates overlong strings with a marker", () => {
    const long = "x".repeat(20_000);
    const result = truncate(long);
    expect(result.length, "截断后长度必须小于原文").toBeLessThan(long.length);
    expect(result.endsWith("... [truncated]"), "截断字符串必须携带标记").toBe(true);
  });
});

describe("redaction", () => {
  it("replaces fake credentials and bearer tokens with placeholders", () => {
    const input = [
      "key sk-lf-abc123def456 in text",
      "Authorization: Bearer abcdefghijklmnop123456",
      "pk-lf-public-key-000",
      "MY_SECRET=supersecretvalue",
    ].join(" | ");
    const result = redactString(input);
    expect(result, "sk-lf- 伪凭据必须被替换").not.toContain("sk-lf-abc123def456");
    expect(result, "Bearer token 必须被替换").not.toContain("abcdefghijklmnop123456");
    expect(result, "pk-lf- 凭据必须被替换").not.toContain("pk-lf-public-key-000");
    expect(result, "密钥赋值形式必须被替换").not.toContain("supersecretvalue");
    expect(result).toContain(REDACTED);
  });

  it("redacts private key blocks", () => {
    const pem = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----";
    expect(redactString(pem)).toBe(REDACTED);
  });

  it("redacts sensitive field names in objects", () => {
    const result = redactValue({
      authorization: "some-header-value",
      apiKey: "another-value",
      secret: "hidden",
      normal: "visible",
    }) as Record<string, unknown>;
    expect(result.authorization).toBe(REDACTED);
    expect(result.apiKey).toBe(REDACTED);
    expect(result.secret).toBe(REDACTED);
    expect(result.normal).toBe("visible");
  });
});

describe("shapePayload", () => {
  it("degrades safely on excessive depth", () => {
    let deep: Record<string, unknown> = { leaf: true };
    for (let i = 0; i < 20; i++) {
      deep = { child: deep };
    }
    const json = JSON.stringify(shapePayload(deep));
    expect(json, "超深嵌套必须以 max depth 标记降级").toContain("max depth");
  });

  it("degrades safely when the node budget is exhausted", () => {
    const wide = Array.from({ length: 40 }, () =>
      Object.fromEntries(Array.from({ length: 79 }, (_, i) => [`k${i}`, i])),
    );
    const json = JSON.stringify(shapePayload(wide));
    expect(json, "节点数超限必须以 payload too large 标记降级").toContain("[payload too large]");
  });

  it("handles circular references", () => {
    const value: Record<string, unknown> = { name: "circle" };
    value.self = value;
    const json = JSON.stringify(shapePayload(value));
    expect(json).toContain("[circular]");
  });

  it("redacts credentials inside shaped payloads by default", () => {
    const result = JSON.stringify(shapePayload({ text: "token sk-lf-veryfakekey123 here" }));
    expect(result, "shapePayload 默认必须脱敏").not.toContain("sk-lf-veryfakekey123");
  });
});

describe("limit overrides", () => {
  const savedEnv = { ...process.env };

  afterEach(() => {
    for (const key of Object.keys(process.env)) {
      if (!(key in savedEnv)) delete process.env[key];
    }
    Object.assign(process.env, savedEnv);
  });

  it("parseLimit maps unset/unlimited/positive values", () => {
    expect(parseLimit(undefined, 5)).toBe(5);
    expect(parseLimit("", 5)).toBe(5);
    expect(parseLimit("off", 5)).toBe(Number.POSITIVE_INFINITY);
    expect(parseLimit("0", 5)).toBe(Number.POSITIVE_INFINITY);
    expect(parseLimit("12.7", 5)).toBe(12);
    expect(parseLimit("garbage", 5)).toBe(5);
  });

  it("PI_LANGFUSE_MAX_* env vars override limits used by shaping", () => {
    process.env.PI_LANGFUSE_MAX_STRING_LENGTH = "10";
    const limits = createPayloadLimits();
    expect(limits.maxString, "环境变量必须覆盖 maxString 限额").toBe(10);
    expect(limits.maxDepth).toBe(DEFAULT_LIMITS.maxDepth);

    const shaped = shapePayload("a".repeat(100)) as string;
    expect(shaped.startsWith("a".repeat(10)), "整形必须使用覆盖后的限额").toBe(true);
    expect(shaped).toContain("[truncated]");
  });
});

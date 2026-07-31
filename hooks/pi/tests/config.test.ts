import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { loadPiEnv, resolveTags, resolveUserId } from "../src/config.ts";

describe("loadPiEnv", () => {
  let dir: string | undefined;

  afterEach(() => {
    if (dir) {
      rmSync(dir, { recursive: true, force: true });
      dir = undefined;
    }
  });

  function writeEnvFile(content: string): string {
    dir = mkdtempSync(join(tmpdir(), "pi-env-"));
    const path = join(dir, "pi.env");
    writeFileSync(path, content);
    return path;
  }

  it("injects missing variables and keeps existing process env values", () => {
    const path = writeEnvFile(
      [
        'export LANGFUSE_BASE_URL="https://langfuse.example.com"',
        'export LANGFUSE_PUBLIC_KEY="pk-lf-test-public"',
        'export PI_LANGFUSE_MAX_STRING_LENGTH="100"',
        "# comment line",
        "invalid line",
      ].join("\n"),
    );
    const env: Record<string, string | undefined> = {
      LANGFUSE_BASE_URL: "https://already-set.example.com",
    };

    loadPiEnv(path, env as NodeJS.ProcessEnv);

    expect(
      env.LANGFUSE_BASE_URL,
      "已有环境变量必须优先于 pi.env 中的值",
    ).toBe("https://already-set.example.com");
    expect(env.LANGFUSE_PUBLIC_KEY, "pi.env 中缺失的变量必须被注入").toBe("pk-lf-test-public");
    expect(env.PI_LANGFUSE_MAX_STRING_LENGTH, "限额覆盖变量必须被注入").toBe("100");
    expect(env["# comment line"]).toBeUndefined();
  });

  it("degrades silently when the env file is missing", () => {
    const env: Record<string, string | undefined> = {};
    expect(() => loadPiEnv("/nonexistent/path/pi.env", env as NodeJS.ProcessEnv)).not.toThrow();
    expect(Object.keys(env)).toHaveLength(0);
  });
});

describe("resolveTags", () => {
  it("always includes the fixed pi tag", () => {
    expect(resolveTags({} as NodeJS.ProcessEnv)).toEqual(["pi"]);
    expect(resolveTags({ LANGFUSE_TAGS: "team-a, prod" } as NodeJS.ProcessEnv)).toEqual([
      "team-a",
      "prod",
      "pi",
    ]);
    expect(resolveTags({ LANGFUSE_TAGS: "pi,x" } as NodeJS.ProcessEnv)).toEqual(["pi", "x"]);
  });
});

describe("resolveUserId", () => {
  it("prefers LANGFUSE_USER_ID then falls back to OS user", () => {
    expect(resolveUserId({ LANGFUSE_USER_ID: "alice" } as NodeJS.ProcessEnv)).toBe("alice");
    expect(resolveUserId({ USER: "bob" } as NodeJS.ProcessEnv)).toBe("bob");
    expect(resolveUserId({} as NodeJS.ProcessEnv)).toBe("unknown");
  });
});

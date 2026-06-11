import { readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export function readStdin<T>(): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let buffer = "";
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => (buffer += chunk));
    process.stdin.on("end", () => {
      const trimmed = buffer.trim();
      if (!trimmed) {
        reject(new Error("empty hook stdin"));
        return;
      }
      try {
        resolve(JSON.parse(trimmed) as T);
      } catch (error) {
        reject(
          new Error(
            `failed to parse hook stdin: ${error instanceof Error ? error.message : String(error)}`,
          ),
        );
      }
    });
    process.stdin.once("error", reject);
  });
}

export function isPrimitive(value: unknown): value is string | number | boolean {
  const t = typeof value;
  return t === "string" || t === "number" || t === "boolean";
}

export function toText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (isPrimitive(value)) return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function truncate(
  value: string,
  maxChars: number,
): { text: string; meta?: { truncated: true; originalLength: number } } {
  if (value.length <= maxChars) return { text: value };
  return {
    text: value.slice(0, maxChars),
    meta: { truncated: true, originalLength: value.length },
  };
}

let debugEnabled = false;
export function setDebug(enabled: boolean): void {
  debugEnabled = enabled;
}
export function debugLog(...args: unknown[]): void {
  if (!debugEnabled) return;
  // eslint-disable-next-line no-console
  console.error("[langfuse-codex]", ...args);
}

/**
 * Load env vars from the agent-exporter-to-langfuse config file.
 * Only sets variables that are not already in the environment.
 */
export function loadEnvFile(): void {
  const envFile = join(homedir(), ".agent-exporter-to-langfuse", "config", "codex.env");
  try {
    const content = readFileSync(envFile, "utf8");
    for (const line of content.split("\n")) {
      const m = line.match(/^export\s+([A-Za-z_][A-Za-z0-9_]*)="(.*)"\s*$/);
      if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
    }
  } catch {
    // env file not found or unreadable — that's fine
  }
}

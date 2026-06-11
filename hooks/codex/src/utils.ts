import { appendFileSync, mkdirSync, readFileSync, renameSync, statSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

const LOG_DIR = join(homedir(), ".codex", "state");
const LOG_FILE = join(LOG_DIR, "langfuse_hook.log");
const LOG_MAX_BYTES = 200_000_000;
const LOG_BACKUP_COUNT = 3;
let logDirReady = false;
let logRotationChecked = false;

function rotateIfNeeded(): void {
  if (logRotationChecked) return;
  logRotationChecked = true;
  try {
    const stat = statSync(LOG_FILE);
    if (stat.size < LOG_MAX_BYTES) return;
    for (let i = LOG_BACKUP_COUNT - 1; i >= 1; i--) {
      try { renameSync(`${LOG_FILE}.${i}`, `${LOG_FILE}.${i + 1}`); } catch {}
    }
    try { renameSync(LOG_FILE, `${LOG_FILE}.1`); } catch {}
    try { unlinkSync(`${LOG_FILE}.${LOG_BACKUP_COUNT + 1}`); } catch {}
  } catch {}
}

function logTimestamp(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function writeLog(level: string, args: unknown[]): void {
  const msg = args
    .map((a) => (typeof a === "string" ? a : JSON.stringify(a)))
    .join(" ");
  const line = `${logTimestamp()} [${level}] ${msg}\n`;
  try {
    if (!logDirReady) {
      mkdirSync(LOG_DIR, { recursive: true });
      logDirReady = true;
    }
    rotateIfNeeded();
    appendFileSync(LOG_FILE, line);
  } catch (e) {
    try {
      process.stderr.write(`[codex-langfuse] ${line}`);
      if (!logDirReady) {
        process.stderr.write(`[codex-langfuse] log dir creation failed: ${LOG_DIR} — ${e}\n`);
      }
    } catch {}
  }
}

let debugEnabled = false;
export function setDebug(enabled: boolean): void {
  debugEnabled = enabled;
}

export function debugLog(...args: unknown[]): void {
  if (!debugEnabled) return;
  writeLog("DEBUG", args);
}

export function info(...args: unknown[]): void {
  writeLog("INFO", args);
}

export function warn(...args: unknown[]): void {
  writeLog("WARN", args);
}

export function error(...args: unknown[]): void {
  writeLog("ERROR", args);
}

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

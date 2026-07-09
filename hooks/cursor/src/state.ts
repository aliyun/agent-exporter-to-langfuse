import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import type { StateRecord } from "./types.js";

/** Return the cursor-sessions directory path (computed lazily for testability). */
export function getSessionsDir(): string {
  return join(homedir(), ".agent-exporter-to-langfuse", "data", "cursor-sessions");
}

/** Return the state file path for a given conversation_id. */
export function getStateFilePath(conversationId: string): string {
  return join(getSessionsDir(), `${conversationId}.jsonl`);
}

/** Ensure the sessions directory exists. */
export function ensureSessionsDir(): void {
  mkdirSync(getSessionsDir(), { recursive: true });
}

/**
 * Append a single JSONL record to the per-conversation state file.
 * Uses O_APPEND which is atomic for writes under the PIPE_BUF threshold on POSIX.
 */
export function appendStateRecord(record: StateRecord): void {
  ensureSessionsDir();
  const filePath = getStateFilePath(record.conversation_id ?? "unknown");
  const line = JSON.stringify(record) + "\n";
  appendFileSync(filePath, line, { flag: "a" });
}

/**
 * Read all JSONL records from a state file.
 * Skips malformed lines (does not throw).
 */
export function readStateRecords(filePath: string): StateRecord[] {
  if (!existsSync(filePath)) return [];
  const data = readFileSync(filePath, "utf-8");
  const records: StateRecord[] = [];
  for (const raw of data.split("\n")) {
    const trimmed = raw.trim();
    if (!trimmed) continue;
    try {
      records.push(JSON.parse(trimmed) as StateRecord);
    } catch {
      // skip malformed lines
    }
  }
  return records;
}

/** Delete the state file after all turns have been delivered. */
export function deleteStateFile(filePath: string): void {
  try {
    unlinkSync(filePath);
  } catch {
    // best-effort
  }
}

/** List all state files in the sessions directory. */
export function listSessionFiles(): string[] {
  const dir = getSessionsDir();
  if (!existsSync(dir)) return [];
  const entries = readdirSync(dir);
  return entries
    .filter((name) => name.endsWith(".jsonl"))
    .map((name) => join(dir, name));
}

/** Return the mtime (ms epoch) of a file, or 0 if unavailable. */
export function getFileMtimeMs(filePath: string): number {
  try {
    return statSync(filePath).mtimeMs;
  } catch {
    return 0;
  }
}

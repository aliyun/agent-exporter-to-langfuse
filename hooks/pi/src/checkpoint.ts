/**
 * Crash-recovery checkpoints (R-4). After every turn_end the current run's
 * accumulated span record tree (including the root created at run start) is
 * serialized to a hook-owned file under
 * ~/.agent-exporter-to-langfuse/data/pi-checkpoints/<sessionId>.json.
 * agent_end and interrupted emission clear it. On extension load a leftover
 * checkpoint is rebuilt as a cancelled partial trace and re-delivered.
 * All checkpoint IO failures are logged only and never disturb the normal
 * trace flow.
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import type { RunState, SpanRecord } from "./state.ts";

/** Resolved per call so the hook always follows the current HOME. */
export function checkpointDir(): string {
  return join(homedir(), ".agent-exporter-to-langfuse", "data", "pi-checkpoints");
}

interface CheckpointFile {
  sessionId: string;
  traceId: string;
  rootSpanId: string;
  spans: SpanRecord[];
  cwd: string;
  savedAtMs: number;
}

function checkpointPath(sessionId: string, dir: string): string {
  const safeSessionId = sessionId.replace(/[^A-Za-z0-9._-]/g, "_") || "default";
  return join(dir, `${safeSessionId}.json`);
}

/** Persist the run atomically (tmp file + rename) so no half-written file is ever replayed. */
export function writeCheckpoint(run: RunState, sessionId: string, dir = checkpointDir()): void {
  try {
    mkdirSync(dir, { recursive: true });
    const payload: CheckpointFile = {
      sessionId,
      traceId: run.traceId,
      rootSpanId: run.root.spanId,
      spans: run.spans,
      cwd: run.cwd,
      savedAtMs: Date.now(),
    };
    const target = checkpointPath(sessionId, dir);
    const tmp = `${target}.tmp`;
    writeFileSync(tmp, JSON.stringify(payload), "utf8");
    renameSync(tmp, target);
  } catch (e) {
    console.warn("📊 Langfuse: Failed to write checkpoint", e);
  }
}

export function clearCheckpoint(sessionId: string, dir = checkpointDir()): void {
  try {
    const target = checkpointPath(sessionId, dir);
    if (existsSync(target)) {
      rmSync(target, { force: true });
    }
  } catch (e) {
    console.warn("📊 Langfuse: Failed to clear checkpoint", e);
  }
}

export interface RecoveredRun {
  run: RunState;
  sessionId: string;
}

/**
 * Read and delete every leftover checkpoint, rebuilding each as a run whose
 * dangling records and root are marked cancelled. Reading before deleting and
 * deleting before delivery keeps a single re-delivery per checkpoint.
 */
export function recoverCheckpoints(dir = checkpointDir()): RecoveredRun[] {
  let files: string[];
  try {
    files = readdirSync(dir).filter((name) => name.endsWith(".json"));
  } catch {
    return [];
  }

  const recovered: RecoveredRun[] = [];
  for (const name of files) {
    const path = join(dir, name);
    let parsed: CheckpointFile;
    try {
      parsed = JSON.parse(readFileSync(path, "utf8")) as CheckpointFile;
    } catch (e) {
      console.warn("📊 Langfuse: Discarding unreadable checkpoint", e);
      try {
        rmSync(path, { force: true });
      } catch {
        // best-effort
      }
      continue;
    }

    try {
      rmSync(path, { force: true });
    } catch (e) {
      console.warn("📊 Langfuse: Failed to remove recovered checkpoint", e);
    }

    const run = rebuildRun(parsed);
    if (run) {
      recovered.push({ run, sessionId: parsed.sessionId ?? "" });
    }
  }
  return recovered;
}

function rebuildRun(parsed: CheckpointFile): RunState | null {
  if (!parsed || !Array.isArray(parsed.spans) || typeof parsed.traceId !== "string") {
    console.warn("📊 Langfuse: Discarding malformed checkpoint payload");
    return null;
  }

  const root = parsed.spans.find((span) => span.spanId === parsed.rootSpanId);
  if (!root) {
    console.warn("📊 Langfuse: Discarding checkpoint without root span record");
    return null;
  }

  const recoveredAt = Date.now();
  for (const span of parsed.spans) {
    if (span.endTimeMs === undefined) {
      span.endTimeMs = Math.max(span.startTimeMs, parsed.savedAtMs ?? recoveredAt);
      if (span !== root) {
        span.level = "WARNING";
        span.statusMessage = span.statusMessage ?? "Pi process ended before observation finalized";
        span.metadata = { ...(span.metadata ?? {}), cancelled: true };
      }
    }
  }
  root.metadata = {
    ...(root.metadata ?? {}),
    completed: false,
    cancelled: true,
    recoveredFromCheckpoint: true,
  };

  return {
    traceId: parsed.traceId,
    root,
    spans: parsed.spans,
    activeGenerations: new Map(),
    generationOrder: [],
    activeTools: new Map(),
    generationSeq: 0,
    cwd: parsed.cwd ?? "",
    emitted: false,
  };
}

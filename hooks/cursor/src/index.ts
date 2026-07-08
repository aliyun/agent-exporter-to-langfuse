// Bootstrap: write to stderr immediately so we can tell if the hook was even invoked.
// This fires before any imports or file I/O — if you don't see this line, Cursor
// never called the hook command.
process.stderr.write("[cursor-langfuse] BOOT pid=" + process.pid + " cwd=" + process.cwd() + " home=" + (process.env.HOME ?? "unset") + " node=" + process.execPath + "\n");

import { getConfig } from "./config.js";
import { handleEvent } from "./handlers.js";
import { handleSessionStart } from "./recovery.js";
import { deleteStateFile, getStateFilePath, readStateRecords } from "./state.js";
import { deliverTurn, type DeliverFn } from "./trace.js";
import { splitTurns } from "./turns.js";
import type { CursorHookPayload } from "./types.js";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { error, info, loadEnvFile, readStdin, setDebug } from "./utils.js";

/** stdout JSON returned on fail-open for non-stop hooks. */
const FAIL_OPEN_EVENT_STDOUT = JSON.stringify({
  continue: true,
  permission: "allow",
});

/** stdout JSON returned on fail-open for stop hook. */
const FAIL_OPEN_STOP_STDOUT = JSON.stringify({});

function isStopEvent(eventName: string | undefined): boolean {
  return eventName === "stop" || eventName === "Stop";
}

function emitStdout(eventName: string | undefined): void {
  if (isStopEvent(eventName)) {
    process.stdout.write(FAIL_OPEN_STOP_STDOUT);
  } else {
    process.stdout.write(FAIL_OPEN_EVENT_STDOUT);
  }
}

/**
 * Handle the stop hook: read the state file, split into turns, build OTLP
 * and deliverTrace per turn, then delete the state file.
 * Delivery failure does not block deletion — OTLP is persisted by
 * langstash-deliver's Tier 3 data/failed/.
 */
async function handleStop(
  payload: CursorHookPayload,
  config: { max_chars: number; user_id?: string; tags?: string[] },
  deliverFn?: DeliverFn,
): Promise<void> {
  const conversationId = payload.conversation_id;
  if (!conversationId) {
    info("stop: no conversation_id in payload, skipping");
    return;
  }

  const stateFile = getStateFilePath(conversationId);
  const records = readStateRecords(stateFile);

  if (records.length === 0) {
    info(`stop: no records in state file ${stateFile}, deleting`);
    deleteStateFile(stateFile);
    return;
  }

  // Attach stop event status to the last turn
  const stopStatus = payload.status ?? "completed";

  const turns = splitTurns(records);
  if (turns.length > 0) {
    turns[turns.length - 1].cursorStatus = stopStatus;
  }

  for (let i = 0; i < turns.length; i++) {
    const turn = turns[i];
    const traceName = `Cursor - Turn ${i + 1}`;
    const delivered = await deliverTurn(turn, config as never, traceName, deliverFn);
    if (delivered) {
      info(`stop: delivered turn ${i + 1} for ${conversationId}`);
    } else {
      info(`stop: delivery failed for turn ${i + 1} (OTLP persisted in data/failed/)`);
    }
  }

  // Delete state file after all turns delivered (delivery failure does not block deletion)
  deleteStateFile(stateFile);
  info(`stop: deleted state file for ${conversationId}`);
}

/**
 * Main hook entry point.
 *
 * Reads stdin JSON, dispatches by hook_event_name:
 *   - 9 Agent events → append to state file, stdout {continue,allow}
 *   - stop → turn split + per-turn deliverTrace, stdout {}
 *   - sessionStart → orphan recovery scan, stdout {continue,allow}
 *
 * Fail-open: any exception → stderr log + appropriate stdout JSON + exit 0.
 * stdout contains only valid JSON; all logging goes to stderr.
 */
async function runHook(): Promise<void> {
  loadEnvFile();

  // Bootstrap diagnostics: check critical paths
  const envFile = join(homedir(), ".agent-exporter-to-langfuse", "config", "cursor.env");
  process.stderr.write("[cursor-langfuse] envFile=" + envFile + " exists=" + (existsSync(envFile) ? "Y" : "N") + "\n");
  process.stderr.write("[cursor-langfuse] LANGFUSE_PUBLIC_KEY=" + (process.env.LANGFUSE_PUBLIC_KEY ? "set" : "MISSING") + "\n");
  process.stderr.write("[cursor-langfuse] LANGFUSE_SECRET_KEY=" + (process.env.LANGFUSE_SECRET_KEY ? "set" : "MISSING") + "\n");
  process.stderr.write("[cursor-langfuse] LANGFUSE_BASE_URL=" + (process.env.LANGFUSE_BASE_URL ?? "MISSING") + "\n");

  info("cursor langfuse hook started");

  let payload: CursorHookPayload;
  try {
    payload = await readStdin<CursorHookPayload>();
  } catch (e) {
    error("failed to read hook stdin:", e);
    // Cannot determine event name — use event fail-open
    emitStdout(undefined);
    return;
  }

  const config = getConfig();
  setDebug(config.debug);

  if (!config.enabled) {
    info("tracing disabled (set TRACE_TO_LANGFUSE=true to enable)");
    emitStdout(payload.hook_event_name);
    return;
  }

  const eventName = payload.hook_event_name;

  try {
    if (isStopEvent(eventName)) {
      await handleStop(payload, config);
    } else if (eventName === "sessionStart") {
      await handleSessionStart(payload, config);
    } else if (eventName) {
      handleEvent(payload, config);
    } else {
      info("hook payload missing hook_event_name, skipping");
    }
  } catch (e) {
    error(`hook handler error for event ${eventName ?? "(unknown)"}:`, e);
  }

  // Always emit stdout JSON and exit 0 (fail-open)
  emitStdout(eventName);
}

const isMainModule = import.meta.url === `file://${process.argv[1]}`;

if (isMainModule) {
  runHook().catch((e) => {
    error("fatal:", e);
    // Last-resort fail-open: emit event stdout (not stop) and exit 0
    try {
      process.stdout.write(FAIL_OPEN_EVENT_STDOUT);
    } catch {}
  });
}

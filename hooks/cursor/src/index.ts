// Bootstrap: write to stderr immediately so we can tell if the hook was even invoked.
process.stderr.write("[cursor-langfuse] BOOT pid=" + process.pid + " cwd=" + process.cwd() + " home=" + (process.env.HOME ?? "unset") + " node=" + process.execPath + "\\n");

import { getConfig } from "./config.js";
import { handleEvent } from "./handlers.js";
import { handleSessionStart } from "./recovery.js";
import { deleteStateFile, getStateFilePath, readStateRecords } from "./state.js";
import { deliverTurn, type DeliverFn } from "./trace.js";
import { splitTurns } from "./turns.js";
import type { CursorHookPayload } from "./types.js";
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
    info("stop: no records in state file, deleting: " + stateFile);
    deleteStateFile(stateFile);
    return;
  }

  const stopStatus = payload.status ?? "completed";
  const turns = splitTurns(records);
  if (turns.length > 0) {
    turns[turns.length - 1].cursorStatus = stopStatus;
  }

  for (let i = 0; i < turns.length; i++) {
    const turn = turns[i];
    const traceName = "Cursor - Turn " + (i + 1);
    const delivered = await deliverTurn(turn, config as never, traceName, deliverFn);
    if (delivered) {
      info("stop: delivered turn " + (i + 1) + " for " + conversationId);
    } else {
      info("stop: delivery failed for turn " + (i + 1) + " (OTLP persisted in data/failed/)");
    }
  }

  deleteStateFile(stateFile);
  info("stop: deleted state file for " + conversationId);
}

async function runHook(): Promise<void> {
  loadEnvFile();
  info("cursor langfuse hook started");

  let payload: CursorHookPayload;
  try {
    payload = await readStdin<CursorHookPayload>();
  } catch (e) {
    error("failed to read hook stdin:", e);
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
    error("hook handler error for event " + (eventName ?? "(unknown)") + ":", e);
  }

  emitStdout(eventName);
}

const isMainModule = import.meta.url === "file://" + process.argv[1];

if (isMainModule) {
  runHook().catch((e) => {
    error("fatal:", e);
    try {
      process.stdout.write(FAIL_OPEN_EVENT_STDOUT);
    } catch {}
  });
}

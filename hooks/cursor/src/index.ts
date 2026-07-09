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

const FAIL_OPEN_EVENT_STDOUT = JSON.stringify({ continue: true, permission: "allow" });
const FAIL_OPEN_STOP_STDOUT = JSON.stringify({});

function isStopEvent(eventName: string | undefined): boolean {
  return eventName === "stop" || eventName === "Stop";
}

function emitStdout(eventName: string | undefined): void {
  process.stdout.write(isStopEvent(eventName) ? FAIL_OPEN_STOP_STDOUT : FAIL_OPEN_EVENT_STDOUT);
}

async function handleStop(payload: CursorHookPayload, config: any, deliverFn?: DeliverFn): Promise<void> {
  const conversationId = payload.conversation_id;
  if (!conversationId) { info("stop: no conversation_id, skipping"); return; }

  const stateFile = getStateFilePath(conversationId);
  const records = readStateRecords(stateFile);
  if (records.length === 0) { info("stop: no records, deleting: " + stateFile); deleteStateFile(stateFile); return; }

  const stopStatus = payload.status ?? "completed";
  const turns = splitTurns(records);
  if (turns.length > 0) turns[turns.length - 1].cursorStatus = stopStatus;

  for (let i = 0; i < turns.length; i++) {
    const turn = turns[i];
    const delivered = await deliverTurn(turn, config as never, "Cursor - Turn " + (i + 1), deliverFn);
    info("stop: " + (delivered ? "delivered" : "delivery failed") + " turn " + (i + 1) + " for " + conversationId);
  }
  deleteStateFile(stateFile);
  info("stop: deleted state file for " + conversationId);
}

/** Deliver subagent as a standalone trace immediately using summary from payload. */
async function handleSubagentStop(payload: any, config: any, deliverFn?: DeliverFn): Promise<void> {
  const subagentType = payload.subagent_type ?? "unknown";
  const summary = payload.summary ?? "";
  const task = payload.task ?? "";
  const status = payload.status ?? "unknown";
  const durationMs = payload.duration_ms ?? 0;
  info("subagentStop: type=" + subagentType + " status=" + status + " duration_ms=" + durationMs);

  if (!summary && !task) { info("subagentStop: no summary/task, skipping"); return; }

  const now = new Date().toISOString();
  const turn = {
    conversationId: payload.parent_conversation_id ?? payload.conversation_id ?? "subagent",
    events: [{
      hook_event_name: "subagentStop", generation_id: payload.generation_id,
      timestamp: now, conversation_id: payload.conversation_id,
      model: payload.model, user_email: payload.user_email,
      subagent_type: subagentType, status, summary, task, duration_ms: durationMs,
    }],
    userInput: task, finalOutput: summary, model: payload.model,
    startTime: new Date(Date.now() - durationMs).toISOString(), endTime: now,
    userEmail: payload.user_email, cursorStatus: status,
  };

  const traceName = "Cursor Subagent - " + subagentType;
  const delivered = await deliverTurn(turn as any, config as never, traceName, deliverFn);
  info("subagentStop: " + (delivered ? "delivered" : "delivery failed") + " trace");
}

async function runHook(): Promise<void> {
  loadEnvFile();
  info("cursor langfuse hook started");

  let payload: CursorHookPayload;
  try { payload = await readStdin<CursorHookPayload>(); }
  catch (e) { error("failed to read stdin:", e); emitStdout(undefined); return; }

  const config = getConfig();
  setDebug(config.debug);
  if (!config.enabled) { info("tracing disabled"); emitStdout(payload.hook_event_name); return; }

  const eventName = payload.hook_event_name;
  try {
    if (isStopEvent(eventName)) await handleStop(payload, config);
    else if (eventName === "subagentStop") await handleSubagentStop(payload, config);
    else if (eventName === "sessionStart") await handleSessionStart(payload, config);
    else if (eventName) handleEvent(payload, config);
    else info("missing hook_event_name, skipping");
  } catch (e) { error("handler error for " + (eventName ?? "?") + ":", e); }

  emitStdout(eventName);
}

const isMainModule = import.meta.url === "file://" + process.argv[1];
if (isMainModule) {
  runHook().catch((e) => { error("fatal:", e); try { process.stdout.write(FAIL_OPEN_EVENT_STDOUT); } catch {} process.exit(0); });
}

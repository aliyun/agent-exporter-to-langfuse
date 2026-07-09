import type { Config } from "./config.js";
import { deleteStateFile, getFileMtimeMs, listSessionFiles, readStateRecords } from "./state.js";
import type { CursorHookPayload } from "./types.js";
import { deliverTurn, type DeliverFn } from "./trace.js";
import { splitTurns } from "./turns.js";
import { info, warn } from "./utils.js";

/** Orphan threshold: 6 hours in milliseconds (hardcoded per spec R-5). */
const ORPHAN_THRESHOLD_MS = 6 * 60 * 60 * 1000;

/**
 * Handle the sessionStart hook: scan for orphaned state files older than 6
 * hours, recover each via turn-splitting + per-turn deliverTrace, and delete
 * on success. Skip the current session's file (match conversation_id from
 * stdin). Recovery failure retains the file for the next sessionStart.
 *
 * All errors are swallowed (fail-open) — recovery exceptions do not block
 * session start.
 */
export async function handleSessionStart(
  payload: CursorHookPayload,
  config: Config,
  deliverFn?: DeliverFn,
): Promise<void> {
  const now = Date.now();
  const currentConversationId = payload.conversation_id;

  let files: string[];
  try {
    files = listSessionFiles();
  } catch (e) {
    warn("recovery scan: failed to list session files:", e);
    return;
  }

  for (const file of files) {
    // Skip the current session's file
    const fileName = file.split("/").pop() ?? "";
    const fileConversationId = fileName.replace(/\.jsonl$/, "");
    if (currentConversationId && fileConversationId === currentConversationId) {
      continue;
    }

    // Check age threshold
    const mtime = getFileMtimeMs(file);
    if (mtime === 0) continue; // stat failed, skip
    const ageMs = now - mtime;
    if (ageMs < ORPHAN_THRESHOLD_MS) {
      continue; // under 6h threshold
    }

    info(`recovery: recovering orphaned session file: ${fileName} (age: ${Math.round(ageMs / 3600000)}h)`);

    try {
      const records = readStateRecords(file);
      if (records.length === 0) {
        // Empty file, just delete
        deleteStateFile(file);
        continue;
      }

      const turns = splitTurns(records);
      let allDelivered = true;

      for (let i = 0; i < turns.length; i++) {
        const turn = turns[i];
        // Recovery traces get cursor_status=unknown
        turn.cursorStatus = "unknown";
        const traceName = `Cursor - Turn ${i + 1}`;
        const delivered = await deliverTurn(turn, config, traceName, deliverFn);
        if (delivered) {
          info(`recovery: delivered turn ${i + 1} for ${fileName}`);
        } else {
          warn(`recovery: failed to deliver turn ${i + 1} for ${fileName}`);
          allDelivered = false;
        }
      }

      if (allDelivered) {
        deleteStateFile(file);
        info(`recovery: deleted recovered file: ${fileName}`);
      } else {
        // Retain file for next recovery attempt
        warn(`recovery: retaining ${fileName} due to delivery failure`);
      }
    } catch (e) {
      warn(`recovery: error processing ${fileName}:`, e);
      // Do not delete on error — retain for next attempt
    }
  }
}

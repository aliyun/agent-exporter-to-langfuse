import type { Config } from "./config.js";
import { appendStateRecord } from "./state.js";
import type { CursorHookPayload, StateRecord } from "./types.js";
import { toText, truncate } from "./utils.js";

/** Fields whose values are large text and may need truncation. */
const TRUNCATABLE_FIELDS = new Set([
  "prompt",
  "text",
  "output",
  "content",
  "result_json",
]);

/** Per-event business field mapping (from spec R-1). */
const EVENT_FIELDS: Record<string, string[]> = {
  beforeSubmitPrompt: ["prompt", "attachments", "model"],
  afterAgentResponse: ["text", "model"],
  afterAgentThought: ["text", "duration_ms"],
  beforeShellExecution: ["command", "cwd"],
  afterShellExecution: ["command", "output", "duration"],
  beforeMCPExecution: ["tool_name", "tool_input", "url", "command"],
  afterMCPExecution: ["tool_name", "tool_input", "result_json", "duration"],
  beforeReadFile: ["file_path", "content"],
  afterFileEdit: ["file_path", "edits"],
};

/**
 * Build a state record from a Cursor event payload.
 * Extracts hook_event_name, generation_id, timestamp, conversation_id,
 * model, user_email, and the event-specific business fields.
 * Large text fields are truncated at LANGFUSE_MAX_CHARS.
 */
export function buildStateRecord(
  payload: CursorHookPayload,
  config: Config,
  now: Date = new Date(),
): StateRecord {
  const eventName = payload.hook_event_name;
  const record: StateRecord = {
    hook_event_name: eventName,
    generation_id: payload.generation_id,
    timestamp: now.toISOString(),
    conversation_id: payload.conversation_id,
    model: payload.model,
    user_email: payload.user_email,
  };

  const fields = EVENT_FIELDS[eventName] ?? [];
  for (const field of fields) {
    const value = payload[field as keyof CursorHookPayload];
    if (value === undefined || value === null) continue;

    if (TRUNCATABLE_FIELDS.has(field) && typeof value === "string") {
      const { text, meta } = truncate(value, config.max_chars);
      record[field] = text;
      if (meta) {
        record[`${field}_truncated`] = true;
        record[`${field}_original_length`] = meta.originalLength;
      }
    } else if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      // Preserve primitive types (numbers like duration_ms, booleans)
      record[field] = value;
    } else {
      // Convert objects/arrays to text
      record[field] = toText(value);
    }
  }

  return record;
}

/**
 * Handle a Cursor Agent event hook.
 * Appends a JSONL record to the per-conversation state file.
 * Does NOT build OTLP or deliver — that happens in the stop hook (R-2).
 */
export function handleEvent(payload: CursorHookPayload, config: Config): void {
  const record = buildStateRecord(payload, config);
  appendStateRecord(record);
}

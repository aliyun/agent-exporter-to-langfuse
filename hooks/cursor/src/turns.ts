import type { StateRecord, Turn } from "./types.js";

/**
 * Split state file records into turns.
 *
 * Turn boundary rule (spec R-2):
 *   1. Each `beforeSubmitPrompt` event opens a new turn, closing at the
 *      next `beforeSubmitPrompt` or end-of-records.
 *   2. If no `beforeSubmitPrompt` events exist, degrade to splitting by
 *      `generation_id` changes.
 *   3. If still no split possible, treat all records as a single turn.
 *
 * The `stop` event is NOT stored in the state file (it triggers processing);
 * if present in records it is treated as a non-boundary event appended to
 * the current turn.
 */
export function splitTurns(records: StateRecord[]): Turn[] {
  if (records.length === 0) return [];

  const conversationId =
    records[0].conversation_id ?? "unknown";

  const hasBeforeSubmit = records.some(
    (r) => r.hook_event_name === "beforeSubmitPrompt",
  );

  if (hasBeforeSubmit) {
    return splitByBeforeSubmitPrompt(records, conversationId);
  }

  // Degrade: split by generation_id
  const hasGenerationId = records.some((r) => r.generation_id);
  if (hasGenerationId) {
    return splitByGenerationId(records, conversationId);
  }

  // Final degrade: single turn
  return [buildTurn(records, conversationId)];
}

function splitByBeforeSubmitPrompt(
  records: StateRecord[],
  conversationId: string,
): Turn[] {
  const turns: Turn[] = [];
  let current: StateRecord[] = [];

  for (const record of records) {
    if (
      record.hook_event_name === "beforeSubmitPrompt" &&
      current.length > 0
    ) {
      turns.push(buildTurn(current, conversationId));
      current = [];
    }
    current.push(record);
  }

  if (current.length > 0) {
    turns.push(buildTurn(current, conversationId));
  }

  return turns;
}

function splitByGenerationId(
  records: StateRecord[],
  conversationId: string,
): Turn[] {
  const turns: Turn[] = [];
  let current: StateRecord[] = [];
  let currentGenId: string | undefined = undefined;

  for (const record of records) {
    const genId = record.generation_id;
    if (genId && currentGenId && genId !== currentGenId && current.length > 0) {
      turns.push(buildTurn(current, conversationId));
      current = [];
    }
    current.push(record);
    if (genId) currentGenId = genId;
  }

  if (current.length > 0) {
    turns.push(buildTurn(current, conversationId));
  }

  return turns;
}

function buildTurn(events: StateRecord[], conversationId: string): Turn {
  const submitPrompt = events.find(
    (r) => r.hook_event_name === "beforeSubmitPrompt",
  );
  const response = events.find(
    (r) => r.hook_event_name === "afterAgentResponse",
  );
  const stopEvent = events.find((r) => r.hook_event_name === "stop");

  const userInput = submitPrompt
    ? (submitPrompt.prompt as string | undefined)
    : undefined;
  const finalOutput = response
    ? (response.text as string | undefined)
    : undefined;
  const model =
    (submitPrompt?.model as string | undefined) ??
    (response?.model as string | undefined);

  const startTime = events[0].timestamp;
  const endTime = events[events.length - 1].timestamp;

  const userEmail = events.find((r) => r.user_email)?.user_email as
    | string
    | undefined;

  const cursorStatus = stopEvent
    ? ((stopEvent.status as string | undefined) ?? "completed")
    : undefined;

  return {
    conversationId,
    events,
    userInput,
    finalOutput,
    model,
    startTime,
    endTime,
    userEmail,
    cursorStatus,
  };
}

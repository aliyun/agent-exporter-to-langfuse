/**
 * Turn-level span records so generations and tools can nest under a turn.
 * Ported from pi-langfuse src/handlers/turn.ts.
 */
import { shapePayload } from "../shape.ts";
import { endSpanRecord, getSessionRunState, startSpanRecord } from "../state.ts";

export function startTurnObservation(event: Record<string, unknown>) {
  const session = getSessionRunState();
  const run = session.run;
  if (!run) {
    return;
  }

  // If a turn is already active, close it (fallback safety)
  if (run.activeTurn) {
    endSpanRecord(run.activeTurn);
    run.activeTurn = undefined;
  }

  try {
    const turnIndex = event.turnIndex ?? session.turnCount;
    run.activeTurn = startSpanRecord(run, {
      parentSpanId: run.root.spanId,
      name: "turn",
      type: "span",
      input: shapePayload(event.context ?? event),
      metadata: { turnIndex },
    });
  } catch (e) {
    console.warn("📊 Langfuse: Failed to start turn observation", e);
  }
}

export function finishTurnObservation(_event?: Record<string, unknown>) {
  const run = getSessionRunState().run;
  if (!run?.activeTurn) {
    return;
  }

  try {
    endSpanRecord(run.activeTurn);
    run.activeTurn = undefined;
  } catch (e) {
    console.warn("📊 Langfuse: Failed to finish turn observation", e);
  }
}

/** session_compact produces a marker span in the current trace. */
export function recordSessionCompact(event: Record<string, unknown>) {
  const run = getSessionRunState().run;
  if (!run) {
    return;
  }

  try {
    const parent = run.activeTurn ?? run.root;
    const marker = startSpanRecord(run, {
      parentSpanId: parent.spanId,
      name: "session_compact",
      type: "span",
      statusMessage: "Context was compacted",
      metadata: shapePayload({ ...event }) as Record<string, unknown>,
    });
    endSpanRecord(marker);
  } catch (e) {
    console.warn("📊 Langfuse: Failed to record session_compact", e);
  }
}

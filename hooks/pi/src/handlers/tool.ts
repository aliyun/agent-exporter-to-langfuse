/**
 * Tool observation lifecycle keyed by toolCallId (concurrent-safe),
 * plus dangling-observation cleanup. Ported from pi-langfuse
 * src/handlers/tool.ts onto span records.
 */
import { redactString } from "../redaction.ts";
import { shapePayload, truncate } from "../shape.ts";
import { endSpanRecord, getSessionRunState, startSpanRecord } from "../state.ts";
import {
  extractTextContent,
  getToolCallId,
  getToolInput,
  getToolName,
  toolPayloadLimit,
} from "./extract.ts";

export function startToolObservation(event: Record<string, unknown>) {
  const session = getSessionRunState();
  const run = session.run;
  if (!run) {
    return;
  }

  const toolCallId = getToolCallId(event);
  if (!toolCallId || run.activeTools.has(toolCallId)) {
    return;
  }

  try {
    const toolName = getToolName(event);
    const shapedInput = shapePayload(getToolInput(event), { maxString: toolPayloadLimit() });

    const parent = run.activeTurn ?? run.root;
    const record = startSpanRecord(run, {
      parentSpanId: parent.spanId,
      name: toolName,
      type: "tool",
      input: shapedInput,
      metadata: { toolName, toolCallId },
    });

    session.toolCallCount++;
    run.activeTools.set(toolCallId, {
      record,
      toolName,
      ended: false,
      startedAt: Date.now(),
    });
  } catch (e) {
    console.warn("📊 Langfuse: Failed to start tool observation", e);
  }
}

export function finishToolObservation(event: Record<string, unknown>) {
  const session = getSessionRunState();
  const run = session.run;
  if (!run) {
    return;
  }

  const toolCallId = getToolCallId(event);
  if (!toolCallId) {
    return;
  }

  const activeTool = run.activeTools.get(toolCallId);
  if (!activeTool || activeTool.ended) {
    return;
  }

  const isError = Boolean(event.isError ?? event.error ?? event.status === "error");
  const output =
    extractTextContent(event.content, toolPayloadLimit()) ??
    event.output ??
    event.result ??
    event.error ??
    event.content ??
    event;

  try {
    const durationMs = Math.max(0, Date.now() - activeTool.startedAt);

    activeTool.record.output = shapePayload(output, { maxString: toolPayloadLimit() });
    activeTool.record.level = isError ? "ERROR" : "DEFAULT";
    if (isError) {
      activeTool.record.statusMessage = redactString(truncate(String(event.error ?? output), 1_000));
    }
    activeTool.record.metadata = {
      ...(activeTool.record.metadata ?? {}),
      isError,
      durationMs,
    };
    endSpanRecord(activeTool.record);
    activeTool.ended = true;

    if (isError) {
      session.errorCount++;
    }
  } catch (e) {
    console.warn("📊 Langfuse: Failed to finish tool observation", e);
  } finally {
    run.activeTools.delete(toolCallId);
  }
}

/** Close all dangling tool/generation records with WARNING before emission. */
export function closeDanglingObservations(statusMessage: string) {
  const run = getSessionRunState().run;
  if (!run) {
    return;
  }

  for (const activeTool of run.activeTools.values()) {
    if (!activeTool.ended) {
      activeTool.record.level = "WARNING";
      activeTool.record.statusMessage = statusMessage;
      activeTool.record.metadata = {
        ...(activeTool.record.metadata ?? {}),
        cancelled: true,
      };
      endSpanRecord(activeTool.record);
      activeTool.ended = true;
    }
  }

  for (const track of run.activeGenerations.values()) {
    if (!track.ended) {
      track.record.level = "WARNING";
      track.record.statusMessage = statusMessage;
      track.record.metadata = {
        ...(track.record.metadata ?? {}),
        cancelled: true,
      };
      endSpanRecord(track.record);
      track.ended = true;
    }
  }

  run.activeTools.clear();
}

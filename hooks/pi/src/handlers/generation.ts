/**
 * Generation lifecycle: provider request start, response metadata and
 * early error status, TTFT, completion, and turn-level fallback generation.
 * Ported from pi-langfuse src/handlers/generation.ts onto span records.
 */
import { shapePayload } from "../shape.ts";
import {
  endSpanRecord,
  getSessionRunState,
  startSpanRecord,
  type GenerationTrack,
} from "../state.ts";
import {
  extractAssistantOutput,
  extractCostDetails,
  extractModelParameters,
  extractResponseMetadata,
  extractUsage,
  getMessageFromEvent,
  getProviderPayload,
  getRequestKey,
} from "./extract.ts";

export function getOpenGeneration(): GenerationTrack | undefined {
  const run = getSessionRunState().run;
  if (!run) {
    return undefined;
  }

  for (let i = run.generationOrder.length - 1; i >= 0; i--) {
    const track = run.activeGenerations.get(run.generationOrder[i]);
    if (track && !track.ended) {
      return track;
    }
  }

  return undefined;
}

export function startGeneration(event: Record<string, unknown>) {
  const session = getSessionRunState();
  const run = session.run;
  if (!run) {
    return;
  }

  try {
    const key = getRequestKey(event, `generation-${++run.generationSeq}`);
    if (run.activeGenerations.has(key)) {
      return;
    }
    const payload = getProviderPayload(event);
    const modelParameters = extractModelParameters(payload);
    const model = String(event.model ?? event.modelId ?? session.currentModel ?? "");
    const provider = String(event.provider ?? session.currentProvider ?? "");
    const metadata = shapePayload({
      provider,
      requestId: key,
      url: event.url,
      method: event.method,
    }) as Record<string, unknown>;

    const parent = run.activeTurn ?? run.root;
    const record = startSpanRecord(run, {
      parentSpanId: parent.spanId,
      name: "llm-generation",
      type: "generation",
      input: shapePayload(payload),
      model: model || undefined,
      modelParameters,
      metadata,
    });

    run.activeGenerations.set(key, { record, requestKey: key, ended: false });
    run.generationOrder.push(key);
  } catch (e) {
    console.warn("📊 Langfuse: Failed to start generation", e);
  }
}

export function updateGenerationMetadata(event: Record<string, unknown>) {
  const run = getSessionRunState().run;
  if (!run) {
    return;
  }

  try {
    const key = getRequestKey(event, "");
    const metadata = extractResponseMetadata(event);
    const track = (key ? run.activeGenerations.get(key) : undefined) ?? getOpenGeneration();
    if (!track) {
      return;
    }

    track.record.metadata = { ...(track.record.metadata ?? {}), ...metadata };

    const isError =
      (typeof metadata.status === "number" && metadata.status >= 400) ||
      event.error ||
      event.isError;

    if (isError) {
      track.record.level = "ERROR";
      track.record.statusMessage = String(event.error ?? metadata.statusMessage ?? "Provider request failed");
      endSpanRecord(track.record);
      track.ended = true;
    }
  } catch (e) {
    console.warn("📊 Langfuse: Failed to update generation metadata", e);
  }
}

export function recordTTFT(event: Record<string, unknown>) {
  const run = getSessionRunState().run;
  if (!run) {
    return;
  }

  const key = getRequestKey(event, "");
  const track = (key ? run.activeGenerations.get(key) : undefined) ?? getOpenGeneration();

  if (track && !track.ttftRecorded && !track.ended) {
    track.ttftRecorded = true;
    track.record.completionStartTimeMs = Date.now();
  }
}

export function finishGenerationFromMessage(event: Record<string, unknown>) {
  const session = getSessionRunState();
  const run = session.run;
  if (!run) {
    return;
  }

  const message = getMessageFromEvent(event);
  if (!message || message.role !== "assistant") {
    return;
  }

  try {
    const output = extractAssistantOutput(message);
    run.latestAssistantOutput = output;

    const track = getOpenGeneration();
    if (!track) {
      return;
    }

    const usageDetails = extractUsage({ ...event, message });
    const costDetails = extractCostDetails({ ...event, message });
    const modelParameters = extractModelParameters(getProviderPayload(event)) ?? track.record.modelParameters;
    const model = String(message.model ?? event.model ?? session.currentModel ?? "");

    track.record.output = shapePayload(output, { parseJson: false });
    track.record.model = model || track.record.model;
    track.record.modelParameters = modelParameters;
    track.record.usageDetails = usageDetails ?? track.record.usageDetails;
    if (costDetails) {
      track.record.costDetails = costDetails;
    }
    track.record.metadata = {
      ...(track.record.metadata ?? {}),
      finishReason: message.finishReason ?? message.stopReason ?? event.finishReason,
    };
    endSpanRecord(track.record);
    track.ended = true;
  } catch (e) {
    console.warn("📊 Langfuse: Failed to finish generation", e);
  }
}

export function createFallbackGenerationFromTurn(event: Record<string, unknown>, message: Record<string, unknown>) {
  const session = getSessionRunState();
  const run = session.run;
  if (!run || run.generationOrder.length > 0) {
    return;
  }

  try {
    const usageDetails = extractUsage({ ...event, message });
    const costDetails = extractCostDetails({ ...event, message });
    const modelParameters = extractModelParameters(getProviderPayload(event));
    const model = String(message.model ?? event.model ?? session.currentModel ?? "");

    const parent = run.activeTurn ?? run.root;
    const record = startSpanRecord(run, {
      parentSpanId: parent.spanId,
      name: "llm-generation",
      type: "generation",
      input: run.promptInput,
      output: shapePayload(extractAssistantOutput(message), { parseJson: false }),
      model: model || undefined,
      modelParameters,
      usageDetails,
      ...(costDetails ? { costDetails } : {}),
      metadata: {
        ...(session.currentProvider ? { provider: session.currentProvider } : {}),
        sourceEvent: "turn_end",
      },
    });
    endSpanRecord(record);
    run.generationOrder.push("turn-end-fallback");
  } catch (e) {
    console.warn("📊 Langfuse: Failed to create fallback generation", e);
  }
}

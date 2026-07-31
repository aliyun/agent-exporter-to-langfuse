/**
 * Root agent-run lifecycle: idempotent run creation at
 * before_agent_start/agent_start and finalization at agent_end.
 * Ported from pi-langfuse src/handlers/agent.ts onto span records.
 */
import { shapePayload, truncate } from "../shape.ts";
import { getLimits } from "../limits.ts";
import {
  computeEvaluationScores,
  currentSessionId,
  endSpanRecord,
  getSessionRunState,
  newTraceId,
  resetRunState,
  startSpanRecord,
  type RunState,
} from "../state.ts";
import { extractAssistantOutput, extractFinalAssistant } from "./extract.ts";
import { closeDanglingObservations } from "./tool.ts";

export function startAgentRun(event: Record<string, unknown>, ctx: any): RunState | null {
  const session = getSessionRunState();
  if (session.run && !session.run.emitted) {
    return session.run;
  }

  try {
    const cwd = String(
      (event.systemPromptOptions && typeof event.systemPromptOptions === "object"
        ? (event.systemPromptOptions as Record<string, unknown>).cwd
        : undefined) ?? process.cwd(),
    );

    if (!session.currentModel && ctx?.model) {
      session.currentModel = ctx.model.id || "";
      session.currentProvider = ctx.model.provider || "";
    }

    const promptInput = shapePayload({
      prompt: event.prompt,
      images: event.images,
      context: event.context ?? event.attachments,
    });

    const run: RunState = {
      traceId: newTraceId(),
      root: undefined as unknown as RunState["root"],
      spans: [],
      activeGenerations: new Map(),
      generationOrder: [],
      activeTools: new Map(),
      generationSeq: 0,
      promptInput,
      cwd,
      emitted: false,
    };

    run.root = startSpanRecord(run, {
      name: "pi-agent",
      type: "agent",
      input: promptInput,
      metadata: {
        cwd,
        ...(session.currentModel ? { model: session.currentModel } : {}),
        ...(session.currentProvider ? { provider: session.currentProvider } : {}),
        ...(currentSessionId() ? { sessionId: truncate(currentSessionId(), 200) } : {}),
      },
    });

    session.run = run;
    return run;
  } catch (e) {
    console.warn("📊 Langfuse: Failed to start agent run", e);
    return null;
  }
}

/**
 * Finalize the run's record tree at agent_end: close dangling observations
 * with WARNING, set the final output and metadata (including the score
 * mirror), and return the finished run. Emission is owned by the caller.
 */
export function finishAgentRun(event: Record<string, unknown> = {}): RunState | null {
  const session = getSessionRunState();
  const run = session.run;
  if (!run) {
    resetRunState();
    return null;
  }

  try {
    const lastAssistant = extractFinalAssistant(event.messages);
    const rawOutput = lastAssistant ? extractAssistantOutput(lastAssistant) : run.latestAssistantOutput;
    const output = rawOutput !== undefined ? shapePayload(rawOutput, { maxString: getLimits().maxString }) : undefined;

    closeDanglingObservations("Agent run ended before observation finalized");

    if (run.activeTurn) {
      endSpanRecord(run.activeTurn);
      run.activeTurn = undefined;
    }

    run.root.output = output;
    run.root.metadata = {
      ...(run.root.metadata ?? {}),
      completed: true,
      ...(session.currentModel ? { model: session.currentModel } : {}),
      ...(session.currentProvider ? { provider: session.currentProvider } : {}),
      totalTools: session.toolCallCount,
      ...computeEvaluationScores(),
    };
    endSpanRecord(run.root);
    return run;
  } catch (e) {
    console.warn("📊 Langfuse: Failed to finish agent run", e);
    return run;
  }
}

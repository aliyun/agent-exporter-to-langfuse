/**
 * Langfuse trace-delivery extension for Pi Coding Agent
 * (agent-exporter-to-langfuse pi hook).
 *
 * Accumulates one in-memory span record tree per Pi agent run and emits it
 * as a single OTLP JSON trace through the shared three-tier delivery at
 * agent_end. No Langfuse SDK, no interactive commands: runtime config
 * comes only from ~/.agent-exporter-to-langfuse/config/pi.env.
 */
import { basename } from "node:path";

import { loadPiEnv } from "./src/config.ts";
import { clearCheckpoint, recoverCheckpoints, writeCheckpoint } from "./src/checkpoint.ts";
import { emitRun } from "./src/emit.ts";
import {
  currentSessionId,
  endSpanRecord,
  getSessionRunState,
  resetRunState,
  runWithSession,
  setCurrentSession,
} from "./src/state.ts";
import { startAgentRun, finishAgentRun } from "./src/handlers/agent.ts";
import {
  startTurnObservation,
  finishTurnObservation,
  recordSessionCompact,
} from "./src/handlers/turn.ts";
import {
  startGeneration,
  updateGenerationMetadata,
  finishGenerationFromMessage,
  createFallbackGenerationFromTurn,
  recordTTFT,
} from "./src/handlers/generation.ts";
import {
  startToolObservation,
  finishToolObservation,
  closeDanglingObservations,
} from "./src/handlers/tool.ts";
import { extractAssistantOutput, getMessageFromEvent } from "./src/handlers/extract.ts";

export default async function (pi: any) {
  loadPiEnv();

  // Re-deliver any run interrupted by a crash/kill before this load.
  for (const { run, sessionId } of recoverCheckpoints()) {
    await emitRun(run, { sessionId });
  }

  const getSessionId = (ctx?: any) => {
    try {
      const sessionFile = ctx?.sessionManager?.getSessionFile?.();
      return sessionFile ? basename(sessionFile, ".jsonl") : undefined;
    } catch {
      return undefined;
    }
  };

  const withSession = <T>(ctx: any, fn: () => T): T =>
    runWithSession(getSessionId(ctx) ?? currentSessionId(), fn);

  /** Every handler is fail-open: hook errors never propagate into Pi. */
  const guard = (label: string, fn: (event: any, ctx: any) => unknown) => {
    return async (event: any, ctx: any) => {
      try {
        await withSession(ctx, () => fn(event ?? {}, ctx));
      } catch (e) {
        console.warn(`📊 Langfuse: ${label} handler failed`, e);
      }
    };
  };

  pi.on("session_start", guard("session_start", () => {
    resetRunState();
  }));

  pi.on("model_select", guard("model_select", (event) => {
    const session = getSessionRunState();
    session.currentModel = event.model?.id || "";
    session.currentProvider = event.model?.provider || "";
  }));

  pi.on("before_agent_start", guard("before_agent_start", (event, ctx) => {
    startAgentRun(event, ctx);
  }));

  pi.on("agent_start", guard("agent_start", (event, ctx) => {
    startAgentRun(event, ctx);
  }));

  pi.on("turn_start", guard("turn_start", (event) => {
    startTurnObservation(event);
  }));

  pi.on("before_provider_request", guard("before_provider_request", (event) => {
    startGeneration(event);
  }));

  pi.on("after_provider_response", guard("after_provider_response", (event) => {
    updateGenerationMetadata(event);
  }));

  pi.on("message_update", guard("message_update", (event) => {
    recordTTFT(event);
    const message = getMessageFromEvent(event);
    const run = getSessionRunState().run;
    if (message?.role === "assistant" && run) {
      run.latestAssistantOutput = extractAssistantOutput(message);
    }
  }));

  pi.on("message_end", guard("message_end", (event) => {
    finishGenerationFromMessage(event);
  }));

  pi.on("tool_execution_start", guard("tool_execution_start", (event) => {
    startToolObservation(event);
  }));

  pi.on("tool_call", guard("tool_call", (event) => {
    startToolObservation(event);
  }));

  pi.on("tool_result", guard("tool_result", (event) => {
    finishToolObservation(event);
  }));

  pi.on("tool_execution_end", guard("tool_execution_end", (event) => {
    finishToolObservation(event);
  }));

  pi.on("turn_end", guard("turn_end", (event) => {
    const session = getSessionRunState();
    session.turnCount++;
    const message = getMessageFromEvent(event);
    if (message?.role === "assistant") {
      createFallbackGenerationFromTurn(event, message);
      finishGenerationFromMessage(event);
    }
    finishTurnObservation(event);
    if (session.run) {
      writeCheckpoint(session.run, currentSessionId());
    }
  }));

  pi.on("agent_end", guard("agent_end", async (event) => {
    const run = finishAgentRun(event);
    const sessionId = currentSessionId();
    clearCheckpoint(sessionId);
    if (run) {
      // Emitted before the session state is reset so score aggregates stay available.
      await emitRun(run, { sessionId, withScores: true });
    }
    resetRunState();
  }));

  pi.on("session_before_switch", async (_event: any, ctx: any) => {
    const sessionId = getSessionId(ctx);
    if (sessionId) {
      setCurrentSession(sessionId);
    }
  });

  pi.on("session_before_fork", async (_event: any, ctx: any) => {
    const sessionId = getSessionId(ctx);
    if (sessionId) {
      setCurrentSession(sessionId);
    }
  });

  pi.on("session_compact", guard("session_compact", (event) => {
    recordSessionCompact(event);
  }));

  pi.on("session_shutdown", guard("session_shutdown", async () => {
    const session = getSessionRunState();
    const run = session.run;
    const sessionId = currentSessionId();
    if (run && !run.emitted) {
      closeDanglingObservations("Session shutdown before agent completed");
      if (run.activeTurn) {
        run.activeTurn.level = "WARNING";
        run.activeTurn.metadata = { ...(run.activeTurn.metadata ?? {}), cancelled: true };
        endSpanRecord(run.activeTurn);
        run.activeTurn = undefined;
      }
      run.root.metadata = {
        ...(run.root.metadata ?? {}),
        completed: false,
        cancelled: true,
      };
      endSpanRecord(run.root);
      await emitRun(run, { sessionId, withScores: true });
      clearCheckpoint(sessionId);
      resetRunState();
    } else {
      resetRunState();
    }
  }));
}

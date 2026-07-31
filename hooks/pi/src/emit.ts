/**
 * Emit one run as a single OTLP JSON trace through the shared three-tier
 * delivery (local exporter buffer -> Langfuse OTel endpoint -> failed log).
 * Build or delivery failures are logged only and never propagate into Pi.
 *
 * For a live run (`withScores`), the aggregate score values are mirrored into
 * the root metadata before the payload is built and the scores themselves are
 * sent best-effort after delivery returns, so score outcome never affects the
 * reported trace result. A checkpoint-recovered run has no live session
 * counters, so it keeps the mirrored values captured in the checkpoint and
 * sends no scores.
 */
import { deliverTrace, type FetchFn } from "../../langstash-deliver/typescript/src/index.ts";

import { resolveTags, resolveUserId } from "./config.ts";
import { buildOtlpJson } from "./otlp.ts";
import { mirrorScoresIntoRootMetadata, sendScores } from "./score.ts";
import type { RunState } from "./state.ts";

export interface EmitOptions {
  sessionId?: string;
  fetchFn?: FetchFn;
  withScores?: boolean;
  scoreFetchFn?: FetchFn;
}

export interface EmitResult {
  delivered: boolean;
  otlpJson: Record<string, unknown> | null;
}

export async function emitRun(run: RunState, options: EmitOptions = {}): Promise<EmitResult> {
  if (run.emitted) {
    return { delivered: false, otlpJson: null };
  }
  run.emitted = true;

  let otlpJson: Record<string, unknown>;
  try {
    if (options.withScores) {
      mirrorScoresIntoRootMetadata(run, options.sessionId);
    }
    otlpJson = buildOtlpJson(run, {
      sessionId: options.sessionId || undefined,
      userId: resolveUserId(),
      tags: resolveTags(),
    });
  } catch (e) {
    console.warn("📊 Langfuse: Failed to build OTLP payload", e);
    return { delivered: false, otlpJson: null };
  }

  let delivered = false;
  try {
    delivered = await deliverTrace(
      otlpJson,
      options.fetchFn ? { fetchFn: options.fetchFn } : undefined,
    );
  } catch (e) {
    console.warn("📊 Langfuse: Failed to deliver trace", e);
  }

  if (options.withScores) {
    await sendScores(run, {
      sessionId: options.sessionId,
      fetchFn: options.scoreFetchFn,
    });
  }

  return { delivered, otlpJson };
}

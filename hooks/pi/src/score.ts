/**
 * Best-effort score delivery (R-5). Langfuse Scores are not an OTel span
 * concept, so they cannot travel through the OTLP delivery chain; they are
 * POSTed directly to {LANGFUSE_BASE_URL}/api/public/ingestion using the same
 * credentials as delivery Tier 2. This is the only exemption from R-1's
 * "never bypass deliverTrace()" rule. A thrown fetch (e.g. a stale keep-alive
 * socket surfacing as `connect EBADF`) is retried once on a fresh connection;
 * after that failures are logged as a single concise line and dropped: no
 * buffering, and never any effect on the trace delivery result. The same
 * aggregate values are mirrored into the root observation metadata.
 */
import { randomUUID } from "node:crypto";

import type { FetchFn } from "../../langstash-deliver/typescript/src/index.ts";
import { computeEvaluationScores, type RunState } from "./state.ts";

export type ScoreDataType = "NUMERIC" | "BOOLEAN";

export interface ScoreEntry {
  name: string;
  value: number;
  dataType: ScoreDataType;
  traceId: string;
  observationId?: string;
}

/** Aggregate trace-level scores plus one tool-level score per failed tool observation. */
export function collectScores(run: RunState, sessionId?: string): ScoreEntry[] {
  const aggregates = computeEvaluationScores(sessionId);
  const entries: ScoreEntry[] = [
    { name: "tool_call_count", value: aggregates.tool_call_count, dataType: "NUMERIC", traceId: run.traceId },
    { name: "turn_count", value: aggregates.turn_count, dataType: "NUMERIC", traceId: run.traceId },
    { name: "total_tool_errors", value: aggregates.total_tool_errors, dataType: "NUMERIC", traceId: run.traceId },
    { name: "tool_success_rate", value: aggregates.tool_success_rate, dataType: "NUMERIC", traceId: run.traceId },
    { name: "session_had_errors", value: aggregates.session_had_errors, dataType: "BOOLEAN", traceId: run.traceId },
  ];

  for (const record of run.spans) {
    if (record.type === "tool" && record.level === "ERROR") {
      entries.push({
        name: "tool_is_error",
        value: 1,
        dataType: "BOOLEAN",
        traceId: run.traceId,
        observationId: record.spanId,
      });
    }
  }

  return entries;
}

/** Mirror the aggregate values into the root observation metadata as the fallback. */
export function mirrorScoresIntoRootMetadata(run: RunState, sessionId?: string): void {
  run.root.metadata = {
    ...(run.root.metadata ?? {}),
    ...computeEvaluationScores(sessionId),
  };
}

export interface SendScoresOptions {
  sessionId?: string;
  fetchFn?: FetchFn;
}

const SCORE_REQUEST_TIMEOUT_MS = 10_000;
const SCORE_RETRY_DELAY_MS = 200;

const defaultFetch: FetchFn = async (url, options) => {
  const resp = await fetch(url, {
    method: options.method,
    headers: options.headers,
    body: options.body,
    signal: AbortSignal.timeout(SCORE_REQUEST_TIMEOUT_MS),
  });
  return { ok: resp.ok, status: resp.status };
};

/** One concise line for the warn log: no stack trace dump into the Pi terminal. */
function describeError(e: unknown): string {
  if (!(e instanceof Error)) {
    return String(e);
  }
  const cause = (e as { cause?: unknown }).cause;
  const causeText = cause instanceof Error ? ` (cause: ${cause.message})` : "";
  return `${e.name}: ${e.message}${causeText}`;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Send the scores for an already-delivered run. Returns true only when the
 * request succeeded; a missing base URL or credentials skips sending entirely.
 */
export async function sendScores(
  run: RunState,
  options: SendScoresOptions = {},
): Promise<boolean> {
  const baseUrl = (process.env.LANGFUSE_BASE_URL || "").replace(/\/+$/, "");
  const publicKey = process.env.LANGFUSE_PUBLIC_KEY || "";
  const secretKey = process.env.LANGFUSE_SECRET_KEY || "";
  if (!baseUrl || !publicKey || !secretKey) {
    return false;
  }

  let request: { url: string; options: Parameters<FetchFn>[1] };
  try {
    const timestamp = new Date().toISOString();
    const batch = collectScores(run, options.sessionId).map((entry) => ({
      id: randomUUID(),
      type: "score-create",
      timestamp,
      body: {
        name: entry.name,
        value: entry.value,
        dataType: entry.dataType,
        traceId: entry.traceId,
        ...(entry.observationId ? { observationId: entry.observationId } : {}),
      },
    }));

    const credentials = Buffer.from(`${publicKey}:${secretKey}`).toString("base64");
    request = {
      url: `${baseUrl}/api/public/ingestion`,
      options: {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${credentials}`,
        },
        body: JSON.stringify({ batch }),
      },
    };
  } catch (e) {
    console.warn(`📊 Langfuse: Failed to build score batch (dropped, trace unaffected): ${describeError(e)}`);
    return false;
  }

  // A thrown fetch is usually a transient socket problem (stale keep-alive
  // connection reused right after the trace POST → `connect EBADF`); one
  // retry on a fresh connection recovers it. HTTP errors are not retried.
  const doFetch = options.fetchFn ?? defaultFetch;
  let lastError: unknown;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt > 0) {
      await delay(SCORE_RETRY_DELAY_MS);
    }
    try {
      const resp = await doFetch(request.url, request.options);
      if (!resp.ok) {
        console.warn(`📊 Langfuse: Score request rejected with HTTP ${resp.status} (dropped, trace unaffected)`);
      }
      return resp.ok;
    } catch (e) {
      lastError = e;
    }
  }

  console.warn(`📊 Langfuse: Failed to send scores (dropped, trace unaffected): ${describeError(lastError)}`);
  return false;
}

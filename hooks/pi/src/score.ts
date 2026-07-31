/**
 * Best-effort score delivery (R-5). Langfuse Scores are not an OTel span
 * concept, so they cannot travel through the OTLP delivery chain; they are
 * POSTed directly to {LANGFUSE_BASE_URL}/api/public/ingestion using the same
 * credentials as delivery Tier 2. This is the only exemption from R-1's
 * "never bypass deliverTrace()" rule. Failures are logged and dropped: no
 * retry, no buffering, and never any effect on the trace delivery result.
 * The same aggregate values are mirrored into the root observation metadata.
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

const defaultFetch: FetchFn = async (url, options) => {
  const resp = await fetch(url, {
    method: options.method,
    headers: options.headers,
    body: options.body,
  });
  return { ok: resp.ok, status: resp.status };
};

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

    const doFetch = options.fetchFn ?? defaultFetch;
    const credentials = Buffer.from(`${publicKey}:${secretKey}`).toString("base64");
    const resp = await doFetch(`${baseUrl}/api/public/ingestion`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Basic ${credentials}`,
      },
      body: JSON.stringify({ batch }),
    });
    return resp.ok;
  } catch (e) {
    console.warn("📊 Langfuse: Failed to send scores (dropped, trace unaffected)", e);
    return false;
  }
}

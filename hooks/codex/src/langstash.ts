import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { randomUUID } from "node:crypto";

import type { Config } from "./config.js";
import type { ModelStep, SessionMeta, TokenUsage, Turn } from "./types.js";
import { error, warn, toText, truncate } from "./utils.js";

const FAILED_DIR = join(homedir(), ".agent-exporter-to-langfuse", "data", "failed");

type TraceV2Usage = {
  input?: number;
  output?: number;
  cache_read_input_tokens?: number;
};

function toUsage(usage: TokenUsage | undefined): TraceV2Usage | undefined {
  if (!usage) return undefined;
  const result: TraceV2Usage = {};
  if (typeof usage.input_tokens === "number") result.input = usage.input_tokens;
  if (typeof usage.output_tokens === "number") result.output = usage.output_tokens;
  if (typeof usage.cached_input_tokens === "number") {
    result.cache_read_input_tokens = usage.cached_input_tokens;
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

function clipText(value: string, maxChars: number): string {
  const { text, meta } = truncate(value, maxChars);
  return meta ? `${text}\n…[truncated ${meta.originalLength - text.length} chars]` : text;
}

function buildGenerationOutput(
  step: ModelStep,
  maxChars: number,
): Record<string, unknown> | undefined {
  const output: Record<string, unknown> = {};
  if (step.text) output.content = clipText(step.text, maxChars);
  if (step.reasoning) output.reasoning = clipText(step.reasoning, maxChars);
  if (step.toolCalls.length > 0) {
    output.tool_calls = step.toolCalls.map((tc) => ({
      id: tc.callId,
      name: tc.name,
      arguments: tc.args,
    }));
  }
  return Object.keys(output).length > 0 ? output : undefined;
}

export function buildTraceV2(
  turn: Turn,
  sessionMeta: SessionMeta,
  config: Config,
  traceName: string,
): Record<string, unknown> {
  const maxChars = config.max_chars;

  const generations: Record<string, unknown>[] = [];
  const spans: Record<string, unknown>[] = [];

  let previousToolResults: unknown = undefined;

  for (let i = 0; i < turn.steps.length; i++) {
    const step = turn.steps[i];

    const genInput =
      i === 0
        ? turn.userInput != null
          ? { role: "user", content: clipText(turn.userInput, maxChars) }
          : undefined
        : previousToolResults;

    const genOutput = buildGenerationOutput(step, maxChars);
    const usage = toUsage(step.usage);

    generations.push({
      name: turn.model ?? "codex.generation",
      model: turn.model || "unknown",
      start_time: new Date(step.startTime).toISOString(),
      end_time: new Date(step.endTime).toISOString(),
      ...(genInput != null ? { input: genInput } : {}),
      output: genOutput ?? {},
      ...(usage ? { usage } : {}),
      metadata: { "codex.step_index": i },
    });

    for (const tc of step.toolCalls) {
      const tcOutput = tc.output != null ? clipText(toText(tc.output), maxChars) : undefined;
      spans.push({
        name: tc.name || "tool",
        generation_index: i,
        start_time: new Date(tc.startTime).toISOString(),
        end_time: new Date(tc.endTime ?? step.endTime).toISOString(),
        ...(tc.args != null ? { input: tc.args } : {}),
        ...(tcOutput != null ? { output: tcOutput } : {}),
        metadata: {
          "codex.call_id": tc.callId,
          ...(tc.error ? { error: clipText(tc.error, maxChars) } : {}),
        },
      });
    }

    previousToolResults =
      step.toolCalls.length > 0
        ? step.toolCalls.map((tc) => ({
            name: tc.name,
            output: tc.output != null ? clipText(toText(tc.output), maxChars) : undefined,
            ...(tc.error ? { error: clipText(tc.error, maxChars) } : {}),
          }))
        : undefined;
  }

  return {
    schema_version: "2",
    id: randomUUID(),
    source: "codex",
    session_id: sessionMeta.sessionId,
    ...(config.user_id ? { user_id: config.user_id } : {}),
    ...(config.tags?.length ? { tags: config.tags } : {}),
    trace: {
      name: traceName,
      start_time: new Date(turn.startTime).toISOString(),
      end_time: new Date(turn.endTime).toISOString(),
      input:
        turn.userInput != null
          ? { role: "user", content: clipText(turn.userInput, maxChars) }
          : undefined,
      output:
        turn.finalOutput != null
          ? { role: "assistant", content: clipText(turn.finalOutput, maxChars) }
          : undefined,
      metadata: {
        "codex.turn_id": turn.turnId,
        "codex.thread_id": sessionMeta.sessionId,
        "codex.model": turn.model,
        "codex.model_provider": sessionMeta.modelProvider,
        "codex.cli_version": sessionMeta.cliVersion,
        "codex.aborted": turn.aborted,
        "codex.tool_call_count": turn.steps.reduce((n, s) => n + s.toolCalls.length, 0),
      },
    },
    generations,
    spans,
  };
}

export async function postLangstash(
  traceJson: Record<string, unknown>,
  config: Config,
): Promise<boolean> {
  const url = `${config.langstash_url.replace(/\/+$/, "")}/ingest`;
  const body = JSON.stringify(traceJson);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.langstash_timeout * 1000);

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      signal: controller.signal,
    });
    return resp.ok;
  } catch (e) {
    warn("langstash POST failed:", e);
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

export function appendFailedTrace(traceJson: Record<string, unknown>): void {
  try {
    mkdirSync(FAILED_DIR, { recursive: true });
    const today = new Date().toISOString().slice(0, 10);
    const line = JSON.stringify(traceJson) + "\n";
    appendFileSync(join(FAILED_DIR, `${today}.jsonl`), line, { flag: "a" });
  } catch (e) {
    error("appendFailedTrace error:", e);
  }
}

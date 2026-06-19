import type { Dirent } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";

import { context, trace, TraceFlags } from "@opentelemetry/api";
import type { SpanContext } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

import { deliverTrace } from "../../langstash-deliver/typescript/dist/index.js";

import type { Config } from "./config.js";
import { parseSession } from "./parse.js";
import { loadUploadedTurnIds, markTurnUploaded } from "./sidecar.js";
import type { ModelStep, RolloutLine, SessionMeta, TokenUsage, Turn } from "./types.js";
import { debugLog, error, info, warn, toText, truncate } from "./utils.js";

const SCOPE_NAME = "agent-exporter-to-langfuse";

async function loadSession(file: string): Promise<RolloutLine[]> {
  const data = await fs.readFile(file, "utf-8");
  const lines: RolloutLine[] = [];
  for (const raw of data.split("\n")) {
    const trimmed = raw.trim();
    if (!trimmed) continue;
    try {
      lines.push(JSON.parse(trimmed) as RolloutLine);
    } catch {
      // skip malformed lines
    }
  }
  return lines;
}

async function findSubagentRollout(
  parentFile: string,
  threadId: string,
): Promise<string | undefined> {
  const suffix = `-${threadId}.jsonl`;
  const root = path.resolve(path.dirname(parentFile), "../../..");

  async function walk(dir: string): Promise<string | undefined> {
    let entries: Dirent[];
    try {
      entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return undefined;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        const found = await walk(full);
        if (found) return found;
      } else if (entry.isFile() && entry.name.endsWith(suffix)) {
        return full;
      }
    }
    return undefined;
  }

  return walk(root);
}

function toUsageDetails(usage: TokenUsage | undefined): Record<string, number> | undefined {
  if (!usage) return undefined;
  const details: Record<string, number> = {};
  if (typeof usage.input_tokens === "number") details.input = usage.input_tokens;
  if (typeof usage.output_tokens === "number") details.output = usage.output_tokens;
  if (typeof usage.total_tokens === "number") details.total = usage.total_tokens;
  if (typeof usage.cached_input_tokens === "number") {
    details.cache_read_input_tokens = usage.cached_input_tokens;
  }
  if (typeof usage.reasoning_output_tokens === "number") {
    details.reasoning_tokens = usage.reasoning_output_tokens;
  }
  return Object.keys(details).length > 0 ? details : undefined;
}

type Clip = {
  (value: string): string;
  (value: unknown): unknown;
};

function makeClip(maxChars: number): Clip {
  function clip(value: string): string;
  function clip(value: unknown): unknown;
  function clip(value: unknown): unknown {
    if (typeof value !== "string") return value;
    const { text, meta } = truncate(value, maxChars);
    return meta ? `${text}\n…[truncated ${meta.originalLength - text.length} chars]` : text;
  }
  return clip;
}

function buildGenerationOutput(step: ModelStep, clip: Clip): Record<string, unknown> | undefined {
  const output: Record<string, unknown> = {};
  if (step.text) output.content = clip(step.text);
  if (step.reasoning) output.reasoning = clip(step.reasoning);
  if (step.toolCalls.length > 0) {
    output.tool_calls = step.toolCalls.map((tc) => ({
      id: tc.callId,
      name: tc.name,
      arguments: tc.args,
    }));
  }
  return Object.keys(output).length > 0 ? output : undefined;
}

/* ------------------------------------------------------------------ */
/*  OTLP JSON serialization helpers                                    */
/* ------------------------------------------------------------------ */

type OtlpAttribute = {
  key: string;
  value:
    | { stringValue: string }
    | { intValue: string }
    | { boolValue: boolean }
    | { doubleValue: number };
};

type OtlpSpan = {
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  name: string;
  startTimeUnixNano: string;
  endTimeUnixNano: string;
  attributes: OtlpAttribute[];
};

function readableSpanToOtlp(span: ReadableSpan): OtlpSpan {
  const ctx = span.spanContext();
  const attrs: OtlpAttribute[] = [];
  for (const [key, value] of Object.entries(span.attributes)) {
    if (value === undefined || value === null) continue;
    if (typeof value === "string") {
      attrs.push({ key, value: { stringValue: value } });
    } else if (typeof value === "number") {
      if (Number.isInteger(value)) {
        attrs.push({ key, value: { intValue: String(value) } });
      } else {
        attrs.push({ key, value: { doubleValue: value } });
      }
    } else if (typeof value === "boolean") {
      attrs.push({ key, value: { boolValue: value } });
    }
  }

  const parentId = span.parentSpanContext?.spanId;
  return {
    traceId: ctx.traceId,
    spanId: ctx.spanId,
    ...(parentId ? { parentSpanId: parentId } : {}),
    name: span.name,
    startTimeUnixNano: hrTimeToNanos(span.startTime),
    endTimeUnixNano: hrTimeToNanos(span.endTime),
    attributes: attrs,
  };
}

function hrTimeToNanos(hrTime: [number, number]): string {
  return String(BigInt(hrTime[0]) * 1_000_000_000n + BigInt(hrTime[1]));
}

/* ------------------------------------------------------------------ */
/*  Parent span context for subagent nesting                           */
/* ------------------------------------------------------------------ */

export type ParentSpanContext = {
  traceId: string;
  spanId: string;
};

/* ------------------------------------------------------------------ */
/*  buildOtlpJson                                                      */
/* ------------------------------------------------------------------ */

export function buildOtlpJson(
  turn: Turn,
  sessionMeta: SessionMeta,
  config: Config,
  traceName: string,
  parentContext?: ParentSpanContext,
): Record<string, unknown> {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const tracer = provider.getTracer(SCOPE_NAME);
  const clip = makeClip(config.max_chars);

  // If we have a parent context, create a synthetic parent span context
  // so the root span inherits the parent's traceId.
  let rootCtx = context.active();
  if (parentContext) {
    const parentSpanContext: SpanContext = {
      traceId: parentContext.traceId,
      spanId: parentContext.spanId,
      traceFlags: TraceFlags.SAMPLED,
    };
    rootCtx = trace.setSpanContext(rootCtx, parentSpanContext);
  }

  // --- Root span ---
  const rootSpan = tracer.startSpan(
    traceName,
    { startTime: new Date(turn.startTime) },
    rootCtx,
  );

  // Set root attributes
  rootSpan.setAttribute("langfuse.trace.name", traceName);
  rootSpan.setAttribute("session.id", sessionMeta.sessionId);
  if (config.user_id) {
    rootSpan.setAttribute("user.id", config.user_id);
  }
  if (config.tags && config.tags.length > 0) {
    rootSpan.setAttribute("langfuse.trace.tags", JSON.stringify(config.tags));
  }
  if (turn.userInput != null) {
    rootSpan.setAttribute(
      "langfuse.observation.input",
      JSON.stringify({ role: "user", content: clip(turn.userInput) }),
    );
  }
  if (turn.finalOutput != null) {
    rootSpan.setAttribute(
      "langfuse.observation.output",
      JSON.stringify({ role: "assistant", content: clip(turn.finalOutput) }),
    );
  }
  rootSpan.end(new Date(turn.endTime));

  // Create a context with the root span as parent for generation spans
  const rootParentCtx = trace.setSpan(context.active(), rootSpan);

  // --- Generation and tool spans ---
  let previousToolResults: unknown = undefined;

  for (let i = 0; i < turn.steps.length; i++) {
    const step = turn.steps[i];

    const genSpan = tracer.startSpan(
      turn.model ?? "codex.generation",
      { startTime: new Date(step.startTime) },
      rootParentCtx,
    );

    genSpan.setAttribute("langfuse.observation.type", "generation");
    if (turn.model) {
      genSpan.setAttribute("langfuse.observation.model.name", turn.model);
    }

    const usageDetails = toUsageDetails(step.usage);
    if (usageDetails) {
      genSpan.setAttribute("langfuse.observation.usage_details", JSON.stringify(usageDetails));
    }

    const genInput =
      i === 0
        ? turn.userInput != null
          ? clip(turn.userInput)
          : undefined
        : previousToolResults;

    if (genInput != null) {
      genSpan.setAttribute("langfuse.observation.input", JSON.stringify(genInput));
    }

    const genOutput = buildGenerationOutput(step, clip);
    if (genOutput) {
      genSpan.setAttribute("langfuse.observation.output", JSON.stringify(genOutput));
    }

    genSpan.end(new Date(step.endTime));

    // Create a context with the generation span as parent for tool spans
    const genParentCtx = trace.setSpan(context.active(), genSpan);

    // --- Tool call spans under this generation ---
    for (const tc of step.toolCalls) {
      const toolSpan = tracer.startSpan(
        tc.name || "tool",
        { startTime: new Date(tc.startTime) },
        genParentCtx,
      );

      toolSpan.setAttribute("langfuse.observation.type", "tool");
      if (tc.args != null) {
        toolSpan.setAttribute("langfuse.observation.input", JSON.stringify(tc.args));
      }
      if (tc.output != null) {
        toolSpan.setAttribute("langfuse.observation.output", JSON.stringify(clip(toText(tc.output))));
      }

      toolSpan.end(new Date(tc.endTime ?? step.endTime));
    }

    previousToolResults =
      step.toolCalls.length > 0
        ? step.toolCalls.map((tc) => ({
            name: tc.name,
            output: tc.output != null ? clip(toText(tc.output)) : undefined,
            ...(tc.error ? { error: clip(tc.error) } : {}),
          }))
        : undefined;
  }

  // Force flush and collect spans
  provider.forceFlush();

  const finishedSpans = exporter.getFinishedSpans();

  // Serialize to OTLP JSON
  const otlpSpans: OtlpSpan[] = finishedSpans.map(readableSpanToOtlp);

  provider.shutdown();

  return {
    resourceSpans: [
      {
        scopeSpans: [
          {
            scope: { name: SCOPE_NAME },
            spans: otlpSpans,
          },
        ],
      },
    ],
  };
}

/* ------------------------------------------------------------------ */
/*  convertRollout — entry point                                       */
/* ------------------------------------------------------------------ */

export async function convertRollout(
  rolloutFile: string,
  options: { config: Config; parentContext?: ParentSpanContext },
): Promise<void> {
  const { sessionMeta, turns } = parseSession(await loadSession(rolloutFile));
  info(`parsed ${turns.length} turn(s) from ${path.basename(rolloutFile)}`);

  if (options.parentContext) {
    // Subagent: emit all turns under the parent span, do not deduplicate
    for (let i = 0; i < turns.length; i++) {
      const turn = turns[i];
      const traceName = `Codex - Subagent Turn ${i + 1}`;
      const otlpJson = buildOtlpJson(turn, sessionMeta, options.config, traceName, options.parentContext);

      // Recurse into sub-subagents
      for (const threadId of turn.subagentThreadIds) {
        const subFile = await findSubagentRollout(rolloutFile, threadId);
        if (!subFile) {
          warn(`subagent rollout not found for thread ${threadId}`);
          continue;
        }
        // For nested subagents, use the root span of this turn as parent.
        // Extract root spanId from the built OTLP (first span is root).
        const spans = (
          (otlpJson.resourceSpans as Array<{ scopeSpans: Array<{ spans: OtlpSpan[] }> }>)[0]
            .scopeSpans[0].spans
        );
        const rootSpan = spans.find((s) => s.parentSpanId === options.parentContext!.spanId) ?? spans[0];
        await convertRollout(subFile, {
          config: options.config,
          parentContext: { traceId: rootSpan.traceId, spanId: rootSpan.spanId },
        });
      }

      const delivered = await deliverTrace(otlpJson);
      if (delivered) {
        info(`delivered subagent turn ${i + 1} via langstash-deliver`);
      } else {
        warn(`failed to deliver subagent turn ${i + 1}`);
      }
    }
    return;
  }

  // Top-level: deduplicate by turn id
  const uploaded = await loadUploadedTurnIds(rolloutFile);

  for (let i = 0; i < turns.length; i++) {
    const turn = turns[i];
    const effectiveId = turn.turnId ?? `idx-${i}`;

    if (uploaded.has(effectiveId)) {
      debugLog(`skipping already-uploaded turn ${effectiveId}`);
      continue;
    }

    const traceName = `Codex - Turn ${i + 1}`;
    const otlpJson = buildOtlpJson(turn, sessionMeta, options.config, traceName);

    // Handle subagent recursion: use this turn's root span as parent
    for (const threadId of turn.subagentThreadIds) {
      const subFile = await findSubagentRollout(rolloutFile, threadId);
      if (!subFile) {
        warn(`subagent rollout not found for thread ${threadId}`);
        continue;
      }
      const spans = (
        (otlpJson.resourceSpans as Array<{ scopeSpans: Array<{ spans: OtlpSpan[] }> }>)[0]
          .scopeSpans[0].spans
      );
      // Root span is the one without parentSpanId (top-level turn)
      const rootSpan = spans.find((s) => !s.parentSpanId) ?? spans[0];
      await convertRollout(subFile, {
        config: options.config,
        parentContext: { traceId: rootSpan.traceId, spanId: rootSpan.spanId },
      });
    }

    const delivered = await deliverTrace(otlpJson);
    if (delivered) {
      info(`delivered turn ${effectiveId} via langstash-deliver`);
    } else {
      warn(`failed to deliver turn ${effectiveId}`);
    }

    uploaded.add(effectiveId);
    await markTurnUploaded(rolloutFile, effectiveId);
  }
}

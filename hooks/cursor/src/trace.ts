import { context, trace } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

import { deliverTrace as defaultDeliverTrace } from "../../langstash-deliver/typescript/dist/index.js";

import type { Config } from "./config.js";
import type { StateRecord, Turn } from "./types.js";
import { toText, truncate } from "./utils.js";

const SCOPE_NAME = "agent-exporter-to-langfuse";

/* ------------------------------------------------------------------ */
/*  Truncation helper                                                  */
/* ------------------------------------------------------------------ */

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
/*  Tool span data extraction                                          */
/* ------------------------------------------------------------------ */

interface ToolSpanData {
  name: string;
  input?: unknown;
  output?: unknown;
  startTime: string;
  endTime: string;
  metadata: Record<string, string>;
}

function str(val: unknown): string {
  return typeof val === "string" ? val : "";
}

function buildToolSpans(turn: Turn, clip: Clip): ToolSpanData[] {
  const events = turn.events;
  const spans: ToolSpanData[] = [];

  // --- Shell: pair beforeShellExecution + afterShellExecution ---
  const shellBe = events.filter((r) => r.hook_event_name === "beforeShellExecution");
  const shellAf = events.filter((r) => r.hook_event_name === "afterShellExecution");
  const shellCount = Math.max(shellBe.length, shellAf.length);
  for (let i = 0; i < shellCount; i++) {
    const before = shellBe[i];
    const after = shellAf[i];
    const startTs = before?.timestamp ?? after!.timestamp;
    const endTs = after?.timestamp ?? before!.timestamp;
    spans.push({
      name: str(before?.command) || str(after?.command) || "shell",
      input: before ? { command: before.command, cwd: before.cwd } : undefined,
      output: after
        ? { output: clip(str(after.output)), duration: after.duration }
        : undefined,
      startTime: startTs,
      endTime: endTs,
      metadata: { tool_type: "shell" },
    });
  }

  // --- MCP: pair beforeMCPExecution + afterMCPExecution ---
  const mcpBe = events.filter((r) => r.hook_event_name === "beforeMCPExecution");
  const mcpAf = events.filter((r) => r.hook_event_name === "afterMCPExecution");
  const mcpCount = Math.max(mcpBe.length, mcpAf.length);
  for (let i = 0; i < mcpCount; i++) {
    const before = mcpBe[i];
    const after = mcpAf[i];
    const startTs = before?.timestamp ?? after!.timestamp;
    const endTs = after?.timestamp ?? before!.timestamp;
    spans.push({
      name: str(before?.tool_name) || str(after?.tool_name) || "mcp",
      input: before
        ? { tool_name: before.tool_name, tool_input: before.tool_input, url: before.url, command: before.command }
        : undefined,
      output: after
        ? { result_json: clip(str(after.result_json)), duration: after.duration }
        : undefined,
      startTime: startTs,
      endTime: endTs,
      metadata: { tool_type: "mcp" },
    });
  }

  // --- File read: beforeReadFile (single) ---
  for (const r of events.filter((r) => r.hook_event_name === "beforeReadFile")) {
    spans.push({
      name: "file_read",
      input: { file_path: r.file_path },
      output: { content: clip(str(r.content)) },
      startTime: r.timestamp,
      endTime: r.timestamp,
      metadata: { tool_type: "file" },
    });
  }

  // --- File edit: afterFileEdit (single) ---
  for (const r of events.filter((r) => r.hook_event_name === "afterFileEdit")) {
    spans.push({
      name: "file_edit",
      input: { file_path: r.file_path },
      output: { edits: r.edits },
      startTime: r.timestamp,
      endTime: r.timestamp,
      metadata: { tool_type: "file" },
    });
  }

  // --- Thinking: afterAgentThought (single) ---
  for (const r of events.filter((r) => r.hook_event_name === "afterAgentThought")) {
    spans.push({
      name: "thinking",
      output: { text: clip(str(r.text)), duration_ms: r.duration_ms },
      startTime: r.timestamp,
      endTime: r.timestamp,
      metadata: { tool_type: "thinking" },
    });
  }

  return spans;
}

/* ------------------------------------------------------------------ */
/*  buildOtlpJson                                                      */
/* ------------------------------------------------------------------ */

export function buildOtlpJson(
  turn: Turn,
  config: Config,
  traceName: string,
): Record<string, unknown> {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const tracer = provider.getTracer(SCOPE_NAME);
  const clip = makeClip(config.max_chars);

  // --- Root span ---
  const rootSpan = tracer.startSpan(
    traceName,
    { startTime: new Date(turn.startTime) },
  );

  rootSpan.setAttribute("langfuse.trace.name", traceName);
  rootSpan.setAttribute("session.id", turn.conversationId);
  const userId = turn.userEmail || config.user_id;
  if (userId) {
    rootSpan.setAttribute("user.id", userId);
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
  if (turn.cursorStatus) {
    rootSpan.setAttribute("langfuse.observation.metadata.cursor_status", turn.cursorStatus);
  }
  rootSpan.end(new Date(turn.endTime));

  const rootParentCtx = trace.setSpan(context.active(), rootSpan);

  // --- Generation span ---
  const genSpan = tracer.startSpan(
    turn.model ?? "cursor.generation",
    { startTime: new Date(turn.startTime) },
    rootParentCtx,
  );

  genSpan.setAttribute("langfuse.observation.type", "generation");
  if (turn.model) {
    genSpan.setAttribute("langfuse.observation.model.name", turn.model);
  }
  if (turn.userInput != null) {
    genSpan.setAttribute(
      "langfuse.observation.input",
      JSON.stringify({ role: "user", content: clip(turn.userInput) }),
    );
  }
  if (turn.finalOutput != null) {
    genSpan.setAttribute(
      "langfuse.observation.output",
      JSON.stringify({ role: "assistant", content: clip(turn.finalOutput) }),
    );
  }
  genSpan.end(new Date(turn.endTime));

  const genParentCtx = trace.setSpan(context.active(), genSpan);

  // --- Tool spans ---
  const toolSpansData = buildToolSpans(turn, clip);
  for (const ts of toolSpansData) {
    const span = tracer.startSpan(
      ts.name,
      { startTime: new Date(ts.startTime) },
      genParentCtx,
    );

    span.setAttribute("langfuse.observation.type", "tool");
    if (ts.input != null) {
      span.setAttribute("langfuse.observation.input", JSON.stringify(ts.input));
    }
    if (ts.output != null) {
      span.setAttribute("langfuse.observation.output", JSON.stringify(clip(toText(ts.output))));
    }
    for (const [k, v] of Object.entries(ts.metadata)) {
      span.setAttribute(`langfuse.observation.metadata.${k}`, v);
    }

    span.end(new Date(ts.endTime));
  }

  // Force flush and collect spans
  provider.forceFlush();
  const finishedSpans = exporter.getFinishedSpans();
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
/*  deliverTurn — build + deliver one turn                             */
/* ------------------------------------------------------------------ */

export type DeliverFn = (otlpJson: Record<string, unknown>) => Promise<boolean>;

/**
 * Build OTLP JSON for a turn and deliver it via langstash-deliver.
 * Returns true if delivery succeeded, false otherwise.
 * Delivery failure does NOT block — the OTLP is persisted in data/failed/
 * by langstash-deliver's Tier 3.
 */
export async function deliverTurn(
  turn: Turn,
  config: Config,
  traceName: string,
  deliverFn: DeliverFn = defaultDeliverTrace,
): Promise<boolean> {
  const otlpJson = buildOtlpJson(turn, config, traceName);
  return deliverFn(otlpJson);
}

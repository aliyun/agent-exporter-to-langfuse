/**
 * Build one OTLP JSON payload (resourceSpans -> scopeSpans -> spans) from a
 * run's span record tree. The output must satisfy the exporter's
 * validate_otlp() contract: exactly one root span without parentSpanId,
 * 32-hex traceId, 16-hex spanIds, nanosecond string timestamps with
 * end >= start, and attributes as a KeyValue array.
 */
import type { RunState, SpanRecord } from "./state.ts";

const SCOPE_NAME = "agent-exporter-to-langfuse";

type OtlpAttribute = {
  key: string;
  value:
    | { stringValue: string }
    | { intValue: string }
    | { boolValue: boolean }
    | { doubleValue: number };
};

interface OtlpSpan {
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  name: string;
  startTimeUnixNano: string;
  endTimeUnixNano: string;
  attributes: OtlpAttribute[];
}

function msToNanos(ms: number): string {
  return String(BigInt(Math.round(ms)) * 1_000_000n);
}

function stringAttr(key: string, value: string): OtlpAttribute {
  return { key, value: { stringValue: value } };
}

function jsonAttr(key: string, value: unknown): OtlpAttribute {
  return stringAttr(key, typeof value === "string" ? value : JSON.stringify(value ?? null));
}

export interface TraceContext {
  sessionId?: string;
  userId: string;
  tags: string[];
  traceMetadata?: Record<string, unknown>;
}

function spanAttributes(record: SpanRecord, trace: TraceContext): OtlpAttribute[] {
  const attrs: OtlpAttribute[] = [];
  const isRoot = record.parentSpanId === undefined;

  attrs.push(stringAttr("langfuse.observation.type", record.type === "agent" ? "agent" : record.type));

  if (record.input !== undefined) {
    attrs.push(jsonAttr("langfuse.observation.input", record.input));
  }
  if (record.output !== undefined) {
    attrs.push(jsonAttr("langfuse.observation.output", record.output));
  }
  if (record.model) {
    attrs.push(stringAttr("langfuse.observation.model.name", record.model));
  }
  if (record.modelParameters) {
    attrs.push(jsonAttr("langfuse.observation.model_parameters", record.modelParameters));
  }
  if (record.usageDetails) {
    attrs.push(jsonAttr("langfuse.observation.usage_details", record.usageDetails));
  }
  if (record.costDetails) {
    attrs.push(jsonAttr("langfuse.observation.cost_details", record.costDetails));
  }
  if (record.completionStartTimeMs !== undefined) {
    attrs.push(
      jsonAttr(
        "langfuse.observation.completion_start_time",
        new Date(record.completionStartTimeMs).toISOString(),
      ),
    );
  }
  if (record.level && record.level !== "DEFAULT") {
    attrs.push(stringAttr("langfuse.observation.level", record.level));
  }
  if (record.statusMessage) {
    attrs.push(stringAttr("langfuse.observation.status_message", record.statusMessage));
  }
  if (record.metadata && Object.keys(record.metadata).length > 0) {
    attrs.push(jsonAttr("langfuse.observation.metadata", record.metadata));
  }

  if (isRoot) {
    attrs.push(stringAttr("langfuse.trace.name", "pi-agent"));
    if (trace.sessionId) {
      attrs.push(stringAttr("session.id", trace.sessionId));
    }
    attrs.push(stringAttr("user.id", trace.userId));
    attrs.push(jsonAttr("langfuse.trace.tags", trace.tags));
    if (trace.traceMetadata && Object.keys(trace.traceMetadata).length > 0) {
      attrs.push(jsonAttr("langfuse.trace.metadata", trace.traceMetadata));
    }
    if (record.input !== undefined) {
      attrs.push(jsonAttr("langfuse.trace.input", record.input));
    }
    if (record.output !== undefined) {
      attrs.push(jsonAttr("langfuse.trace.output", record.output));
    }
  }

  return attrs;
}

export function buildOtlpJson(run: RunState, trace: TraceContext): Record<string, unknown> {
  const fallbackEnd = Date.now();
  const spans: OtlpSpan[] = run.spans.map((record) => {
    const endMs = Math.max(record.endTimeMs ?? fallbackEnd, record.startTimeMs);
    return {
      traceId: run.traceId,
      spanId: record.spanId,
      ...(record.parentSpanId ? { parentSpanId: record.parentSpanId } : {}),
      name: record.name || "span",
      startTimeUnixNano: msToNanos(record.startTimeMs),
      endTimeUnixNano: msToNanos(endMs),
      attributes: spanAttributes(record, trace),
    };
  });

  return {
    resourceSpans: [
      {
        scopeSpans: [
          {
            scope: { name: SCOPE_NAME },
            spans,
          },
        ],
      },
    ],
  };
}

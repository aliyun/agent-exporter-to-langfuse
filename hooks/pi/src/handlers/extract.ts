/**
 * Tolerant event-payload extraction helpers, ported from pi-langfuse
 * src/utils.ts. Pi/provider event shapes vary, so extraction favors
 * fallbacks over strict schemas.
 */
import { getLimits } from "../limits.ts";
import { shapePayload, truncate } from "../shape.ts";

export function extractTextContent(content: unknown, maxLength?: number): string | undefined {
  if (typeof content === "string") {
    return maxLength ? truncate(content, maxLength) : content;
  }

  if (!Array.isArray(content)) {
    return undefined;
  }

  const text = content
    .map((item) => {
      if (!item || typeof item !== "object") return "";
      const block = item as { type?: string; text?: string };
      return block.type === "text" && block.text ? block.text : "";
    })
    .filter(Boolean)
    .join("\n");

  if (!text) {
    return undefined;
  }

  return maxLength ? truncate(text, maxLength) : text;
}

export function extractToolCalls(message: Record<string, unknown>): unknown | undefined {
  return (
    message.toolCalls ??
    message.tool_calls ??
    message.function_calls ??
    (message.content && Array.isArray(message.content)
      ? (message.content as unknown[]).filter((block) => {
          return block && typeof block === "object" && ["tool_use", "tool_call", "toolCall"].includes(String((block as { type?: string }).type));
        })
      : undefined)
  );
}

export function extractAssistantOutput(message: unknown): unknown | undefined {
  if (!message || typeof message !== "object") {
    return undefined;
  }

  const msg = message as Record<string, unknown>;
  const text = extractTextContent(msg.content);
  if (text) {
    return text;
  }

  const toolCalls = extractToolCalls(msg);
  if (toolCalls) {
    return { toolCalls: shapePayload(toolCalls) };
  }

  return shapePayload(msg);
}

export function extractFinalAssistant(messages: unknown): Record<string, unknown> | undefined {
  if (!Array.isArray(messages)) {
    return undefined;
  }
  return messages.filter((message) => message?.role === "assistant").pop() as
    | Record<string, unknown>
    | undefined;
}

export function getRequestKey(event: Record<string, unknown>, fallback: string): string {
  return String(
    event.requestId ??
      event.providerRequestId ??
      event.messageId ??
      event.turnId ??
      event.turnIndex ??
      event.id ??
      fallback,
  );
}

export function getToolCallId(event: Record<string, unknown>): string | undefined {
  const id = event.toolCallId ?? event.id ?? event.callId ?? event.tool_use_id ?? event.toolUseId;
  return id === undefined || id === null ? undefined : String(id);
}

export function getToolName(event: Record<string, unknown>): string {
  return String(
    event.toolName ??
      event.name ??
      event.tool ??
      event.functionName ??
      (event.call && typeof event.call === "object" ? (event.call as Record<string, unknown>).name : undefined) ??
      "tool",
  );
}

export function getToolInput(event: Record<string, unknown>): unknown {
  return (
    event.input ??
    event.args ??
    event.arguments ??
    event.params ??
    (event.call && typeof event.call === "object" ? (event.call as Record<string, unknown>).input : undefined) ??
    event
  );
}

export function getProviderPayload(event: Record<string, unknown>): unknown {
  return event.request ?? event.payload ?? event.body ?? event.providerPayload ?? event.messages ?? event;
}

export function extractModelParameters(payload: unknown): Record<string, string | number> | undefined {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return undefined;
  }

  const params: Record<string, string | number> = {};
  const record = payload as Record<string, unknown>;
  for (const key of [
    "temperature",
    "top_p",
    "topP",
    "max_tokens",
    "maxTokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "reasoning_effort",
  ]) {
    const value = record[key];
    if (typeof value === "string" || typeof value === "number") {
      params[key] = value;
    }
  }

  return Object.keys(params).length > 0 ? params : undefined;
}

export function getMessageFromEvent(event: Record<string, unknown>): Record<string, unknown> | undefined {
  if (event.message && typeof event.message === "object") {
    return event.message as Record<string, unknown>;
  }
  if (event.role || event.content) {
    return event;
  }
  return undefined;
}

/** Usage normalized to the exporter's usage_details keys. */
export function extractUsage(messageOrEvent: Record<string, unknown>): Record<string, number> | undefined {
  const usage = (messageOrEvent.usage ??
    (messageOrEvent.message && typeof messageOrEvent.message === "object"
      ? (messageOrEvent.message as Record<string, unknown>).usage
      : undefined)) as Record<string, unknown> | undefined;
  if (!usage || typeof usage !== "object") {
    return undefined;
  }

  const input = Number(usage.input ?? usage.inputTokens ?? usage.prompt_tokens ?? usage.promptTokens ?? 0);
  const output = Number(usage.output ?? usage.outputTokens ?? usage.completion_tokens ?? usage.completionTokens ?? 0);
  const cacheRead = Number(usage.cacheRead ?? usage.cache_read ?? usage.cachedTokens ?? 0);
  const cacheWrite = Number(usage.cacheWrite ?? usage.cache_write ?? 0);

  return {
    input,
    output,
    cache_read_input_tokens: cacheRead,
    cache_creation_input_tokens: cacheWrite,
  };
}

export function extractCostDetails(messageOrEvent: Record<string, unknown>): Record<string, number> | undefined {
  const usage = (messageOrEvent.usage ??
    (messageOrEvent.message && typeof messageOrEvent.message === "object"
      ? (messageOrEvent.message as Record<string, unknown>).usage
      : undefined)) as Record<string, unknown> | undefined;
  const cost = (messageOrEvent.cost ?? usage?.cost ?? messageOrEvent.costDetails) as Record<string, unknown> | undefined;
  if (!cost || typeof cost !== "object") {
    return undefined;
  }

  const input = Number(cost.input ?? cost.inputCost ?? 0);
  const output = Number(cost.output ?? cost.outputCost ?? 0);
  const total = Number(cost.total ?? cost.totalCost ?? input + output);
  if (input === 0 && output === 0 && total === 0) {
    return undefined;
  }

  return { input, output, total };
}

export function extractResponseMetadata(event: Record<string, unknown>): Record<string, unknown> {
  return shapePayload(
    {
      status: event.status ?? event.statusCode ?? event.httpStatus,
      headers: event.headers,
      responseHeaders: event.responseHeaders,
      providerMetadata: event.providerMetadata ?? event.metadata,
      requestId: event.requestId ?? event.providerRequestId,
    },
    { depth: 4, maxString: 4_000 },
  ) as Record<string, unknown>;
}

export function toolPayloadLimit(): number {
  return getLimits().maxToolPayload;
}

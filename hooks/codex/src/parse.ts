import type {
  EventMsgPayload,
  MessageContentPart,
  ModelStep,
  ResponseItemFunctionCall,
  ResponseItemFunctionCallOutput,
  ResponseItemCustomToolCall,
  ResponseItemMessage,
  RolloutLine,
  SessionMeta,
  TokenUsage,
  ToolCall,
  Turn,
} from "./types.js";
import { isPrimitive, toText } from "./utils.js";

function extractMessageText(content: MessageContentPart[] | undefined): string {
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (!part || typeof part !== "object") return "";
      if (part.type === "input_text" || part.type === "output_text" || part.type === "text") {
        return typeof part.text === "string" ? part.text : "";
      }
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function extractReasoning(item: {
  content?: unknown[] | string | null;
  summary?: unknown[];
}): string {
  if (typeof item.content === "string") return item.content;
  if (Array.isArray(item.content)) {
    return item.content
      .map((c) =>
        c && typeof c === "object" && "text" in c
          ? toText((c as { text: unknown }).text)
          : toText(c),
      )
      .filter(Boolean)
      .join("\n");
  }
  if (Array.isArray(item.summary) && item.summary.length > 0) {
    return item.summary
      .map((s) => toText(s))
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

function parseArgs(raw: string): unknown {
  if (typeof raw !== "string") return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function extractToolError(payload: EventMsgPayload): string | undefined {
  const explicit = payload.error ?? payload.codex_error_info;
  if (explicit != null) {
    return isPrimitive(explicit) ? String(explicit) : JSON.stringify(explicit);
  }
  const streams = [payload.stdout, payload.stderr]
    .filter((s): s is string => typeof s === "string" && s.length > 0)
    .join("\n");
  if (typeof payload.aggregated_output === "string" && payload.aggregated_output) {
    return payload.aggregated_output;
  }
  if (streams) return streams;
  if (typeof payload.exit_code === "number") return `Exit code: ${payload.exit_code}`;
  return undefined;
}

type MutableTurn = Turn & { lastAgentMessage?: string; userInputFallback?: string };

function newTurn(startTime: number): MutableTurn {
  return {
    turnId: undefined,
    startTime,
    endTime: startTime,
    steps: [],
    subagentThreadIds: [],
    completed: false,
    aborted: false,
  };
}

export function parseSession(lines: RolloutLine[]): {
  sessionMeta: SessionMeta;
  turns: Turn[];
} {
  let sessionMeta: SessionMeta = { sessionId: "unknown" };
  const turns: Turn[] = [];

  let turn: MutableTurn | null = null;
  let step: ModelStep | null = null;
  let toolCallsById = new Map<string, ToolCall>();
  let lastTimestamp = Date.now();

  function newStep(startTime: number): ModelStep {
    return { startTime, endTime: startTime, toolCalls: [] };
  }

  const ensureTurn = (ts: number): MutableTurn => (turn ??= newTurn(ts));
  const ensureStep = (ts: number) => (step ??= newStep(ts));

  const closeStep = (ts: number, usage?: TokenUsage) => {
    if (!step) return;
    step.endTime = Math.max(step.endTime, ts);
    if (usage) step.usage = usage;
    turn!.steps.push(step);
    step = null;
  };

  const finishTurn = (ts: number, opts: { completed: boolean; aborted: boolean }) => {
    if (!turn) return;
    closeStep(ts);
    turn.endTime = Math.max(turn.endTime, ts);
    turn.completed = opts.completed;
    turn.aborted = opts.aborted;
    turn.userInput = turn.userInput ?? turn.userInputFallback;
    turn.finalOutput = turn.lastAgentMessage ?? turn.steps.filter((s) => s.text).at(-1)?.text;
    delete turn.lastAgentMessage;
    delete turn.userInputFallback;
    turns.push(turn);
    turn = null;
    toolCallsById = new Map();
  };

  for (const line of lines) {
    const ts = Number.isFinite(Date.parse(line.timestamp))
      ? Date.parse(line.timestamp)
      : lastTimestamp;
    lastTimestamp = ts;

    if (line.type === "session_meta") {
      const p = line.payload as RolloutLine["payload"] & {
        id?: string;
        cli_version?: string;
        model_provider?: string | null;
        base_instructions?: { text?: string } | null;
      };
      sessionMeta = {
        sessionId: typeof p.id === "string" ? p.id : sessionMeta.sessionId,
        cliVersion: p.cli_version,
        modelProvider: p.model_provider ?? undefined,
        baseInstructions: p.base_instructions?.text,
      };
      continue;
    }

    if (line.type === "turn_context") {
      const t = ensureTurn(ts);
      const p = line.payload as { model?: string };
      t.model = p.model ?? t.model;
      t.invocationParams = line.payload as Record<string, unknown>;
      continue;
    }

    if (line.type === "response_item") {
      const p = line.payload as { type?: string } & Record<string, unknown>;
      ensureTurn(ts);

      if (p.type === "message") {
        const msg = p as unknown as ResponseItemMessage;
        const text = extractMessageText(msg.content as MessageContentPart[]);
        if (msg.role === "assistant") {
          const s = ensureStep(ts);
          if (text) s.text = s.text ? `${s.text}\n${text}` : text;
        } else if (msg.role === "user" && text) {
          if (
            !turn!.userInputFallback &&
            !/^<(environment_context|user_instructions)/.test(text.trim())
          ) {
            turn!.userInputFallback = text;
          }
        }
      } else if (p.type === "function_call") {
        const call = p as unknown as ResponseItemFunctionCall;
        const s = ensureStep(ts);
        const tc: ToolCall = {
          callId: call.call_id,
          name: call.name,
          args: parseArgs(call.arguments),
          startTime: ts,
        };
        s.toolCalls.push(tc);
        toolCallsById.set(tc.callId, tc);
      } else if (p.type === "custom_tool_call") {
        const call = p as unknown as ResponseItemCustomToolCall;
        const s = ensureStep(ts);
        const tc: ToolCall = {
          callId: call.call_id,
          name: call.name,
          args: parseArgs(call.input),
          startTime: ts,
        };
        s.toolCalls.push(tc);
        toolCallsById.set(tc.callId, tc);
      } else if (p.type === "function_call_output" || p.type === "custom_tool_call_output") {
        const out = p as unknown as ResponseItemFunctionCallOutput;
        const tc = toolCallsById.get(out.call_id);
        if (tc) {
          if (tc.output == null) tc.output = out.output;
          tc.endTime = Math.max(tc.endTime ?? ts, ts);
        }
      } else if (p.type === "reasoning") {
        const reasoning = extractReasoning(
          p as { content?: unknown[] | string | null; summary?: unknown[] },
        );
        if (reasoning) {
          const s = ensureStep(ts);
          s.reasoning = s.reasoning ? `${s.reasoning}\n${reasoning}` : reasoning;
        }
      }
      continue;
    }

    if (line.type === "event_msg") {
      const p = line.payload as EventMsgPayload;
      const et = p.type;

      if (et === "task_started") {
        if (turn) finishTurn(ts, { completed: false, aborted: false });
        turn = newTurn(ts);
        turn.turnId = typeof p.turn_id === "string" ? p.turn_id : undefined;
        continue;
      }

      ensureTurn(ts);

      if (et === "user_message" && typeof p.message === "string") {
        if (!turn!.userInput) turn!.userInput = p.message;
      } else if (et === "agent_message" && typeof p.message === "string") {
        turn!.lastAgentMessage = p.message;
      } else if (et === "token_count") {
        if (p.info?.total_token_usage) turn!.totalUsage = p.info.total_token_usage;
        closeStep(ts, p.info?.last_token_usage ?? undefined);
      } else if (et === "task_complete") {
        finishTurn(ts, { completed: true, aborted: false });
      } else if (et === "turn_aborted") {
        finishTurn(ts, { completed: true, aborted: true });
      } else {
        if (et === "collab_agent_spawn_end" && typeof p.new_thread_id === "string") {
          turn!.subagentThreadIds.push(p.new_thread_id);
        }
        if (typeof p.call_id === "string" && et.endsWith("_end")) {
          const tc = toolCallsById.get(p.call_id);
          if (tc) {
            tc.endTime = Math.max(tc.endTime ?? ts, ts);
            if (p.status === "failed" || p.status === "declined") {
              tc.error = extractToolError(p);
            }
            if (tc.output == null) {
              tc.output = p.aggregated_output ?? p.stdout ?? (p as { result?: unknown }).result;
            }
          }
        }
      }
      continue;
    }
  }

  if (turn) finishTurn(lastTimestamp, { completed: false, aborted: false });

  return { sessionMeta, turns };
}

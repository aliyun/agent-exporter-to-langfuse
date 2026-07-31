/**
 * Session-scoped mutable runtime state, ported from pi-langfuse src/state.ts.
 * Instead of live Langfuse SDK observations, each agent run accumulates an
 * in-memory span record tree that is built into one OTLP JSON payload at
 * agent_end. All records are JSON-serializable so the run can be
 * checkpointed and rebuilt after a crash.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";

export type ObservationType = "agent" | "span" | "generation" | "tool";
export type ObservationLevel = "DEFAULT" | "WARNING" | "ERROR";

export interface SpanRecord {
  spanId: string;
  parentSpanId?: string;
  name: string;
  type: ObservationType;
  startTimeMs: number;
  endTimeMs?: number;
  input?: unknown;
  output?: unknown;
  model?: string;
  modelParameters?: Record<string, string | number>;
  usageDetails?: Record<string, number>;
  costDetails?: Record<string, number>;
  completionStartTimeMs?: number;
  level?: ObservationLevel;
  statusMessage?: string;
  metadata?: Record<string, unknown>;
}

export interface GenerationTrack {
  record: SpanRecord;
  requestKey: string;
  ended: boolean;
  ttftRecorded?: boolean;
}

export interface ToolTrack {
  record: SpanRecord;
  toolName: string;
  ended: boolean;
  startedAt: number;
}

export interface RunState {
  traceId: string;
  root: SpanRecord;
  spans: SpanRecord[];
  activeTurn?: SpanRecord;
  activeGenerations: Map<string, GenerationTrack>;
  generationOrder: string[];
  activeTools: Map<string, ToolTrack>;
  generationSeq: number;
  promptInput?: unknown;
  latestAssistantOutput?: unknown;
  cwd: string;
  emitted: boolean;
}

export interface SessionRunState {
  currentModel: string;
  currentProvider: string;
  run: RunState | null;
  toolCallCount: number;
  errorCount: number;
  turnCount: number;
}

export function newTraceId(): string {
  return randomBytes(16).toString("hex");
}

export function newSpanId(): string {
  return randomBytes(8).toString("hex");
}

const DEFAULT_SESSION_ID = "__pi_hook_default_session__";

let activeSessionId = DEFAULT_SESSION_ID;
const sessionScope = new AsyncLocalStorage<string>();
const sessionStates = new Map<string, SessionRunState>();

function createSessionRunState(): SessionRunState {
  return {
    currentModel: "",
    currentProvider: "",
    run: null,
    toolCallCount: 0,
    errorCount: 0,
    turnCount: 0,
  };
}

function normalizeSessionId(sessionId?: string) {
  return sessionId || DEFAULT_SESSION_ID;
}

function getActiveSessionId() {
  return sessionScope.getStore() ?? activeSessionId;
}

export function getSessionRunState(sessionId = getActiveSessionId()): SessionRunState {
  const normalizedSessionId = normalizeSessionId(sessionId);
  let sessionState = sessionStates.get(normalizedSessionId);
  if (!sessionState) {
    sessionState = createSessionRunState();
    sessionStates.set(normalizedSessionId, sessionState);
  }
  return sessionState;
}

export function setCurrentSession(sessionId?: string) {
  activeSessionId = normalizeSessionId(sessionId);
  getSessionRunState(activeSessionId);
}

export function runWithSession<T>(sessionId: string | undefined, fn: () => T): T {
  const normalizedSessionId = normalizeSessionId(sessionId);
  setCurrentSession(normalizedSessionId);
  return sessionScope.run(normalizedSessionId, fn);
}

export function currentSessionId(): string {
  const sessionId = getActiveSessionId();
  return sessionId === DEFAULT_SESSION_ID ? "" : sessionId;
}

export function resetRunState(sessionId = getActiveSessionId()) {
  sessionStates.set(normalizeSessionId(sessionId), createSessionRunState());
}

export function clearAllSessionStates() {
  sessionStates.clear();
  activeSessionId = DEFAULT_SESSION_ID;
  getSessionRunState();
}

export function computeEvaluationScores(sessionId = getActiveSessionId()) {
  const sessionState = getSessionRunState(sessionId);
  const toolSuccessRate =
    sessionState.toolCallCount > 0
      ? (sessionState.toolCallCount - sessionState.errorCount) / sessionState.toolCallCount
      : 1;

  return {
    tool_call_count: sessionState.toolCallCount,
    turn_count: sessionState.turnCount,
    total_tool_errors: sessionState.errorCount,
    tool_success_rate: toolSuccessRate,
    session_had_errors: sessionState.errorCount > 0 ? 1 : 0,
  };
}

/** Create a span record, register it in the run tree, and return it. */
export function startSpanRecord(
  run: RunState,
  fields: Omit<SpanRecord, "spanId" | "startTimeMs"> & { startTimeMs?: number },
): SpanRecord {
  const record: SpanRecord = {
    spanId: newSpanId(),
    startTimeMs: fields.startTimeMs ?? Date.now(),
    ...fields,
  };
  run.spans.push(record);
  return record;
}

export function endSpanRecord(record: SpanRecord, endTimeMs = Date.now()) {
  if (record.endTimeMs === undefined) {
    record.endTimeMs = Math.max(endTimeMs, record.startTimeMs);
  }
}

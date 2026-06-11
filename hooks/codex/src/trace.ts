import type { Dirent } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";

import { propagateAttributes, startObservation, type LangfuseObservation } from "@langfuse/tracing";

import type { Config } from "./config.js";
import { appendFailedTrace, buildTraceV2, postLangstash } from "./langstash.js";
import { parseSession } from "./parse.js";
import { loadUploadedTurnIds, markTurnUploaded } from "./sidecar.js";
import type { ModelStep, RolloutLine, SessionMeta, TokenUsage, ToolCall, Turn } from "./types.js";
import { debugLog, error, info, warn, toText, truncate } from "./utils.js";

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

async function emitTurnOtel(
  turn: Turn,
  sessionMeta: SessionMeta,
  ctx: {
    config: Config;
    rolloutFile: string;
    traceName: string;
    parentObservation?: LangfuseObservation;
  },
): Promise<void> {
  const clip = makeClip(ctx.config.max_chars);

  const root = startObservation(
    ctx.traceName,
    {
      input: turn.userInput != null ? clip(turn.userInput) : undefined,
      output: turn.finalOutput != null ? clip(turn.finalOutput) : undefined,
      level: turn.aborted ? "WARNING" : undefined,
      statusMessage: turn.aborted ? "Turn interrupted by user" : undefined,
      metadata: {
        "codex.turn_id": turn.turnId,
        "codex.thread_id": sessionMeta.threadId,
        "codex.model": turn.model,
        "codex.model_provider": sessionMeta.modelProvider,
        "codex.cli_version": sessionMeta.cliVersion,
        "codex.aborted": turn.aborted,
        "codex.tool_call_count": turn.steps.reduce((n, s) => n + s.toolCalls.length, 0),
        ...(sessionMeta.isSubagent
          ? {
              "codex.is_subagent": true,
              "codex.parent_thread_id": sessionMeta.parentThreadId,
              "codex.agent_nickname": sessionMeta.agentNickname,
            }
          : {}),
      },
    },
    {
      asType: "agent",
      startTime: new Date(turn.startTime),
      parentSpanContext: ctx.parentObservation?.otelSpan.spanContext(),
    },
  );

  let previousToolResults: unknown = undefined;

  for (let i = 0; i < turn.steps.length; i++) {
    const step = turn.steps[i];
    const generation = startObservation(
      turn.model ?? "codex.generation",
      {
        input:
          i === 0
            ? turn.userInput != null
              ? clip(turn.userInput)
              : undefined
            : previousToolResults,
        output: buildGenerationOutput(step, clip),
        model: turn.model,
        usageDetails: toUsageDetails(step.usage),
        metadata: { "codex.step_index": i },
      },
      {
        asType: "generation",
        startTime: new Date(step.startTime),
        parentSpanContext: root.otelSpan.spanContext(),
      },
    );

    for (const tc of step.toolCalls) {
      emitToolCall(tc, generation, clip, step.endTime);
    }

    generation.end(new Date(step.endTime));

    previousToolResults =
      step.toolCalls.length > 0
        ? step.toolCalls.map((tc) => ({
            name: tc.name,
            output: tc.output != null ? clip(toText(tc.output)) : undefined,
            ...(tc.error ? { error: clip(tc.error) } : {}),
          }))
        : undefined;
  }

  for (const threadId of turn.subagentThreadIds) {
    const subFile = await findSubagentRollout(ctx.rolloutFile, threadId);
    if (!subFile) {
      warn(`subagent rollout not found for thread ${threadId}`);
      continue;
    }
    await convertRollout(subFile, { config: ctx.config, parentObservation: root });
  }

  root.end(new Date(turn.endTime));
}

function emitToolCall(
  tc: ToolCall,
  parent: LangfuseObservation,
  clip: Clip,
  fallbackEnd: number,
): void {
  const tool = startObservation(
    tc.name || "tool",
    {
      input: tc.args,
      output: tc.output != null ? clip(toText(tc.output)) : undefined,
      level: tc.error ? "ERROR" : undefined,
      statusMessage: tc.error ? clip(tc.error) : undefined,
      metadata: { "codex.call_id": tc.callId },
    },
    {
      asType: "tool",
      startTime: new Date(tc.startTime),
      parentSpanContext: parent.otelSpan.spanContext(),
    },
  );
  tool.end(new Date(tc.endTime ?? fallbackEnd));
}

export async function convertRollout(
  rolloutFile: string,
  options: { config: Config; parentObservation?: LangfuseObservation },
): Promise<void> {
  const { sessionMeta, turns } = parseSession(await loadSession(rolloutFile));
  info(`parsed ${turns.length} turn(s) from ${path.basename(rolloutFile)}`);

  if (options.parentObservation) {
    for (let i = 0; i < turns.length; i++) {
      await emitTurnOtel(turns[i], sessionMeta, {
        config: options.config,
        rolloutFile,
        traceName: `Codex - Subagent Turn ${i + 1}`,
        parentObservation: options.parentObservation,
      });
    }
    return;
  }

  const uploaded = await loadUploadedTurnIds(rolloutFile);

  for (let i = 0; i < turns.length; i++) {
    const turn = turns[i];
    const effectiveId = turn.turnId ?? `idx-${i}`;

    if (uploaded.has(effectiveId)) {
      debugLog(`skipping already-uploaded turn ${effectiveId}`);
      continue;
    }

    const traceName = `Codex - Turn ${i + 1}`;
    let delivered = false;

    // Tier 1: langstash
    if (options.config.langstash_enabled) {
      try {
        const traceJson = buildTraceV2(turn, sessionMeta, options.config, traceName);
        delivered = await postLangstash(traceJson, options.config);
        if (delivered) {
          info(`delivered turn ${effectiveId} via langstash`);
        } else {
          warn("langstash delivery failed, falling back to OTel");
        }
      } catch (e) {
        error("langstash build/post error:", e);
      }
    }

    // Tier 2: OTel direct push
    if (!delivered) {
      try {
        await propagateAttributes(
          {
            sessionId: sessionMeta.sessionId,
            traceName,
            ...(options.config.user_id ? { userId: options.config.user_id } : {}),
            ...(options.config.tags ? { tags: options.config.tags } : {}),
          },
          async () => {
            await emitTurnOtel(turn, sessionMeta, {
              config: options.config,
              rolloutFile,
              traceName,
            });
          },
        );
        delivered = true;
      } catch (e) {
        error("OTel direct push failed:", e);
      }
    }

    // Tier 3: failed log
    if (!delivered) {
      try {
        const traceJson = buildTraceV2(turn, sessionMeta, options.config, traceName);
        appendFailedTrace(traceJson);
        warn("trace saved to failed log");
      } catch (e) {
        error("failed log write error:", e);
      }
    }

    uploaded.add(effectiveId);
    await markTurnUploaded(rolloutFile, effectiveId);
  }
}

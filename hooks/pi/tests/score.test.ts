import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { collectScores, mirrorScoresIntoRootMetadata, sendScores } from "../src/score.ts";
import { clearAllSessionStates, getSessionRunState, runWithSession } from "../src/state.ts";
import { loadExtension, replayBasicRun } from "./fake-pi.ts";

interface CapturedRequest {
  url: string;
  body: any;
}

function collectSpans(otlp: Record<string, unknown>): any[] {
  return (otlp.resourceSpans as any[]).flatMap((rs) =>
    (rs.scopeSpans as any[]).flatMap((ss) => ss.spans as any[]),
  );
}

function attrValue(span: any, key: string): string | undefined {
  const attr = (span.attributes as any[]).find((a) => a.key === key);
  return attr?.value?.stringValue;
}

describe("score delivery", () => {
  let requests: CapturedRequest[];
  const savedEnv = { ...process.env };

  beforeEach(() => {
    clearAllSessionStates();
    requests = [];
    // Sandbox HOME so a locally installed pi.env with real credentials never leaks in.
    process.env.HOME = mkdtempSync(join(tmpdir(), "pi-home-"));
    process.env.LANGSTASH_ENABLED = "true";
    process.env.LANGSTASH_URL = "http://127.0.0.1:15288";
    process.env.LANGFUSE_BASE_URL = "https://langfuse.test";
    process.env.LANGFUSE_PUBLIC_KEY = "pk-lf-test";
    process.env.LANGFUSE_SECRET_KEY = "sk-lf-test";
    vi.stubGlobal("fetch", async (url: string, options: any) => {
      requests.push({ url: String(url), body: JSON.parse(options.body) });
      return { ok: true, status: 202 };
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(process.env)) {
      if (!(key in savedEnv)) delete process.env[key];
    }
    Object.assign(process.env, savedEnv);
  });

  const ingestRequests = () => requests.filter((r) => r.url.endsWith("/ingest"));
  const scoreRequests = () => requests.filter((r) => r.url.includes("/api/public/ingestion"));

  it("sends the five trace-level scores with correct dataType and traceId after a normal run", async () => {
    const pi = await loadExtension("/tmp/sessions/score-normal.jsonl");
    await replayBasicRun(pi);

    expect(ingestRequests(), "trace 必须经 /ingest 投递恰好一次").toHaveLength(1);
    expect(scoreRequests(), "score 必须直连 /api/public/ingestion 恰好一次").toHaveLength(1);

    const traceId = collectSpans(ingestRequests()[0].body)[0].traceId;
    const batch = scoreRequests()[0].body.batch as any[];
    const byName = new Map(batch.map((e) => [e.body.name, e]));

    for (const name of ["tool_call_count", "turn_count", "total_tool_errors", "tool_success_rate"]) {
      expect(byName.get(name), `${name} score 必须存在`).toBeTruthy();
      expect(byName.get(name)!.body.dataType, `${name} 必须是 NUMERIC`).toBe("NUMERIC");
      expect(byName.get(name)!.body.traceId, `${name} 必须关联已投递 trace 的 traceId`).toBe(traceId);
      expect(byName.get(name)!.type).toBe("score-create");
    }
    expect(byName.get("session_had_errors")!.body.dataType, "session_had_errors 必须是 BOOLEAN").toBe("BOOLEAN");
    expect(byName.get("session_had_errors")!.body.traceId).toBe(traceId);

    expect(byName.get("tool_call_count")!.body.value, "成功 run 的工具计数为 1").toBe(1);
    expect(byName.get("turn_count")!.body.value).toBe(1);
    expect(byName.get("total_tool_errors")!.body.value).toBe(0);
    expect(byName.get("tool_success_rate")!.body.value).toBe(1);
    expect(byName.get("session_had_errors")!.body.value).toBe(0);
    expect(
      batch.some((e) => e.body.name === "tool_is_error"),
      "无失败工具时不得发送 tool_is_error",
    ).toBe(false);
  });

  it("appends tool_is_error bound to the failed tool span id", async () => {
    const pi = await loadExtension("/tmp/sessions/score-toolerror.jsonl");
    await replayBasicRun(pi, { toolFails: true });

    const spans = collectSpans(ingestRequests()[0].body);
    const toolSpan = spans.find((s) => attrValue(s, "langfuse.observation.type") === "tool");
    const batch = scoreRequests()[0].body.batch as any[];
    const toolScore = batch.find((e) => e.body.name === "tool_is_error");

    expect(toolScore, "失败工具必须追加 tool_is_error").toBeTruthy();
    expect(toolScore.body.dataType, "tool_is_error 必须是 BOOLEAN").toBe("BOOLEAN");
    expect(toolScore.body.value).toBe(1);
    expect(toolScore.body.observationId, "tool_is_error 必须关联该 tool span 的 spanId").toBe(toolSpan.spanId);
    expect(toolScore.body.traceId).toBe(toolSpan.traceId);

    const byName = new Map(batch.map((e) => [e.body.name, e]));
    expect(byName.get("total_tool_errors")!.body.value, "失败工具必须计入 total_tool_errors").toBe(1);
    expect(byName.get("tool_success_rate")!.body.value).toBe(0);
    expect(byName.get("session_had_errors")!.body.value).toBe(1);
  });

  it("keeps the trace delivered and the metadata mirror intact when the score endpoint fails", async () => {
    vi.stubGlobal("fetch", async (url: string, options: any) => {
      const target = String(url);
      if (target.includes("/api/public/ingestion")) {
        throw new Error("score endpoint unreachable");
      }
      requests.push({ url: target, body: JSON.parse(options.body) });
      return { ok: true, status: 202 };
    });

    const pi = await loadExtension("/tmp/sessions/score-failure.jsonl");
    await replayBasicRun(pi, { toolFails: true });

    expect(ingestRequests(), "score 接口不可达不得影响 trace 投递").toHaveLength(1);
    const root = collectSpans(ingestRequests()[0].body).find((s) => !s.parentSpanId);
    const rootMeta = JSON.parse(attrValue(root, "langfuse.observation.metadata")!);
    expect(rootMeta, "root metadata 必须镜像全部聚合值作为兜底").toMatchObject({
      tool_call_count: 1,
      turn_count: 1,
      total_tool_errors: 1,
      tool_success_rate: 0,
      session_had_errors: 1,
    });
  });

  it("sends no score request when credentials are missing", async () => {
    delete process.env.LANGFUSE_PUBLIC_KEY;
    delete process.env.LANGFUSE_SECRET_KEY;

    const pi = await loadExtension("/tmp/sessions/score-nocreds.jsonl");
    await replayBasicRun(pi);

    expect(ingestRequests(), "凭据缺失时 trace 仍走 Tier 1 投递").toHaveLength(1);
    expect(scoreRequests(), "凭据缺失时不得发出 score 请求").toHaveLength(0);
  });

  it("mirrors aggregates into root metadata and never touches the exporter ingest path for scores", async () => {
    await runWithSession("mirror-session", async () => {
      const session = getSessionRunState();
      session.toolCallCount = 4;
      session.errorCount = 1;
      session.turnCount = 2;

      const run = {
        traceId: "a".repeat(32),
        root: { spanId: "b".repeat(16), name: "pi-agent", type: "agent" as const, startTimeMs: 1, metadata: { cwd: "/tmp" } },
        spans: [] as any[],
        activeGenerations: new Map(),
        generationOrder: [],
        activeTools: new Map(),
        generationSeq: 0,
        cwd: "/tmp",
        emitted: false,
      };
      run.spans.push(run.root);

      mirrorScoresIntoRootMetadata(run as any);
      expect(run.root.metadata, "镜像必须保留原有 metadata 并叠加聚合值").toEqual({
        cwd: "/tmp",
        tool_call_count: 4,
        turn_count: 2,
        total_tool_errors: 1,
        tool_success_rate: 0.75,
        session_had_errors: 1,
      });

      const calls: string[] = [];
      const ok = await sendScores(run as any, {
        fetchFn: async (url) => {
          calls.push(url);
          return { ok: true, status: 202 };
        },
      });
      expect(ok, "凭据齐备且请求成功时 sendScores 必须返回 true").toBe(true);
      expect(calls, "score 只能发往 Langfuse ingestion 端点").toEqual([
        "https://langfuse.test/api/public/ingestion",
      ]);
      expect(
        calls.some((url) => url.endsWith("/ingest")),
        "score 载荷不得进入 exporter 的 /ingest",
      ).toBe(false);
    });
  });

  it("collectScores derives entries from run spans and session counters only", () => {
    runWithSession("collect-session", () => {
      const session = getSessionRunState();
      session.toolCallCount = 2;
      session.errorCount = 2;
      session.turnCount = 1;

      const failedTool = {
        spanId: "c".repeat(16),
        name: "bash",
        type: "tool" as const,
        level: "ERROR" as const,
        startTimeMs: 1,
      };
      const okTool = {
        spanId: "d".repeat(16),
        name: "read_file",
        type: "tool" as const,
        level: "DEFAULT" as const,
        startTimeMs: 1,
      };
      const run = {
        traceId: "e".repeat(32),
        root: { spanId: "f".repeat(16), name: "pi-agent", type: "agent" as const, startTimeMs: 1 },
        spans: [] as any[],
        activeGenerations: new Map(),
        generationOrder: [],
        activeTools: new Map(),
        generationSeq: 0,
        cwd: "/tmp",
        emitted: false,
      };
      run.spans.push(run.root, failedTool, okTool);

      const entries = collectScores(run as any);
      const errorEntries = entries.filter((e) => e.name === "tool_is_error");
      expect(errorEntries, "仅 ERROR 级 tool 记录产生 tool_is_error").toHaveLength(1);
      expect(errorEntries[0].observationId).toBe(failedTool.spanId);
      expect(entries.filter((e) => e.observationId === undefined), "五项聚合评分关联 trace 而非观测").toHaveLength(5);
    });
  });
});

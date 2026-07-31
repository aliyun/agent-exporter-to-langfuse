import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { checkpointDir, clearCheckpoint, recoverCheckpoints, writeCheckpoint } from "../src/checkpoint.ts";
import { clearAllSessionStates } from "../src/state.ts";
import { loadExtension, replayBasicRun } from "./fake-pi.ts";

function collectSpans(otlp: Record<string, unknown>): any[] {
  return (otlp.resourceSpans as any[]).flatMap((rs) =>
    (rs.scopeSpans as any[]).flatMap((ss) => ss.spans as any[]),
  );
}

function attrValue(span: any, key: string): string | undefined {
  const attr = (span.attributes as any[]).find((a) => a.key === key);
  return attr?.value?.stringValue;
}

describe("checkpoint recovery", () => {
  let home: string;
  let ckptDir: string;
  let requests: Array<Record<string, unknown>>;
  const savedEnv = { ...process.env };

  beforeEach(() => {
    clearAllSessionStates();
    requests = [];
    home = mkdtempSync(join(tmpdir(), "pi-home-"));
    process.env.HOME = home;
    process.env.LANGSTASH_ENABLED = "true";
    process.env.LANGSTASH_URL = "http://127.0.0.1:15288";
    ckptDir = checkpointDir();
    vi.stubGlobal("fetch", async (_url: string, options: any) => {
      requests.push(JSON.parse(options.body));
      return { ok: true, status: 202 };
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(process.env)) {
      if (!(key in savedEnv)) delete process.env[key];
    }
    Object.assign(process.env, savedEnv);
    rmSync(home, { recursive: true, force: true });
  });

  it("writes a checkpoint containing the root record after turn_end and removes it after agent_end", async () => {
    const pi = await loadExtension("/tmp/sessions/ckpt-normal.jsonl");
    await pi.emit("session_start");
    await pi.emit("before_agent_start", { prompt: "checkpointed" });
    await pi.emit("turn_start", {});
    await pi.emit("turn_end", {});

    const afterTurn = readdirSync(ckptDir).filter((n) => n.endsWith(".json"));
    expect(afterTurn, "turn_end 后必须存在恰好一个 checkpoint 文件").toEqual(["ckpt-normal.json"]);
    const payload = JSON.parse(readFileSync(join(ckptDir, "ckpt-normal.json"), "utf8"));
    expect(
      payload.spans.some((s: any) => s.spanId === payload.rootSpanId),
      "checkpoint 必须含 run 开始即创建的 root 记录",
    ).toBe(true);

    await pi.emit("agent_end", {});
    expect(
      readdirSync(ckptDir).filter((n) => n.endsWith(".json")),
      "agent_end 正常发射后 checkpoint 必须被删除",
    ).toEqual([]);
    expect(requests, "正常 run 只投递一条 trace").toHaveLength(1);
  });

  it("re-delivers a leftover checkpoint as a cancelled partial trace exactly once", async () => {
    // First process: crash right after turn_end (no agent_end, no shutdown).
    const crashed = await loadExtension("/tmp/sessions/ckpt-crash.jsonl");
    await crashed.emit("session_start");
    await crashed.emit("before_agent_start", { prompt: "will crash" });
    await crashed.emit("turn_start", {});
    await crashed.emit("tool_execution_start", { toolCallId: "tc-open", toolName: "bash" });
    await crashed.emit("turn_end", {});
    expect(requests, "崩溃前不得有任何 trace 投递").toHaveLength(0);

    // Second process load: leftover checkpoint is re-delivered.
    clearAllSessionStates();
    await loadExtension("/tmp/sessions/ckpt-crash.jsonl");
    expect(requests, "遗留 checkpoint 必须在加载时补发恰好一条 trace").toHaveLength(1);

    const spans = collectSpans(requests[0]);
    const root = spans.find((s) => !s.parentSpanId);
    expect(root, "补发的部分 trace 必须含 root span（满足 validate_otlp）").toBeTruthy();
    const rootMeta = JSON.parse(attrValue(root, "langfuse.observation.metadata")!);
    expect(rootMeta.cancelled, "补发 trace 的 root 必须标记 cancelled").toBe(true);
    expect(rootMeta.completed).toBe(false);
    const toolSpan = spans.find((s) => s.name === "bash");
    expect(
      JSON.parse(attrValue(toolSpan, "langfuse.observation.metadata")!).cancelled,
      "未结束的工具记录必须标记 cancelled",
    ).toBe(true);
    expect(attrValue(toolSpan, "langfuse.observation.level")).toBe("WARNING");
    for (const span of spans) {
      expect(span.endTimeUnixNano, "补发 trace 的所有 span 必须有结束时间").toMatch(/^\d+$/);
    }

    // Third load: no duplicate re-delivery.
    clearAllSessionStates();
    await loadExtension("/tmp/sessions/ckpt-crash.jsonl");
    expect(requests, "二次加载不得重复补发").toHaveLength(1);
    expect(readdirSync(ckptDir).filter((n) => n.endsWith(".json"))).toEqual([]);
  });

  it("emits a cancelled partial trace on session_shutdown and clears the checkpoint", async () => {
    const pi = await loadExtension("/tmp/sessions/ckpt-shutdown.jsonl");
    await pi.emit("session_start");
    await pi.emit("before_agent_start", { prompt: "interrupted" });
    await pi.emit("turn_start", {});
    await pi.emit("tool_execution_start", { toolCallId: "tc-hanging", toolName: "read_file" });
    await pi.emit("before_provider_request", { requestId: "req-hanging", request: {} });
    await pi.emit("session_shutdown", {});

    expect(requests, "session_shutdown 必须投递一条部分 trace").toHaveLength(1);
    const spans = collectSpans(requests[0]);
    const root = spans.find((s) => !s.parentSpanId);
    const rootMeta = JSON.parse(attrValue(root, "langfuse.observation.metadata")!);
    expect(rootMeta, "中断 trace 的 root metadata 必须标记 completed:false, cancelled:true").toMatchObject({
      completed: false,
      cancelled: true,
    });

    for (const name of ["read_file", "llm-generation", "turn"]) {
      const span = spans.find((s) => s.name === name);
      expect(span, `${name} span 必须存在于中断 trace 中`).toBeTruthy();
      expect(attrValue(span, "langfuse.observation.level"), `${name} 悬挂记录必须标记 WARNING`).toBe("WARNING");
      expect(
        JSON.parse(attrValue(span, "langfuse.observation.metadata")!).cancelled,
        `${name} 悬挂记录必须标记 cancelled`,
      ).toBe(true);
    }

    expect(
      existsSync(ckptDir) ? readdirSync(ckptDir).filter((n) => n.endsWith(".json")) : [],
      "中断发射成功后 checkpoint 必须被清除",
    ).toEqual([]);

    // A later load must not re-deliver the same run.
    clearAllSessionStates();
    await loadExtension("/tmp/sessions/ckpt-shutdown.jsonl");
    expect(requests, "同一 run 不得既中断发射又被补发").toHaveLength(1);
  });

  it("keeps the normal trace flow working when the checkpoint directory is not writable", async () => {
    mkdirSync(ckptDir, { recursive: true });
    chmodSync(ckptDir, 0o500);
    try {
      const pi = await loadExtension("/tmp/sessions/ckpt-readonly.jsonl");
      await replayBasicRun(pi);
      expect(requests, "checkpoint 写入失败不得影响正常 trace 投递").toHaveLength(1);
      const spans = collectSpans(requests[0]);
      expect(spans.filter((s) => !s.parentSpanId)).toHaveLength(1);
    } finally {
      chmodSync(ckptDir, 0o700);
    }
  });

  it("discards an unreadable or root-less checkpoint without delivering a trace", async () => {
    mkdirSync(ckptDir, { recursive: true });
    writeFileSync(join(ckptDir, "broken.json"), "{not json");
    writeFileSync(
      join(ckptDir, "rootless.json"),
      JSON.stringify({
        sessionId: "rootless",
        traceId: "f".repeat(32),
        rootSpanId: "0".repeat(16),
        spans: [{ spanId: "1".repeat(16), parentSpanId: "2".repeat(16), name: "orphan", type: "tool", startTimeMs: 1 }],
        cwd: "/tmp",
        savedAtMs: Date.now(),
      }),
    );

    await loadExtension("/tmp/sessions/ckpt-discard.jsonl");

    expect(requests, "不可读或缺 root 的 checkpoint 不得产生投递").toHaveLength(0);
    expect(readdirSync(ckptDir).filter((n) => n.endsWith(".json")), "损坏的 checkpoint 必须被清除").toEqual([]);
  });

  it("clearCheckpoint is a no-op when no checkpoint exists", () => {
    mkdirSync(ckptDir, { recursive: true });
    expect(() => clearCheckpoint("never-written", ckptDir)).not.toThrow();
    expect(existsSync(join(ckptDir, "never-written.json"))).toBe(false);
  });

  it("writeCheckpoint leaves no temporary file behind", () => {
    const run = {
      traceId: "b".repeat(32),
      root: { spanId: "c".repeat(16), name: "pi-agent", type: "agent" as const, startTimeMs: 1 },
      spans: [] as any[],
      activeGenerations: new Map(),
      generationOrder: [],
      activeTools: new Map(),
      generationSeq: 0,
      cwd: "/tmp",
      emitted: false,
    };
    run.spans.push(run.root);

    writeCheckpoint(run as any, "tmp-check", ckptDir);
    const files = readdirSync(ckptDir);
    expect(files, "原子写入后不得残留 .tmp 文件").toEqual(["tmp-check.json"]);

    const recovered = recoverCheckpoints(ckptDir);
    expect(recovered, "写入的 checkpoint 必须可被重建").toHaveLength(1);
    expect(recovered[0].run.traceId).toBe("b".repeat(32));
  });
});

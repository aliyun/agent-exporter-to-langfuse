import { beforeEach, describe, expect, it } from "vitest";

import {
  clearAllSessionStates,
  computeEvaluationScores,
  getSessionRunState,
  runWithSession,
} from "../src/state.ts";

describe("session isolation", () => {
  beforeEach(() => {
    clearAllSessionStates();
  });

  it("keeps counters of two sessions invisible to each other", () => {
    runWithSession("session-a", () => {
      const s = getSessionRunState();
      s.toolCallCount = 3;
      s.errorCount = 1;
      s.turnCount = 2;
    });

    runWithSession("session-b", () => {
      const s = getSessionRunState();
      expect(s.toolCallCount, "session-b 不得看到 session-a 的 toolCallCount").toBe(0);
      expect(s.errorCount, "session-b 不得看到 session-a 的 errorCount").toBe(0);
      expect(s.turnCount, "session-b 不得看到 session-a 的 turnCount").toBe(0);
    });

    runWithSession("session-a", () => {
      const s = getSessionRunState();
      expect(s.toolCallCount, "session-a 的计数必须保留").toBe(3);
    });
  });

  it("isolates state across overlapping async session work", async () => {
    const results: Record<string, number> = {};

    await Promise.all([
      runWithSession("async-a", async () => {
        getSessionRunState().toolCallCount = 10;
        await new Promise((resolve) => setTimeout(resolve, 20));
        results.a = getSessionRunState().toolCallCount;
      }),
      runWithSession("async-b", async () => {
        getSessionRunState().toolCallCount = 99;
        await new Promise((resolve) => setTimeout(resolve, 5));
        results.b = getSessionRunState().toolCallCount;
      }),
    ]);

    expect(results.a, "并发场景下 async-a 读到的必须是自己的计数").toBe(10);
    expect(results.b, "并发场景下 async-b 读到的必须是自己的计数").toBe(99);
  });

  it("computes evaluation scores from the active session only", () => {
    runWithSession("score-session", () => {
      const s = getSessionRunState();
      s.toolCallCount = 4;
      s.errorCount = 1;
      s.turnCount = 2;

      expect(computeEvaluationScores()).toEqual({
        tool_call_count: 4,
        turn_count: 2,
        total_tool_errors: 1,
        tool_success_rate: 0.75,
        session_had_errors: 1,
      });
    });

    runWithSession("clean-session", () => {
      expect(computeEvaluationScores()).toEqual({
        tool_call_count: 0,
        turn_count: 0,
        total_tool_errors: 0,
        tool_success_rate: 1,
        session_had_errors: 0,
      });
    });
  });
});

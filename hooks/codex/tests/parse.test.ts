import { describe, expect, it } from "vitest";
import { parseSession } from "../src/parse.js";
import type { RolloutLine } from "../src/types.js";

const T0 = "2025-01-01T00:00:00.000Z";
const T1 = "2025-01-01T00:00:01.000Z";
const T2 = "2025-01-01T00:00:02.000Z";
const T3 = "2025-01-01T00:00:03.000Z";
const T4 = "2025-01-01T00:00:04.000Z";

function makeSessionMeta(
  id: string,
  opts?: { parentThreadId?: string; agentNickname?: string; cliVersion?: string },
): RolloutLine {
  return {
    timestamp: T0,
    type: "session_meta",
    payload: {
      id,
      cli_version: opts?.cliVersion ?? "1.0.0",
      model_provider: "openai",
      ...(opts?.parentThreadId
        ? {
            source: {
              subagent: {
                thread_spawn: {
                  parent_thread_id: opts.parentThreadId,
                  agent_nickname: opts.agentNickname,
                },
              },
            },
          }
        : {}),
    },
  };
}

function makeTaskStarted(ts: string, turnId: string): RolloutLine {
  return {
    timestamp: ts,
    type: "event_msg",
    payload: { type: "task_started", turn_id: turnId },
  };
}

function makeUserMessage(ts: string, message: string): RolloutLine {
  return {
    timestamp: ts,
    type: "event_msg",
    payload: { type: "user_message", message },
  };
}

function makeResponseMessage(
  ts: string,
  role: string,
  text: string,
): RolloutLine {
  return {
    timestamp: ts,
    type: "response_item",
    payload: {
      type: "message",
      role,
      content: [{ type: "output_text", text }],
    },
  };
}

function makeFunctionCall(
  ts: string,
  callId: string,
  name: string,
  args: string,
): RolloutLine {
  return {
    timestamp: ts,
    type: "response_item",
    payload: {
      type: "function_call",
      call_id: callId,
      name,
      arguments: args,
    },
  };
}

function makeFunctionCallOutput(
  ts: string,
  callId: string,
  output: string,
): RolloutLine {
  return {
    timestamp: ts,
    type: "response_item",
    payload: {
      type: "function_call_output",
      call_id: callId,
      output,
    },
  };
}

function makeTokenCount(
  ts: string,
  lastUsage: { input_tokens: number; output_tokens: number },
  totalUsage?: { input_tokens: number; output_tokens: number },
): RolloutLine {
  return {
    timestamp: ts,
    type: "event_msg",
    payload: {
      type: "token_count",
      info: {
        last_token_usage: lastUsage,
        ...(totalUsage ? { total_token_usage: totalUsage } : {}),
      },
    },
  };
}

function makeTaskComplete(ts: string): RolloutLine {
  return {
    timestamp: ts,
    type: "event_msg",
    payload: { type: "task_complete" },
  };
}

function makeTurnContext(ts: string, model: string): RolloutLine {
  return {
    timestamp: ts,
    type: "turn_context",
    payload: { model },
  };
}

describe("parseSession", () => {
  describe("session_meta extraction", () => {
    it("extracts sessionId and threadId from session_meta", () => {
      const lines: RolloutLine[] = [makeSessionMeta("thread-abc")];
      const { sessionMeta } = parseSession(lines);
      expect(sessionMeta.sessionId).toBe("thread-abc");
      expect(sessionMeta.threadId).toBe("thread-abc");
      expect(sessionMeta.isSubagent).toBe(false);
    });

    it("detects subagent from parent_thread_id", () => {
      const lines: RolloutLine[] = [
        makeSessionMeta("child-thread", {
          parentThreadId: "parent-thread",
          agentNickname: "researcher",
        }),
      ];
      const { sessionMeta } = parseSession(lines);
      expect(sessionMeta.isSubagent).toBe(true);
      expect(sessionMeta.sessionId).toBe("parent-thread");
      expect(sessionMeta.threadId).toBe("child-thread");
      expect(sessionMeta.parentThreadId).toBe("parent-thread");
      expect(sessionMeta.agentNickname).toBe("researcher");
    });

    it("extracts cliVersion and modelProvider", () => {
      const lines: RolloutLine[] = [
        makeSessionMeta("t1", { cliVersion: "2.5.0" }),
      ];
      const { sessionMeta } = parseSession(lines);
      expect(sessionMeta.cliVersion).toBe("2.5.0");
      expect(sessionMeta.modelProvider).toBe("openai");
    });

    it("returns defaults when no lines", () => {
      const { sessionMeta, turns } = parseSession([]);
      expect(sessionMeta.sessionId).toBe("unknown");
      expect(sessionMeta.threadId).toBe("unknown");
      expect(sessionMeta.isSubagent).toBe(false);
      expect(turns).toEqual([]);
    });
  });

  describe("single turn", () => {
    it("parses a complete turn: task_started -> user_message -> response -> task_complete", () => {
      const lines: RolloutLine[] = [
        makeSessionMeta("sess-1"),
        makeTaskStarted(T1, "turn-1"),
        makeTurnContext(T1, "gpt-4o"),
        makeUserMessage(T1, "Hello, what is 2+2?"),
        makeResponseMessage(T2, "assistant", "2+2 equals 4."),
        makeTokenCount(T2, { input_tokens: 10, output_tokens: 5 }),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      expect(turns).toHaveLength(1);

      const turn = turns[0];
      expect(turn.turnId).toBe("turn-1");
      expect(turn.userInput).toBe("Hello, what is 2+2?");
      expect(turn.finalOutput).toBe("2+2 equals 4.");
      expect(turn.completed).toBe(true);
      expect(turn.aborted).toBe(false);
      expect(turn.model).toBe("gpt-4o");
      expect(turn.steps).toHaveLength(1);
      expect(turn.steps[0].text).toBe("2+2 equals 4.");
      expect(turn.steps[0].usage).toEqual({ input_tokens: 10, output_tokens: 5 });
    });

    it("sets userInput from event_msg user_message", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "first message"),
        makeResponseMessage(T1, "assistant", "ok"),
        makeTaskComplete(T2),
      ];
      const { turns } = parseSession(lines);
      expect(turns[0].userInput).toBe("first message");
    });

    it("uses userInputFallback from response_item user message if no event_msg user_message", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeResponseMessage(T0, "user", "fallback user input"),
        makeResponseMessage(T1, "assistant", "ok"),
        makeTaskComplete(T2),
      ];
      const { turns } = parseSession(lines);
      expect(turns[0].userInput).toBe("fallback user input");
    });

    it("ignores user messages that look like environment_context", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        {
          timestamp: T0,
          type: "response_item",
          payload: {
            type: "message",
            role: "user",
            content: [{ type: "input_text", text: "<environment_context>something</environment_context>" }],
          },
        } as RolloutLine,
        makeResponseMessage(T1, "user", "real user message"),
        makeResponseMessage(T2, "assistant", "ok"),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      expect(turns[0].userInput).toBe("real user message");
    });
  });

  describe("tool calls", () => {
    it("associates function_call with function_call_output by call_id", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "run ls"),
        makeFunctionCall(T1, "call-1", "shell", '{"cmd":"ls"}'),
        makeFunctionCallOutput(T2, "call-1", "file1.txt\nfile2.txt"),
        makeResponseMessage(T3, "assistant", "Here are the files."),
        makeTokenCount(T3, { input_tokens: 20, output_tokens: 10 }),
        makeTaskComplete(T4),
      ];
      const { turns } = parseSession(lines);
      expect(turns).toHaveLength(1);

      const step = turns[0].steps[0];
      expect(step.toolCalls).toHaveLength(1);
      expect(step.toolCalls[0].callId).toBe("call-1");
      expect(step.toolCalls[0].name).toBe("shell");
      expect(step.toolCalls[0].args).toEqual({ cmd: "ls" });
      expect(step.toolCalls[0].output).toBe("file1.txt\nfile2.txt");
    });

    it("handles multiple tool calls in a single step", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "do stuff"),
        makeFunctionCall(T1, "call-a", "read_file", '{"path":"a.txt"}'),
        makeFunctionCallOutput(T1, "call-a", "content-a"),
        makeFunctionCall(T2, "call-b", "read_file", '{"path":"b.txt"}'),
        makeFunctionCallOutput(T2, "call-b", "content-b"),
        makeResponseMessage(T3, "assistant", "Done."),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      const step = turns[0].steps[0];
      expect(step.toolCalls).toHaveLength(2);
      expect(step.toolCalls[0].callId).toBe("call-a");
      expect(step.toolCalls[1].callId).toBe("call-b");
    });

    it("handles custom_tool_call type", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "custom"),
        {
          timestamp: T1,
          type: "response_item",
          payload: {
            type: "custom_tool_call",
            call_id: "ct-1",
            name: "my_tool",
            input: '{"key":"val"}',
          },
        } as RolloutLine,
        {
          timestamp: T2,
          type: "response_item",
          payload: {
            type: "custom_tool_call_output",
            call_id: "ct-1",
            output: "custom output",
          },
        } as RolloutLine,
        makeResponseMessage(T3, "assistant", "Done"),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      const tc = turns[0].steps[0].toolCalls[0];
      expect(tc.callId).toBe("ct-1");
      expect(tc.name).toBe("my_tool");
      expect(tc.args).toEqual({ key: "val" });
      expect(tc.output).toBe("custom output");
    });
  });

  describe("multi-step", () => {
    it("creates multiple steps separated by token_count events", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "multi-step"),
        makeResponseMessage(T1, "assistant", "Step 1 output"),
        makeTokenCount(T1, { input_tokens: 10, output_tokens: 5 }),
        makeResponseMessage(T2, "assistant", "Step 2 output"),
        makeTokenCount(T2, { input_tokens: 15, output_tokens: 8 }),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      expect(turns[0].steps).toHaveLength(2);
      expect(turns[0].steps[0].text).toBe("Step 1 output");
      expect(turns[0].steps[0].usage).toEqual({ input_tokens: 10, output_tokens: 5 });
      expect(turns[0].steps[1].text).toBe("Step 2 output");
      expect(turns[0].steps[1].usage).toEqual({ input_tokens: 15, output_tokens: 8 });
    });
  });

  describe("token usage", () => {
    it("captures totalUsage from token_count event", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "hello"),
        makeResponseMessage(T1, "assistant", "hi"),
        makeTokenCount(T1, { input_tokens: 5, output_tokens: 3 }, { input_tokens: 50, output_tokens: 30 }),
        makeTaskComplete(T2),
      ];
      const { turns } = parseSession(lines);
      expect(turns[0].totalUsage).toEqual({ input_tokens: 50, output_tokens: 30 });
    });
  });

  describe("multi-turn", () => {
    it("parses multiple turns from consecutive task_started/task_complete pairs", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "turn-1"),
        makeUserMessage(T0, "first"),
        makeResponseMessage(T1, "assistant", "reply 1"),
        makeTaskComplete(T1),
        makeTaskStarted(T2, "turn-2"),
        makeUserMessage(T2, "second"),
        makeResponseMessage(T3, "assistant", "reply 2"),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      expect(turns).toHaveLength(2);
      expect(turns[0].turnId).toBe("turn-1");
      expect(turns[0].userInput).toBe("first");
      expect(turns[1].turnId).toBe("turn-2");
      expect(turns[1].userInput).toBe("second");
    });
  });

  describe("aborted turn", () => {
    it("marks turn as aborted on turn_aborted event", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "do something"),
        makeResponseMessage(T1, "assistant", "partial"),
        {
          timestamp: T2,
          type: "event_msg",
          payload: { type: "turn_aborted" },
        } as RolloutLine,
      ];
      const { turns } = parseSession(lines);
      expect(turns[0].aborted).toBe(true);
      expect(turns[0].completed).toBe(true);
    });
  });

  describe("subagent spawn", () => {
    it("records subagent thread IDs from collab_agent_spawn_end", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "spawn"),
        {
          timestamp: T1,
          type: "event_msg",
          payload: { type: "collab_agent_spawn_end", new_thread_id: "sub-thread-1" },
        } as RolloutLine,
        makeResponseMessage(T2, "assistant", "done"),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      expect(turns[0].subagentThreadIds).toEqual(["sub-thread-1"]);
    });
  });

  describe("reasoning", () => {
    it("captures reasoning from response_item type=reasoning", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "think"),
        {
          timestamp: T1,
          type: "response_item",
          payload: {
            type: "reasoning",
            content: [{ text: "Let me think about this..." }],
          },
        } as RolloutLine,
        makeResponseMessage(T2, "assistant", "answer"),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      expect(turns[0].steps[0].reasoning).toBe("Let me think about this...");
    });
  });

  describe("incomplete turn", () => {
    it("auto-finishes turn at end of lines if not explicitly completed", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "hello"),
        makeResponseMessage(T1, "assistant", "partial response"),
      ];
      const { turns } = parseSession(lines);
      expect(turns).toHaveLength(1);
      expect(turns[0].completed).toBe(false);
      expect(turns[0].aborted).toBe(false);
    });
  });

  describe("tool_end event", () => {
    it("sets error on tool call when event_msg *_end has status=failed", () => {
      const lines: RolloutLine[] = [
        makeTaskStarted(T0, "t1"),
        makeUserMessage(T0, "run bad"),
        makeFunctionCall(T1, "call-err", "shell", '{"cmd":"bad"}'),
        {
          timestamp: T2,
          type: "event_msg",
          payload: {
            type: "shell_end",
            call_id: "call-err",
            status: "failed",
            stderr: "command not found",
          },
        } as RolloutLine,
        makeResponseMessage(T3, "assistant", "Failed."),
        makeTaskComplete(T3),
      ];
      const { turns } = parseSession(lines);
      const tc = turns[0].steps[0].toolCalls[0];
      expect(tc.error).toBe("command not found");
      expect(tc.endTime).toBe(Date.parse(T2));
    });
  });
});

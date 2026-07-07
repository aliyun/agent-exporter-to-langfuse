import { describe, expect, it } from "vitest";
import type { StateRecord } from "../src/types.js";
import { splitTurns } from "../src/turns.js";

function makeRecord(event: string, overrides?: Partial<StateRecord>): StateRecord {
  return {
    hook_event_name: event,
    generation_id: "g1",
    timestamp: "2026-01-01T00:00:00.000Z",
    conversation_id: "conv-1",
    ...overrides,
  };
}

describe("splitTurns", () => {
  describe("beforeSubmitPrompt boundary", () => {
    it("splits 2-turn session by beforeSubmitPrompt boundary", () => {
      const records: StateRecord[] = [
        makeRecord("beforeSubmitPrompt", { generation_id: "g1", timestamp: "2026-01-01T00:00:00.000Z", prompt: "prompt1", model: "gpt-4" }),
        makeRecord("afterAgentResponse", { generation_id: "g1", timestamp: "2026-01-01T00:01:00.000Z", text: "response1", model: "gpt-4" }),
        makeRecord("beforeSubmitPrompt", { generation_id: "g2", timestamp: "2026-01-01T00:02:00.000Z", prompt: "prompt2", model: "gpt-4" }),
        makeRecord("afterAgentResponse", { generation_id: "g2", timestamp: "2026-01-01T00:03:00.000Z", text: "response2", model: "gpt-4" }),
      ];

      const turns = splitTurns(records);
      expect(turns, "should split into 2 turns").toHaveLength(2);
      expect(turns[0].userInput, "turn 1 input should be prompt1").toBe("prompt1");
      expect(turns[0].finalOutput, "turn 1 output should be response1").toBe("response1");
      expect(turns[1].userInput, "turn 2 input should be prompt2").toBe("prompt2");
      expect(turns[1].finalOutput, "turn 2 output should be response2").toBe("response2");
    });

    it("sets startTime and endTime from event timestamps", () => {
      const records: StateRecord[] = [
        makeRecord("beforeSubmitPrompt", { timestamp: "2026-01-01T00:00:00.000Z" }),
        makeRecord("afterAgentResponse", { timestamp: "2026-01-01T00:05:00.000Z" }),
      ];

      const turns = splitTurns(records);
      expect(turns[0].startTime, "startTime should be first event timestamp").toBe("2026-01-01T00:00:00.000Z");
      expect(turns[0].endTime, "endTime should be last event timestamp").toBe("2026-01-01T00:05:00.000Z");
    });

    it("sets conversationId from records", () => {
      const records = [makeRecord("beforeSubmitPrompt", { conversation_id: "conv-abc" })];
      const turns = splitTurns(records);
      expect(turns[0].conversationId, "conversationId should come from records").toBe("conv-abc");
    });

    it("sets model from beforeSubmitPrompt or afterAgentResponse", () => {
      const records = [
        makeRecord("beforeSubmitPrompt", { model: "claude-4" }),
        makeRecord("afterAgentResponse", { model: "claude-4" }),
      ];
      const turns = splitTurns(records);
      expect(turns[0].model, "model should come from event payload").toBe("claude-4");
    });
  });

  describe("generation_id degradation", () => {
    it("splits by generation_id when no beforeSubmitPrompt", () => {
      const records: StateRecord[] = [
        makeRecord("afterAgentThought", { generation_id: "g1", timestamp: "2026-01-01T00:00:00.000Z" }),
        makeRecord("afterAgentResponse", { generation_id: "g1", timestamp: "2026-01-01T00:01:00.000Z", text: "r1" }),
        makeRecord("afterAgentThought", { generation_id: "g2", timestamp: "2026-01-01T00:02:00.000Z" }),
        makeRecord("afterAgentResponse", { generation_id: "g2", timestamp: "2026-01-01T00:03:00.000Z", text: "r2" }),
      ];

      const turns = splitTurns(records);
      expect(turns, "should split into 2 turns by generation_id").toHaveLength(2);
    });

    it("degrades to single turn when no beforeSubmitPrompt and no generation_id", () => {
      const records: StateRecord[] = [
        makeRecord("afterAgentThought", { generation_id: undefined, timestamp: "2026-01-01T00:00:00.000Z" }),
        makeRecord("afterAgentResponse", { generation_id: undefined, timestamp: "2026-01-01T00:01:00.000Z" }),
      ];

      const turns = splitTurns(records);
      expect(turns, "should be single turn when no boundary").toHaveLength(1);
    });
  });

  it("returns empty array for empty records", () => {
    expect(splitTurns([]), "empty records should produce no turns").toEqual([]);
  });

  it("handles stop event in records (not a boundary)", () => {
    const records: StateRecord[] = [
      makeRecord("beforeSubmitPrompt", { timestamp: "2026-01-01T00:00:00.000Z" }),
      makeRecord("afterAgentResponse", { timestamp: "2026-01-01T00:01:00.000Z" }),
      makeRecord("stop", { timestamp: "2026-01-01T00:02:00.000Z", status: "completed" }),
    ];

    const turns = splitTurns(records);
    expect(turns, "stop should not create a new turn boundary").toHaveLength(1);
    expect(turns[0].cursorStatus, "cursorStatus should come from stop event").toBe("completed");
  });
});

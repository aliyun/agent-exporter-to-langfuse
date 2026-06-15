import { describe, expect, it, vi, beforeEach } from "vitest";

vi.mock("node:fs/promises", () => ({
  readFile: vi.fn(),
  appendFile: vi.fn(),
}));

import { loadUploadedTurnIds, markTurnUploaded } from "../src/sidecar.js";
import * as fs from "node:fs/promises";

const mockedReadFile = vi.mocked(fs.readFile);
const mockedAppendFile = vi.mocked(fs.appendFile);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("loadUploadedTurnIds", () => {
  it("reads .langfuse file and returns Set of turn IDs", async () => {
    mockedReadFile.mockResolvedValue("turn-1\nturn-2\nturn-3\n");

    const result = await loadUploadedTurnIds("/path/to/rollout.jsonl");

    expect(mockedReadFile).toHaveBeenCalledWith("/path/to/rollout.jsonl.langfuse", "utf-8");
    expect(result).toEqual(new Set(["turn-1", "turn-2", "turn-3"]));
  });

  it("filters out empty lines", async () => {
    mockedReadFile.mockResolvedValue("turn-1\n\nturn-2\n\n");

    const result = await loadUploadedTurnIds("/path/to/rollout.jsonl");
    expect(result).toEqual(new Set(["turn-1", "turn-2"]));
  });

  it("returns empty Set when file does not exist (ENOENT)", async () => {
    const err = new Error("ENOENT") as NodeJS.ErrnoException;
    err.code = "ENOENT";
    mockedReadFile.mockRejectedValue(err);

    const result = await loadUploadedTurnIds("/path/to/rollout.jsonl");
    expect(result).toEqual(new Set());
  });

  it("re-throws non-ENOENT errors", async () => {
    const err = new Error("EACCES") as NodeJS.ErrnoException;
    err.code = "EACCES";
    mockedReadFile.mockRejectedValue(err);

    await expect(loadUploadedTurnIds("/path/to/rollout.jsonl")).rejects.toThrow("EACCES");
  });
});

describe("markTurnUploaded", () => {
  it("appends turn ID with newline to .langfuse file", async () => {
    mockedAppendFile.mockResolvedValue(undefined);

    await markTurnUploaded("/path/to/rollout.jsonl", "turn-42");

    expect(mockedAppendFile).toHaveBeenCalledWith(
      "/path/to/rollout.jsonl.langfuse",
      "turn-42\n",
      "utf-8",
    );
  });

  it("does not throw when appendFile fails (best-effort)", async () => {
    mockedAppendFile.mockRejectedValue(new Error("disk full"));

    // Should not throw
    await markTurnUploaded("/path/to/rollout.jsonl", "turn-99");
  });
});

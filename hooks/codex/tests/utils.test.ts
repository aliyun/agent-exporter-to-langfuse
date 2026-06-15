import { describe, expect, it } from "vitest";
import { truncate, toText, isPrimitive } from "../src/utils.js";

describe("truncate", () => {
  it("returns text as-is when within limit", () => {
    const result = truncate("hello", 10);
    expect(result).toEqual({ text: "hello" });
    expect(result.meta).toBeUndefined();
  });

  it("returns text as-is when exactly at limit", () => {
    const result = truncate("12345", 5);
    expect(result).toEqual({ text: "12345" });
    expect(result.meta).toBeUndefined();
  });

  it("truncates and adds meta when over limit", () => {
    const result = truncate("hello world", 5);
    expect(result.text).toBe("hello");
    expect(result.meta).toEqual({ truncated: true, originalLength: 11 });
  });

  it("handles empty string", () => {
    const result = truncate("", 10);
    expect(result).toEqual({ text: "" });
    expect(result.meta).toBeUndefined();
  });
});

describe("toText", () => {
  it("passes through strings", () => {
    expect(toText("hello")).toBe("hello");
  });

  it("converts numbers to string", () => {
    expect(toText(42)).toBe("42");
    expect(toText(0)).toBe("0");
    expect(toText(-3.14)).toBe("-3.14");
  });

  it("converts booleans to string", () => {
    expect(toText(true)).toBe("true");
    expect(toText(false)).toBe("false");
  });

  it("converts objects to JSON", () => {
    expect(toText({ a: 1 })).toBe('{"a":1}');
    expect(toText([1, 2])).toBe("[1,2]");
  });

  it("returns empty string for null and undefined", () => {
    expect(toText(null)).toBe("");
    expect(toText(undefined)).toBe("");
  });
});

describe("isPrimitive", () => {
  it("returns true for string", () => {
    expect(isPrimitive("hello")).toBe(true);
    expect(isPrimitive("")).toBe(true);
  });

  it("returns true for number", () => {
    expect(isPrimitive(42)).toBe(true);
    expect(isPrimitive(0)).toBe(true);
    expect(isPrimitive(NaN)).toBe(true);
  });

  it("returns true for boolean", () => {
    expect(isPrimitive(true)).toBe(true);
    expect(isPrimitive(false)).toBe(true);
  });

  it("returns false for object", () => {
    expect(isPrimitive({})).toBe(false);
    expect(isPrimitive([])).toBe(false);
  });

  it("returns false for null and undefined", () => {
    expect(isPrimitive(null)).toBe(false);
    expect(isPrimitive(undefined)).toBe(false);
  });
});

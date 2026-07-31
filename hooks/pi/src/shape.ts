/**
 * Payload shaping ported from pi-langfuse src/utils.ts: string truncation,
 * depth/array/key/node limits, JSON-string normalization, circular safety.
 * Everything written into span attributes must pass through shapePayload().
 */
import { getLimits } from "./limits.ts";
import { redactValue } from "./redaction.ts";

export function truncate(value: string, maxLength = getLimits().maxString): string {
  return value.length > maxLength ? `${value.slice(0, maxLength)}... [truncated]` : value;
}

export function tryParseJson(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed || !["{", "["].includes(trimmed[0])) {
    return value;
  }

  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

const PAYLOAD_TOO_LARGE = "[payload too large]";

export function shapePayload(
  value: unknown,
  options: {
    maxString?: number;
    depth?: number;
    maxNodes?: number;
    maxArrayItems?: number;
    maxObjectKeys?: number;
    redact?: boolean;
    parseJson?: boolean;
  } = {},
): unknown {
  const limits = getLimits();
  const maxString = options.maxString ?? limits.maxString;
  const depth = options.depth ?? limits.maxDepth;
  const maxNodes = options.maxNodes ?? limits.maxNodes;
  const maxArrayItems = options.maxArrayItems ?? limits.maxArrayItems;
  const maxObjectKeys = options.maxObjectKeys ?? limits.maxObjectKeys;
  const budget = { exhausted: false, nodeCount: 0 };

  function visit(item: unknown, remainingDepth: number, seen: WeakSet<object>): unknown {
    if (budget.exhausted) {
      return PAYLOAD_TOO_LARGE;
    }

    budget.nodeCount++;
    if (budget.nodeCount > maxNodes) {
      budget.exhausted = true;
      return PAYLOAD_TOO_LARGE;
    }

    if (typeof item === "string") {
      const truncated = truncate(item, maxString);
      if (options.parseJson === false) {
        return truncated;
      }
      const parsed = tryParseJson(truncated);
      if (parsed === truncated) {
        return truncated;
      }
      return visit(parsed, remainingDepth - 1, seen);
    }

    if (
      item === null ||
      typeof item === "undefined" ||
      typeof item === "number" ||
      typeof item === "boolean"
    ) {
      return item;
    }

    if (typeof item === "bigint") {
      return item.toString();
    }

    if (typeof item === "function" || typeof item === "symbol") {
      return `[${typeof item}]`;
    }

    if (remainingDepth <= 0) {
      return `[max depth ${depth} reached]`;
    }

    if (Array.isArray(item)) {
      const output: unknown[] = [];
      const limit = Math.min(item.length, maxArrayItems);
      for (let index = 0; index < limit; index++) {
        output.push(visit(item[index], remainingDepth - 1, seen));
        if (budget.exhausted) {
          break;
        }
      }
      return output;
    }

    if (item instanceof Error) {
      return {
        name: item.name,
        message: item.message,
        stack: item.stack ? truncate(item.stack, maxString) : undefined,
      };
    }

    if (typeof item === "object") {
      if (seen.has(item)) {
        return "[circular]";
      }
      seen.add(item);

      const output: Record<string, unknown> = {};
      let keyCount = 0;
      for (const key in item as Record<string, unknown>) {
        if (!Object.hasOwn(item, key)) {
          continue;
        }
        output[key] = visit((item as Record<string, unknown>)[key], remainingDepth - 1, seen);
        keyCount++;
        if (budget.exhausted || keyCount >= maxObjectKeys) {
          break;
        }
      }
      return output;
    }

    return String(item);
  }

  const shaped = visit(value, depth, new WeakSet<object>());
  return options.redact === false
    ? shaped
    : redactValue(shaped, {
        maxDepth: depth,
        maxStringLength: maxString,
        maxArrayItems,
        maxObjectKeys,
      });
}

export function safeSerialize(value: unknown, maxLength = getLimits().maxToolPayload): string {
  try {
    return truncate(JSON.stringify(shapePayload(value, { maxString: maxLength }), null, 2), maxLength);
  } catch {
    return `[unserializable ${typeof value}]`;
  }
}

/**
 * Payload-shaping limits, ported from pi-langfuse src/limits.ts +
 * src/constants.ts. Defaults keep pi-langfuse's current values; each
 * PI_LANGFUSE_MAX_* env var (delivered via pi.env) overrides the matching
 * limit, and 0/"off"/"unlimited" removes it.
 */

export const MAX_STRING_LENGTH = 12_000;
export const MAX_TOOL_PAYLOAD_LENGTH = 24_000;
export const MAX_DEPTH = 6;
export const MAX_ARRAY_ITEMS = 50;
export const MAX_OBJECT_KEYS = 80;
export const MAX_PAYLOAD_NODES = 2_000;

export interface PayloadLimits {
  readonly maxString: number;
  readonly maxToolPayload: number;
  readonly maxDepth: number;
  readonly maxArrayItems: number;
  readonly maxObjectKeys: number;
  readonly maxNodes: number;
}

export const DEFAULT_LIMITS: PayloadLimits = {
  maxString: MAX_STRING_LENGTH,
  maxToolPayload: MAX_TOOL_PAYLOAD_LENGTH,
  maxDepth: MAX_DEPTH,
  maxArrayItems: MAX_ARRAY_ITEMS,
  maxObjectKeys: MAX_OBJECT_KEYS,
  maxNodes: MAX_PAYLOAD_NODES,
};

/** Words that mean "no limit" when supplied as an env value. */
const UNLIMITED_WORDS = new Set(["off", "none", "false", "no", "unlimited", "inf", "infinity"]);

export function parseLimit(raw: string | undefined, fallback: number): number {
  if (raw === undefined) {
    return fallback;
  }
  const trimmed = raw.trim().toLowerCase();
  if (trimmed === "") {
    return fallback;
  }
  if (UNLIMITED_WORDS.has(trimmed)) {
    return Number.POSITIVE_INFINITY;
  }
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  if (parsed <= 0) {
    return Number.POSITIVE_INFINITY;
  }
  return Math.floor(parsed);
}

export function createPayloadLimits(env: NodeJS.ProcessEnv = process.env): PayloadLimits {
  return {
    maxString: parseLimit(env.PI_LANGFUSE_MAX_STRING_LENGTH, DEFAULT_LIMITS.maxString),
    maxToolPayload: parseLimit(env.PI_LANGFUSE_MAX_TOOL_PAYLOAD_LENGTH, DEFAULT_LIMITS.maxToolPayload),
    maxDepth: parseLimit(env.PI_LANGFUSE_MAX_DEPTH, DEFAULT_LIMITS.maxDepth),
    maxArrayItems: parseLimit(env.PI_LANGFUSE_MAX_ARRAY_ITEMS, DEFAULT_LIMITS.maxArrayItems),
    maxObjectKeys: parseLimit(env.PI_LANGFUSE_MAX_OBJECT_KEYS, DEFAULT_LIMITS.maxObjectKeys),
    maxNodes: parseLimit(env.PI_LANGFUSE_MAX_PAYLOAD_NODES, DEFAULT_LIMITS.maxNodes),
  };
}

/** Resolve limits fresh from the environment so a pi.env change governs truncation everywhere. */
export function getLimits(): PayloadLimits {
  return createPayloadLimits();
}

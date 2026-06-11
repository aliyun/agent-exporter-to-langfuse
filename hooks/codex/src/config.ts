import { z } from "zod";

export const ConfigSchema = z.object({
  enabled: z.boolean(),
  public_key: z.string().optional(),
  secret_key: z.string().optional(),
  base_url: z.string(),
  user_id: z.string().optional(),
  tags: z.array(z.string()).optional(),
  max_chars: z.number().int().positive(),
  debug: z.boolean(),
  fail_on_error: z.boolean(),
  langstash_enabled: z.boolean(),
  langstash_url: z.string(),
  langstash_timeout: z.number().int().positive(),
});

export type Config = z.infer<typeof ConfigSchema>;

const DEFAULTS: Config = {
  enabled: true,
  base_url: "https://us.cloud.langfuse.com",
  max_chars: 800_000,
  debug: true,
  fail_on_error: false,
  langstash_enabled: false,
  langstash_url: "http://127.0.0.1:5288",
  langstash_timeout: 10,
};

function parseBoolean(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value !== "string") return fallback;
  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "off"].includes(normalized)) return false;
  return fallback;
}

function parseTags(value: unknown): string[] | undefined {
  if (typeof value !== "string" || value.trim().length === 0) return undefined;
  const trimmed = value.trim();
  if (trimmed.startsWith("[")) {
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch {
      // fall through
    }
  }
  return trimmed
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

function parseInteger(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return fallback;
  const parsed = Number.parseInt(value.trim(), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function resolveUserId(env: Record<string, string | undefined>): string | undefined {
  const explicit = env.LANGFUSE_USER_ID;
  if (explicit) return explicit;
  for (const k of ["USER", "LOGNAME", "USERNAME"]) {
    const v = env[k];
    if (v) return v;
  }
  return undefined;
}

export function getConfig(env: Record<string, string | undefined> = process.env): Config {
  return ConfigSchema.parse({
    enabled: parseBoolean(env.TRACE_TO_LANGFUSE ?? "true", DEFAULTS.enabled),
    public_key: env.LANGFUSE_PUBLIC_KEY,
    secret_key: env.LANGFUSE_SECRET_KEY,
    base_url: env.LANGFUSE_BASE_URL || DEFAULTS.base_url,
    user_id: resolveUserId(env),
    tags: parseTags(env.LANGFUSE_TAGS) ?? ["codex"],
    max_chars: parseInteger(env.LANGFUSE_MAX_CHARS, DEFAULTS.max_chars),
    debug: parseBoolean(env.LANGFUSE_DEBUG, DEFAULTS.debug),
    fail_on_error: parseBoolean(env.LANGFUSE_CODEX_FAIL_ON_ERROR, DEFAULTS.fail_on_error),
    langstash_enabled: parseBoolean(env.LANGSTASH_ENABLED, DEFAULTS.langstash_enabled),
    langstash_url: env.LANGSTASH_URL || DEFAULTS.langstash_url,
    langstash_timeout: parseInteger(env.LANGSTASH_TIMEOUT, DEFAULTS.langstash_timeout),
  });
}

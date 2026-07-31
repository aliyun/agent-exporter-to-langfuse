import { readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

/**
 * Runtime configuration entry point (R-6): the single config source is
 * ~/.agent-exporter-to-langfuse/config/pi.env written by install.sh.
 * Lines follow the repo-wide `export KEY="value"` format. Existing
 * process.env values always win; a missing file degrades silently.
 * Resolved per call so tests and the hook always follow the current HOME.
 */
export function piEnvPath(): string {
  return join(homedir(), ".agent-exporter-to-langfuse", "config", "pi.env");
}

const ENV_LINE_RE = /^export\s+([A-Za-z_][A-Za-z0-9_]*)="(.*)"\s*$/;

export function loadPiEnv(
  envFilePath: string = piEnvPath(),
  env: NodeJS.ProcessEnv = process.env,
): void {
  let content: string;
  try {
    content = readFileSync(envFilePath, "utf8");
  } catch {
    return;
  }
  for (const line of content.split("\n")) {
    const m = line.match(ENV_LINE_RE);
    if (m && env[m[1]] === undefined) {
      env[m[1]] = m[2];
    }
  }
}

/** Tags for the trace: LANGFUSE_TAGS (comma separated) plus the fixed `pi` tag. */
export function resolveTags(env: NodeJS.ProcessEnv = process.env): string[] {
  const tags = (env.LANGFUSE_TAGS || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  if (!tags.includes("pi")) {
    tags.push("pi");
  }
  return tags;
}

export function resolveUserId(env: NodeJS.ProcessEnv = process.env): string {
  if (env.LANGFUSE_USER_ID) {
    return env.LANGFUSE_USER_ID;
  }
  try {
    return env.USER || env.LOGNAME || env.USERNAME || "unknown";
  } catch {
    return "unknown";
  }
}

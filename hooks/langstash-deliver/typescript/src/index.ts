import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

const INSTALL_DIR = join(homedir(), ".agent-exporter-to-langfuse");
const FAILED_DIR = join(INSTALL_DIR, "data", "failed");

export type FetchFn = (
  url: string,
  options: { method: string; headers: Record<string, string>; body: string },
) => Promise<{ ok: boolean; status: number }>;

export interface DeliverOptions {
  fetchFn?: FetchFn;
}

function langstashEnabled(): boolean {
  return (process.env.LANGSTASH_ENABLED || "").toLowerCase() === "true";
}

function langstashUrl(): string {
  return process.env.LANGSTASH_URL || "http://127.0.0.1:5288";
}

function langstashTimeout(): number {
  const v = parseInt(process.env.LANGSTASH_TIMEOUT || "10", 10);
  return Number.isFinite(v) ? v : 10;
}

async function postLangstash(
  otlpJson: Record<string, unknown>,
  doFetch: FetchFn,
): Promise<boolean> {
  const url = `${langstashUrl().replace(/\/+$/, "")}/ingest`;
  const body = JSON.stringify(otlpJson);
  const controller =
    typeof AbortController !== "undefined" ? new AbortController() : null;
  const timeout = controller
    ? setTimeout(() => controller.abort(), langstashTimeout() * 1000)
    : null;

  try {
    const resp = await doFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    return resp.ok;
  } catch {
    return false;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

async function postLangfuseOtel(
  otlpJson: Record<string, unknown>,
  doFetch: FetchFn,
): Promise<boolean> {
  const baseUrl = (process.env.LANGFUSE_BASE_URL || "").replace(/\/+$/, "");
  const publicKey = process.env.LANGFUSE_PUBLIC_KEY || "";
  const secretKey = process.env.LANGFUSE_SECRET_KEY || "";

  if (!baseUrl || !publicKey || !secretKey) {
    return false;
  }

  const url = `${baseUrl}/api/public/otel/v1/traces`;
  const body = JSON.stringify(otlpJson);
  const credentials = Buffer.from(`${publicKey}:${secretKey}`).toString(
    "base64",
  );

  try {
    const resp = await doFetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Basic ${credentials}`,
      },
      body,
    });
    return resp.ok;
  } catch {
    return false;
  }
}

function appendFailedTrace(otlpJson: Record<string, unknown>): void {
  try {
    mkdirSync(FAILED_DIR, { recursive: true });
    const today = new Date().toISOString().slice(0, 10);
    const line = JSON.stringify(otlpJson) + "\n";
    appendFileSync(join(FAILED_DIR, `${today}.jsonl`), line, { flag: "a" });
  } catch {
    // best-effort
  }
}

const defaultFetch: FetchFn = async (url, options) => {
  const resp = await fetch(url, {
    method: options.method,
    headers: options.headers,
    body: options.body,
  });
  return { ok: resp.ok, status: resp.status };
};

export async function deliverTrace(
  otlpJson: Record<string, unknown>,
  options?: DeliverOptions,
): Promise<boolean> {
  const doFetch = options?.fetchFn || defaultFetch;

  if (langstashEnabled()) {
    if (await postLangstash(otlpJson, doFetch)) {
      return true;
    }
  }

  if (await postLangfuseOtel(otlpJson, doFetch)) {
    return true;
  }

  appendFailedTrace(otlpJson);
  return false;
}

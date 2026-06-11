import { getConfig } from "./config.js";
import { setupInstrumentation } from "./instrumentation.js";
import { convertRollout } from "./trace.js";
import type { HookInput } from "./types.js";
import { debugLog, error, info, loadEnvFile, readStdin, setDebug } from "./utils.js";

let failOnError = process.env.LANGFUSE_CODEX_FAIL_ON_ERROR === "true";

export async function runHook(): Promise<void> {
  loadEnvFile();

  let hookInput: HookInput;
  try {
    hookInput = await readStdin<HookInput>();
  } catch (e) {
    return;
  }

  const config = getConfig();
  setDebug(config.debug);
  failOnError = config.fail_on_error;

  if (!config.enabled) {
    info("tracing disabled (set TRACE_TO_LANGFUSE=true to enable)");
    return;
  }
  if (!config.public_key || !config.secret_key) {
    info("missing LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY; skipping");
    return;
  }
  if (!hookInput.transcript_path) {
    info("hook payload missing transcript_path; skipping");
    return;
  }

  const instrumentation = setupInstrumentation(config);
  try {
    await convertRollout(hookInput.transcript_path, { config });
  } catch (e) {
    error("failed to convert rollout:", e);
    if (config.fail_on_error) throw e;
  } finally {
    try {
      await instrumentation.shutdown();
    } catch (e) {
      error("error during flush/shutdown:", e);
      if (config.fail_on_error) throw e;
    }
  }
}

runHook().catch((e) => {
  error("fatal:", e);
  if (failOnError) {
    process.exitCode = 1;
  }
});

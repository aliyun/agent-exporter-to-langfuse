import { getConfig } from "./config.js";
import { setupInstrumentation } from "./instrumentation.js";
import { convertRollout } from "./trace.js";
import type { HookInput } from "./types.js";
import { debugLog, loadEnvFile, readStdin, setDebug } from "./utils.js";

let failOnError = process.env.LANGFUSE_CODEX_FAIL_ON_ERROR === "true";

export async function runHook(): Promise<void> {
  loadEnvFile();

  let hookInput: HookInput;
  try {
    hookInput = await readStdin<HookInput>();
  } catch (error) {
    return;
  }

  const config = getConfig();
  setDebug(config.debug);
  failOnError = config.fail_on_error;

  if (!config.enabled) {
    debugLog("tracing disabled (set TRACE_TO_LANGFUSE=true to enable)");
    return;
  }
  if (!config.public_key || !config.secret_key) {
    debugLog("missing LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY; skipping");
    return;
  }
  if (!hookInput.transcript_path) {
    debugLog("hook payload missing transcript_path; skipping");
    return;
  }

  const instrumentation = setupInstrumentation(config);
  try {
    await convertRollout(hookInput.transcript_path, { config });
  } catch (error) {
    debugLog("failed to convert rollout:", error);
    if (config.fail_on_error) throw error;
  } finally {
    try {
      await instrumentation.shutdown();
    } catch (error) {
      debugLog("error during flush/shutdown:", error);
      if (config.fail_on_error) throw error;
    }
  }
}

runHook().catch((error) => {
  if (process.env.LANGFUSE_CODEX_DEBUG === "true" || process.env.LANGFUSE_DEBUG === "true") {
    // eslint-disable-next-line no-console
    console.error("[langfuse-codex] fatal:", error);
  }
  if (failOnError) {
    process.exitCode = 1;
  }
});

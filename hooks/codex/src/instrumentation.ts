import { LangfuseSpanProcessor } from "@langfuse/otel";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

import type { Config } from "./config.js";

export type Instrumentation = {
  shutdown: () => Promise<void>;
};

export function setupInstrumentation(config: Config): Instrumentation {
  const spanProcessor = new LangfuseSpanProcessor({
    publicKey: config.public_key,
    secretKey: config.secret_key,
    baseUrl: config.base_url,
    exportMode: "batched",
    shouldExportSpan: () => true,
  });

  const provider = new NodeTracerProvider({
    spanProcessors: [spanProcessor],
  });
  provider.register();

  return {
    shutdown: async () => {
      await spanProcessor.forceFlush();
      await provider.shutdown();
    },
  };
}

import { defineConfig } from "tsdown";

export default defineConfig({
  entry: ["src/index.ts"],
  outDir: "dist",
  format: ["esm"],
  platform: "node",
  target: "node22",
  noExternal: [/^@opentelemetry\//, /^zod$/, /^zod\//],
  dts: false,
  clean: true,
  minify: false,
  outputOptions: {
    inlineDynamicImports: true,
  },
});

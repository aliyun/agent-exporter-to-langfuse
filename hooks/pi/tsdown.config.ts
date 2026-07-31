import { defineConfig } from "tsdown";

export default defineConfig({
  entry: ["index.ts"],
  outDir: "dist",
  format: ["esm"],
  platform: "node",
  target: "node22",
  dts: false,
  clean: true,
  minify: false,
  outputOptions: {
    inlineDynamicImports: true,
  },
});

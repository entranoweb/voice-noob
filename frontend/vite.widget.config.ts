import { defineConfig } from "vite";
import { resolve } from "path";

export default defineConfig({
  publicDir: false, // Disable copying public directory for library builds
  build: {
    lib: {
      entry: resolve(__dirname, "src/widget/widget.ts"),
      name: "VoiceAgentWidget",
      fileName: () => "widget.js",
      formats: ["iife"],
    },
    outDir: "public/widget/v1",
    emptyOutDir: true,
    minify: "terser",
    sourcemap: true,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
});

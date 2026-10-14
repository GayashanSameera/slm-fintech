// Vite build settings for the console.
// base "./" makes every asset path relative, so the built site works from any
// S3/CloudFront path. The output (dist/) is uploaded by scripts/deploy_web.py.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", sourcemap: false },
});

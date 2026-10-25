/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const api = process.env.AWM_API_URL ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5173,
    // The repo's docs/ folder is bundled into the in-app documentation.
    fs: { allow: [".", "../docs"] },
    proxy: {
      "/v1": { target: api, changeOrigin: false },
      "/scim": { target: api, changeOrigin: false },
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 2000 },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});

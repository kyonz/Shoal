/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const api = process.env.SHOAL_API_URL ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5173,
    // The repo's docs/ folder is bundled into the in-app documentation.
    fs: { allow: [".", "../docs"] },
    proxy: {
      "/v1": {
        target: api,
        changeOrigin: false,
        // Node holds response headers until the first body byte. A resumed event
        // stream with nothing new would then look stuck, so send them right away.
        configure: (proxy) =>
          proxy.on("proxyRes", (proxyRes, _req, res) => {
            if (String(proxyRes.headers["content-type"] ?? "").startsWith("text/event-stream")) res.flushHeaders();
          }),
      },
      "/scim": { target: api, changeOrigin: false },
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 2000 },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});

import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 构建产物输出到 webapp/dist —— server.py 检测到 dist/index.html 即优先服务新前端。
export default defineConfig({
  plugins: [react()],
  base: "/",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8317",
    },
  },
});

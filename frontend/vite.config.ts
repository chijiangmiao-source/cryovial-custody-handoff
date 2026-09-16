import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 开发态把 /api 代理到后端容器；前端端口由 Docker 通过 PORT 注入（compose 里映射 WEB_PORT）
const apiTarget = process.env.VITE_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: Number(process.env.PORT ?? 5173),
    strictPort: true,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: Number(process.env.PORT ?? 5173),
    strictPort: true,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: false,
    // e2e 由 Playwright 独立运行，不纳入 Vitest
    include: ["src/**/*.test.{ts,tsx}"],
  },
});

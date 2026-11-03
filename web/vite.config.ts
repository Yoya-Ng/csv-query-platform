import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 開發時把 /api 代理到本機後端（uvicorn）；正式環境由 FastAPI 提供 dist 靜態檔，同源不需代理。
const backend = process.env.CQP_BACKEND ?? "http://127.0.0.1:18765";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { "/api": { target: backend, changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 2000 },
});

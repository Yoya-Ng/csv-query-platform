import { defineConfig, devices } from "@playwright/test";

// 需先啟動後端（提供 web/dist）：見 docs/implementation.md「前端」。
const baseURL = process.env.CQP_E2E_BASE ?? "http://127.0.0.1:18765";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  outputDir: process.env.CQP_E2E_OUT ?? "test-results",
  use: { baseURL, trace: "off" },
  projects: [
    // 功能流程（會寫入資料）只在桌面 Chromium 跑一次
    { name: "flows", testMatch: /flows\.spec\.ts/, use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 900 } } },
    // 版面檢查：WebKit（Safari）＋ Chromium，桌面 1280 與手機 390，深淺色
    { name: "layout-chromium", testMatch: /layout\.spec\.ts/, use: { ...devices["Desktop Chrome"] } },
    { name: "layout-webkit", testMatch: /layout\.spec\.ts/, use: { ...devices["Desktop Safari"] } },
  ],
});

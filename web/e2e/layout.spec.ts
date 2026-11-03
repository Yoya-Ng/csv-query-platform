// 版面：每頁在 1280／390 × 淺色／深色下載入，斷言無 console 錯誤、無失敗請求、無水平捲動，並截圖供人工檢視。
import { expect, test, type Page } from "@playwright/test";

const PAGES = [
  { hash: "#/query", title: "查詢" },
  { hash: "#/import", title: "匯入" },
  { hash: "#/loads", title: "匯入紀錄" },
  { hash: "#/datasets", title: "資料集與欄位" },
];
const SIZES = [{ w: 1280, h: 900 }, { w: 390, h: 844 }];
const SCHEMES = ["light", "dark"] as const;
const shotDir = process.env.CQP_E2E_SHOTS ?? "test-results/shots";

function watch(page: Page) {
  const problems: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") problems.push(`console: ${m.text()}`); });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("requestfailed", (r) => problems.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));
  page.on("response", (r) => { if (r.status() >= 500) problems.push(`HTTP ${r.status()}: ${r.url()}`); });
  return problems;
}

async function noHorizontalScroll(page: Page) {
  const { sw, iw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
  expect(sw, `scrollWidth ${sw} > innerWidth ${iw}`).toBeLessThanOrEqual(iw);
}

for (const size of SIZES) {
  for (const scheme of SCHEMES) {
    test.describe(`${size.w}px ${scheme}`, () => {
      test.use({ viewport: { width: size.w, height: size.h }, colorScheme: scheme });
      for (const p of PAGES) {
        test(`${p.title}`, async ({ page, browserName }) => {
          const problems = watch(page);
          await page.goto(`/${p.hash}`);
          await expect(page.getByRole("heading", { level: 1 })).toContainText(p.title);
          await page.waitForLoadState("networkidle");
          if (p.hash === "#/query") {
            // 帶出建構器與結果表格，才看得到主要版面
            await page.locator("#q-from").selectOption("fact_policy");
            await expect(page.getByRole("button", { name: "預覽" })).toBeEnabled();
            await page.getByPlaceholder("搜尋欄位").fill("policy_no");
            await page.getByRole("checkbox").first().check();
            await page.getByRole("button", { name: "預覽" }).click();
            await expect(page.locator(".ag-row").first()).toBeVisible();
          }
          if (p.hash === "#/datasets") {
            await page.getByRole("listitem").filter({ hasText: "fact_policy" }).first().click();
            await expect(page.locator(".ag-row").first()).toBeVisible();
          }
          await noHorizontalScroll(page);
          // 背景色要跟著配色切換（確認 token 生效）
          const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
          expect(bg).toBe(scheme === "dark" ? "rgb(15, 18, 22)" : "rgb(246, 247, 249)");
          await page.screenshot({ path: `${shotDir}/${browserName}-${size.w}-${scheme}-${p.hash.slice(2)}.png`, fullPage: true });
          expect(problems, problems.join("\n")).toEqual([]);
        });
      }
    });
  }
}

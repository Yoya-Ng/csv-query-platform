// 功能流程（真實後端＋合成資料 fact_policy／fact_claim，各 202609、202610 兩期）。
// 每條斷言「通過的理由」：實際筆數、狀態文字、錯誤訊息，不只看頁面有無錯誤。
import { expect, test, type Page } from "@playwright/test";

// 每次執行用不同的新增主鍵，重跑時「新增 1／更新 1」仍成立
const NEW_CLAIM = `CE2E${Date.now().toString(36).toUpperCase()}`;

/** 以後端總筆數 API 取得實際筆數（測試資料會因增修測試變動，不寫死）。 */
async function countOf(page: Page, table: string, col: string): Promise<number> {
  const r = await page.request.post("/api/query/count", { data: { spec: { from: table, select: [`${table}.${col}`],
    where: [[`${table}.yyyymm`, "=", "202610"]] } } });
  expect(r.status()).toBe(200);
  return (await r.json()).count;
}

function watch(page: Page) {
  const problems: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") problems.push(`console: ${m.text()}`); });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("response", (r) => { if (r.status() >= 500) problems.push(`HTTP ${r.status()}: ${r.url()}`); });
  return problems;
}

async function pickColumn(page: Page, code: string, table: string) {
  await page.getByPlaceholder("搜尋欄位").fill(code);
  await page.locator(".available label.item").filter({ hasText: code })
    .filter({ has: page.locator(`xpath=ancestor::section[contains(., "${table}")]`) }).first()
    .locator("input[type=checkbox]").check();
}

test("查詢：預設帶最新期別、LEFT JOIN＋計算欄位、總筆數、分頁、JSON 規格", async ({ page }) => {
  const problems = watch(page);
  await page.goto("/#/query");
  await page.locator("#q-from").selectOption("fact_policy");
  // 預設帶入最新期別條件（大表必須帶期別）
  await expect(page.locator(".filter select").first()).toHaveValue("fact_policy.yyyymm");
  await expect(page.locator(".filter input").first()).toHaveValue("202610");

  await page.getByRole("combobox", { name: "加入 join 資料集" }).selectOption("fact_claim");
  // 依共同主鍵預設對應 yyyymm、policy_no
  await expect(page.locator(".join .on")).toHaveCount(2);

  await pickColumn(page, "policy_no", "fact_policy");
  await pickColumn(page, "claim_amt", "fact_claim");
  await page.getByRole("button", { name: "＋ 計算欄位" }).click();
  await page.getByLabel("運算式").fill("fact_claim.claim_amt - fact_claim.paid_amt");
  await page.getByRole("button", { name: "＋ 排序" }).click();
  await page.getByLabel("排序欄位").selectOption("fact_policy.policy_no");

  await page.getByRole("button", { name: "預覽" }).click();
  await expect(page.locator(".ag-header-cell-text").filter({ hasText: "calc_1" })).toBeVisible();
  await expect(page.getByText(/第 1–1,000 筆/)).toBeVisible();
  await page.getByRole("button", { name: "總筆數" }).click();
  // LEFT JOIN：至少保留主表 3,000 筆（一張保單可能多筆理賠）
  const txt = await page.getByText(/共 [\d,]+ 筆/).first().textContent();
  const total = Number(txt!.match(/共 ([\d,]+) 筆/)![1].replace(/,/g, ""));
  expect(total).toBeGreaterThanOrEqual(3000);
  await page.getByRole("button", { name: "下一頁" }).click();
  await expect(page.getByText(/第 1,001–2,000 筆/)).toBeVisible();

  await page.getByText("查詢規格（JSON）").click();
  const json = JSON.parse(await page.locator("pre.json").textContent() ?? "{}");
  expect(json.joins[0]).toEqual({ table: "fact_claim", type: "left",
    on: [["fact_policy.yyyymm", "fact_claim.yyyymm"], ["fact_policy.policy_no", "fact_claim.policy_no"]] });
  expect(json.where).toEqual([["fact_policy.yyyymm", "=", "202610"]]);
  expect(problems, problems.join("\n")).toEqual([]);
});

test("查詢：白名單拒絕與 IN 貼上", async ({ page }) => {
  await page.goto("/#/query");
  await page.locator("#q-from").selectOption("fact_policy");
  await pickColumn(page, "policy_no", "fact_policy");
  await page.getByRole("button", { name: "＋ 計算欄位" }).click();
  await page.getByLabel("運算式").fill("__import__('os')");
  await page.getByRole("button", { name: "預覽" }).click();
  await expect(page.locator(".notice.error")).toContainText("不允許的函式：__import__");
  // 移除計算欄位，改用 IN 條件貼上（含重複與空白）
  await page.getByLabel("移除計算欄位").click();
  await page.getByRole("button", { name: "＋ 條件" }).click();
  const f = page.locator(".filter").nth(1);
  await f.getByLabel("篩選欄位").selectOption("fact_policy.policy_no");
  await f.getByLabel("條件", { exact: true }).selectOption("in");
  await f.getByLabel("值清單").fill("P0000000001\nP0000000002\n P0000000001 \n\nP0000000003");
  await expect(f.getByText("3 個值（已去重複）")).toBeVisible();
  await page.getByRole("button", { name: "預覽" }).click();
  await expect(page.getByText(/第 1–3 筆/)).toBeVisible();
});

test("查詢：複製到剪貼簿", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/#/query");
  await page.locator("#q-from").selectOption("fact_claim");
  await pickColumn(page, "claim_no", "fact_claim");
  const n = await countOf(page, "fact_claim", "claim_no");
  await page.getByRole("button", { name: /複製/ }).click();
  await expect(page.locator(".notice.ok")).toContainText(`已複製 ${n.toLocaleString("en-US")} 筆資料`);
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  const lines = clip.replace(/\r?\n$/, "").split(/\r?\n/);
  expect(lines[0]).toBe("fact_claim__claim_no");
  expect(lines).toHaveLength(n + 1);
});

test("查詢：匯出 CSV 由瀏覽器下載", async ({ page }) => {
  await page.goto("/#/query");
  await page.locator("#q-from").selectOption("fact_claim");
  await pickColumn(page, "claim_no", "fact_claim");
  const n = await countOf(page, "fact_claim", "claim_no");
  await page.getByRole("button", { name: "匯出 CSV" }).click();
  await expect(page.getByText(`共 ${n.toLocaleString("en-US")} 筆`)).toBeVisible();
  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "開始下載" }).click()]);
  expect(dl.suggestedFilename()).toMatch(/^fact_claim_\d{8}_\d{6}\.csv$/);
  const body = await (await dl.createReadStream()).toArray();
  const text = Buffer.concat(body).toString("utf8");
  expect(text.startsWith("﻿fact_claim__claim_no")).toBe(true);
  expect(text.trim().split("\n")).toHaveLength(n + 1);
});

test("匯入：增修上傳 → 完成並顯示新增／更新筆數", async ({ page }) => {
  const problems = watch(page);
  await page.goto("/#/import");
  await page.locator("#imp-ds").selectOption("fact_claim");
  await page.locator("#imp-period").fill("202610");
  await page.getByLabel("增修").check();
  const csv = `policy_no,claim_no,claim_amt\nP0000000839,C202610000000000,1.23\nP0000000001,${NEW_CLAIM},4.56\n`;
  await page.locator("input[type=file]").setInputFiles({ name: "upsert.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  await page.getByRole("button", { name: "開始上傳並匯入" }).click();
  await page.getByRole("button", { name: "確認並開始" }).click();
  await expect(page.getByText("完成").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("main")).toContainText("新增 1");
  await expect(page.locator("main")).toContainText("更新 1");
  expect(problems, problems.join("\n")).toEqual([]);
});

test("匯入：格式錯誤 → 失敗並提供錯誤列報表", async ({ page }) => {
  await page.goto("/#/import");
  await page.locator("#imp-ds").selectOption("fact_claim");
  await page.locator("#imp-period").fill("202610");
  await page.getByLabel("增修").check();
  const csv = "policy_no,claim_no,claim_amt\nP1,CBAD,not-a-number\n";
  await page.locator("input[type=file]").setInputFiles({ name: "bad.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  await page.getByRole("button", { name: "開始上傳並匯入" }).click();
  await page.getByRole("button", { name: "確認並開始" }).click();
  await expect(page.locator(".notice.error").first()).toContainText("1 列格式錯誤", { timeout: 30_000 });
  const link = page.getByRole("link", { name: /錯誤列報表|下載/ });
  await expect(link).toBeVisible();
  const r = await page.request.get(await link.getAttribute("href") ?? "");
  expect(r.status()).toBe(200);
  expect(await r.text()).toContain("claim_amt:numeric(18,2) 格式不符");
});

test("匯入：依主鍵刪除 → 預覽 → 填操作人確認 → 刪除 1 筆", async ({ page }) => {
  await page.goto("/#/import");
  await page.locator("#imp-ds").selectOption("fact_claim");
  await page.locator("#imp-period").fill("202610");
  await page.getByLabel("依主鍵刪除").check();
  const csv = `yyyymm,policy_no,claim_no\n202610,P0000000001,${NEW_CLAIM}\n202610,PNONE,CNONE\n`;
  await page.locator("input[type=file]").setInputFiles({ name: "del.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  await page.getByRole("button", { name: "開始上傳並匯入" }).click();
  await page.getByRole("button", { name: "確認並開始" }).click();
  await expect(page.locator("main")).toContainText("PNONE", { timeout: 30_000 });   // 不存在的主鍵列在預覽
  const confirm = page.getByRole("button", { name: "確認刪除" });
  await expect(confirm).toBeDisabled();                       // 未填操作人與勾選前不可按
  await page.locator("#imp-op").fill("測試人員");
  await page.getByLabel(/我已確認上述預覽/).check();
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByText("完成").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("main")).toContainText(/刪除\D*1/);
});

test("匯入紀錄：列出剛才的工作並可開詳情", async ({ page }) => {
  await page.goto("/#/loads");
  await expect(page.locator(".ag-row").first()).toBeVisible();
  await expect(page.locator(".ag-row").first()).toContainText("fact_claim");
  await page.locator(".ag-row").first().click();
  await expect(page.locator("main")).toContainText("依鍵刪除");
});

test("資料集：新增欄位需二次確認與操作人，成功後欄位出現", async ({ page }) => {
  const code = `e2e_${Date.now().toString(36).slice(-6)}`;
  await page.goto("/#/datasets");
  await page.getByRole("listitem").filter({ hasText: "fact_claim" }).first().click();
  await page.getByRole("button", { name: "展開表單" }).click();
  const form = page.locator("form").filter({ hasText: "code（實體欄名）" });
  await form.locator("input.mono").first().fill("Bad-Code");
  await expect(form.getByRole("button", { name: "送出…" })).toBeDisabled();
  await form.locator("input.mono").first().fill(code);
  await form.getByLabel("型別", { exact: true }).selectOption("varchar");
  await form.getByLabel("長度 n").fill("20");
  await form.getByRole("button", { name: "送出…" }).click();
  const dlg = page.locator("dialog[open]");
  const ok = dlg.locator("button[type=submit]");
  await expect(ok).toBeDisabled();
  await dlg.locator("input").last().fill("測試人員");
  await ok.click();
  await expect(page.locator(".notice.ok")).toBeVisible();
  await page.getByLabel("搜尋欄位").fill(code);
  await expect(page.locator(".ag-row").filter({ hasText: code })).toBeVisible();
});

test("查詢：匯出時伺服器回錯誤 → 頁面顯示錯誤訊息（經由 iframe 讀回）", async ({ page }) => {
  await page.goto("/#/query");
  await page.locator("#q-from").selectOption("fact_claim");
  await pickColumn(page, "claim_no", "fact_claim");
  await page.getByRole("button", { name: "＋ 計算欄位" }).click();
  await page.getByLabel("運算式").fill("fact_claim.claim_type + 1");   // 文字欄位不可運算 → 伺服器 400
  await page.getByRole("button", { name: "匯出 CSV" }).click();
  await page.getByRole("button", { name: "開始下載" }).click();
  await expect(page.locator(".notice.error")).toContainText("匯出失敗：", { timeout: 10_000 });
  await expect(page.locator(".notice.error")).toContainText("只能使用數值欄位：claim_type");
});

test("跨站寫入請求被拒（CSRF）", async ({ page }) => {
  const evil = await page.request.post("/api/loads/00000000-0000-0000-0000-000000000000/cancel",
    { headers: { Origin: "https://evil.example" } });
  expect(evil.status()).toBe(403);
  expect((await evil.json()).error).toContain("跨站");
  // 本站頁面發出的同一請求不會被這層擋下（工作不存在 → 400）
  await page.goto("/#/loads");
  const same = await page.evaluate(async () =>
    (await fetch("/api/loads/00000000-0000-0000-0000-000000000000/cancel", { method: "POST" })).status);
  expect(same).toBe(400);
});

test("匯入：網址帶錯誤的工作編號 → 顯示錯誤、不輪詢", async ({ page }) => {
  let loadCalls = 0;
  page.on("request", (r) => { if (r.url().includes("/api/loads/")) loadCalls++; });
  await page.goto("/#/import?load=not-a-uuid");
  await expect(page.locator("main")).toContainText("工作編號格式不正確");
  await page.goto("/#/import");                          // 網址不再帶 load → 訊息清除
  await expect(page.locator("main")).not.toContainText("工作編號格式不正確");
  await page.goto("/#/import?load=00000000-0000-0000-0000-000000000000");
  await expect(page.locator(".notice.error").first()).toContainText("load_id 不存在");
  await page.waitForTimeout(4500);                       // 輪詢間隔 2 秒：若仍在輪詢會有 ≥3 次
  expect(loadCalls).toBe(1);
});

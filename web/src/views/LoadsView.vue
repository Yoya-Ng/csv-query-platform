<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { AgGridVue } from "ag-grid-vue3";
import type { ColDef, GridOptions, ICellRendererParams } from "ag-grid-community";
import { ApiError, STATUS_LABEL, api, isTerminal, type Load } from "../lib/api";
import { gridTheme } from "../lib/grid";
import DatasetsConfirmDialog from "../components/DatasetsConfirmDialog.vue";

const SWAP_WAIT = "SWAP_LOCK_TIMEOUT";   // 對應 app/services/importer.py
const MODE_LABEL: Record<string, string> = { replace: "整期重傳", upsert: "增修", delete: "依鍵刪除", drop: "整期刪除" };

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
}

const rows = ref<Load[]>([]);
const loading = ref(false);
const error = ref("");
const okMsg = ref("");
const dsNames = ref<{ name: string; id: number }[]>([]);
const idToName = computed(() => new Map(dsNames.value.map((d) => [d.id, d.name])));
const datasetFilter = ref("");
const statusGroup = ref<"all" | "running" | "failed" | "done">("all");
const selectedId = ref("");
const lastUpdated = ref("");

// ------------------------------------------------------------ 狀態分類
const isWaitLock = (l: Load) => l.status === "BUILT" && !!l.error?.startsWith(SWAP_WAIT);
/** 需要人工處理、輪詢也不會前進的狀態 */
const isWaiting = (l: Load) => l.status === "PREVIEWED" || isWaitLock(l);
const isActive = (l: Load) => !isTerminal(l.status) && !isWaiting(l);
function groupOf(l: Load): "running" | "failed" | "done" {
  if (l.status === "CLEANED") return "done";
  if (l.status === "FAILED" || l.status === "CANCELLED") return "failed";
  return "running";
}
function tone(l: Load): "ok" | "error" | "warn" | "info" | "" {
  if (l.status === "CLEANED") return "ok";
  if (l.status === "FAILED") return "error";
  if (l.status === "CANCELLED") return "";
  if (isWaiting(l)) return "warn";
  return "info";
}
function statusText(l: Load): string {
  return isWaitLock(l) ? `${STATUS_LABEL[l.status]}（等待切換鎖）` : STATUS_LABEL[l.status] ?? l.status;
}
const n = (v: number | null | undefined) => (v == null ? "" : v.toLocaleString("zh-TW"));
function rowsText(l: Load): string {
  switch (l.mode) {
    case "replace": return n(l.rows_inserted ?? l.rows_staged);
    case "upsert":
      return l.rows_inserted == null && l.rows_updated == null
        ? n(l.rows_staged) : `新增 ${n(l.rows_inserted ?? 0)}／更新 ${n(l.rows_updated ?? 0)}`;
    case "delete": return l.rows_deleted != null ? `刪除 ${n(l.rows_deleted)}` : n(l.rows_staged);
    case "drop": return n(l.rows_deleted);
    default: return "";
  }
}
const localTime = (s: string | null) => {
  if (!s) return "";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString("zh-TW", { hour12: false });
};

// ------------------------------------------------------------ 資料載入
const filtered = computed(() => rows.value.filter((l) => statusGroup.value === "all" || groupOf(l) === statusGroup.value));
const hasActive = computed(() => rows.value.some(isActive));
const selected = computed(() => rows.value.find((l) => l.load_id === selectedId.value) ?? null);

let seq = 0;
async function refresh(silent = false) {
  const my = ++seq;
  if (!silent) loading.value = true;
  try {
    const r = await api.loads(datasetFilter.value || undefined, 200);
    if (my !== seq) return;
    rows.value = r;
    error.value = "";
    lastUpdated.value = new Date().toLocaleTimeString("zh-TW", { hour12: false });
  } catch (e) {
    if (my === seq) error.value = `無法取得匯入紀錄：${errMsg(e)}`;
  } finally {
    if (my === seq) loading.value = false;
  }
}

async function loadDatasetIds() {
  try {
    const list = await api.datasets();
    dsNames.value = list.map((d) => ({ name: d.name, id: d.dataset_id }));
  } catch (e) {
    dsNames.value = [];
    error.value ||= `無法取得資料集清單：${errMsg(e)}`;
  }
}

let timer: ReturnType<typeof setInterval> | null = null;
function startTimer() {
  if (timer) return;
  timer = setInterval(() => { if (hasActive.value && !loading.value) void refresh(true); }, 3000);
}
onMounted(async () => {
  startTimer();
  await Promise.all([loadDatasetIds(), refresh()]);
});
onBeforeUnmount(() => { if (timer) clearInterval(timer); timer = null; seq++; });
watch(datasetFilter, () => { selectedId.value = ""; void refresh(); });

// ------------------------------------------------------------ AG Grid
function statusCell(p: ICellRendererParams<Load>): HTMLElement {
  const s = document.createElement("span");
  const t = p.data ? tone(p.data) : "";
  s.className = t ? `badge ${t}` : "badge";
  s.textContent = p.data ? statusText(p.data) : "";
  return s;
}
const colDefs: ColDef<Load>[] = [
  { headerName: "時間", width: 160, valueGetter: (p) => localTime(p.data?.created_at ?? null) },
  { headerName: "資料集", width: 150, valueGetter: (p) => (p.data ? p.data.dataset_name ?? idToName.value.get(p.data.dataset_id) ?? `#${p.data.dataset_id}` : "") },
  { field: "yyyymm", headerName: "期別", width: 90 },
  { headerName: "模式", width: 100, valueGetter: (p) => MODE_LABEL[p.data?.mode ?? ""] ?? p.data?.mode ?? "" },
  { headerName: "狀態", width: 150, cellRenderer: statusCell, valueGetter: (p) => (p.data ? statusText(p.data) : "") },
  { headerName: "筆數", width: 150, valueGetter: (p) => (p.data ? rowsText(p.data) : "") },
  { field: "filename", headerName: "檔名", minWidth: 160 },
  { field: "operator_note", headerName: "操作註記", width: 120 },
  { field: "client_ip", headerName: "IP", width: 120 },
  { headerName: "錯誤", minWidth: 220, flex: 1,
    valueGetter: (p) => { const e = p.data?.error ?? ""; return e.length > 60 ? `${e.slice(0, 60)}…` : e; } },
];
const gridOptions: GridOptions<Load> = {
  defaultColDef: { sortable: true, resizable: true },
  rowSelection: { mode: "singleRow", checkboxes: false, enableClickSelection: true },
  getRowId: (p) => p.data.load_id,
  onRowClicked: (e) => { if (e.data) selectedId.value = e.data.load_id; },
  overlayNoRowsTemplate: "沒有匯入紀錄",
};

// ------------------------------------------------------------ 詳情
const fields: [keyof Load, string][] = [
  ["load_id", "匯入編號"], ["upload_id", "上傳編號"], ["filename", "檔名"], ["yyyymm", "期別"],
  ["rows_csv", "CSV 列數"], ["rows_staged", "暫存筆數"], ["rows_inserted", "新增筆數"], ["rows_updated", "更新筆數"],
  ["rows_deleted", "刪除筆數"], ["rows_matched", "比對到"], ["rows_unmatched", "未比對到"],
  ["bak_table", "備份分割"], ["operator_note", "操作註記"], ["client_ip", "來源 IP"],
];
const times: [keyof Load, string][] = [["created_at", "建立"], ["swapped_at", "切換"], ["cleaned_at", "清理"]];
const checksums = computed<[string, string][] | string | null>(() => {
  const s = selected.value?.checksums;
  if (!s) return null;
  try {
    const o = JSON.parse(s);
    if (o && typeof o === "object" && !Array.isArray(o)) {
      return Object.entries(o).map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)] as [string, string]);
    }
  } catch { /* 非 JSON：原文顯示 */ }
  return s;
});
const detailText = computed(() => {
  const d = selected.value?.detail;
  if (!d) return "";
  try { return JSON.stringify(JSON.parse(d), null, 2); } catch { return d; }
});

// ------------------------------------------------------------ 重試／取消
const acting = ref(false);
const cancelOpen = ref(false);
const cancelError = ref("");
async function retry(l: Load) {
  acting.value = true; okMsg.value = ""; error.value = "";
  try {
    await api.retryLoad(l.load_id);
    okMsg.value = "已重新排入切換，請稍候狀態更新。";
    await refresh(true);
  } catch (e) {
    error.value = `重試切換失敗：${errMsg(e)}`;
  } finally {
    acting.value = false;
  }
}
async function doCancel() {
  const l = selected.value;
  if (!l) return;
  acting.value = true; cancelError.value = "";
  try {
    await api.cancelLoad(l.load_id);
    cancelOpen.value = false;
    okMsg.value = "已取消此匯入。";
    await refresh(true);
  } catch (e) {
    cancelError.value = errMsg(e);
  } finally {
    acting.value = false;
  }
}
</script>

<template>
  <div class="page stack">
    <h1>匯入紀錄</h1>

    <div class="row">
      <select v-model="datasetFilter" aria-label="資料集">
        <option value="">全部資料集</option>
        <option v-for="d in dsNames" :key="d.id" :value="d.name">{{ d.name }}</option>
      </select>
      <select v-model="statusGroup" aria-label="狀態">
        <option value="all">全部狀態</option>
        <option value="running">進行中／待處理</option>
        <option value="failed">失敗／取消</option>
        <option value="done">完成</option>
      </select>
      <button class="btn" type="button" :disabled="loading" @click="refresh()">重新整理</button>
      <span class="small muted">
        共 {{ filtered.length }} 筆（最近 200 筆）<template v-if="lastUpdated">・更新於 {{ lastUpdated }}</template>
        <template v-if="hasActive">・有進行中的工作，每 3 秒自動更新</template>
      </span>
    </div>

    <div v-if="okMsg" class="notice ok">{{ okMsg }}</div>
    <div v-if="error" class="notice error">{{ error }}</div>

    <div class="ld-grid">
      <AgGridVue :theme="gridTheme" :columnDefs="colDefs" :rowData="filtered" :gridOptions="gridOptions"
                 :loading="loading && !rows.length" style="height: 100%; width: 100%" />
    </div>

    <section class="card stack">
      <h2>詳情</h2>
      <div v-if="!selected" class="muted">點選上方一列以檢視詳情。</div>
      <template v-else>
        <div class="row">
          <span class="badge" :class="tone(selected)">{{ statusText(selected) }}</span>
          <span>{{ selected.dataset_name ?? idToName.get(selected.dataset_id) ?? `#${selected.dataset_id}` }}・{{ selected.yyyymm }}・{{ MODE_LABEL[selected.mode] ?? selected.mode }}</span>
        </div>

        <div v-if="selected.error" class="notice" :class="isWaitLock(selected) ? 'warn' : 'error'">
          <div class="mono ld-pre">{{ selected.error }}</div>
        </div>

        <div class="row">
          <a v-if="selected.status === 'FAILED'" class="btn" :href="api.errorReportUrl(selected.load_id)">下載錯誤報表</a>
          <template v-if="isWaitLock(selected)">
            <button class="btn primary" type="button" :disabled="acting" @click="retry(selected)">重試切換</button>
            <button class="btn danger" type="button" :disabled="acting" @click="cancelError = ''; cancelOpen = true">取消</button>
          </template>
          <a v-if="selected.status === 'PREVIEWED'" class="btn primary" :href="`#/import?load=${selected.load_id}`">前往確認刪除</a>
        </div>
        <div v-if="selected.status === 'FAILED'" class="small muted">錯誤報表僅在驗證階段產生；若無報表，連結會顯示「沒有錯誤報表」。</div>

        <div class="table-wrap">
          <table class="plain">
            <tbody>
              <tr v-for="[k, label] in fields" :key="k">
                <th style="width: 120px">{{ label }}</th>
                <td class="mono" style="overflow-wrap: anywhere">{{ selected[k] ?? "" }}</td>
              </tr>
              <tr v-for="[k, label] in times" :key="k">
                <th>{{ label }}時間</th><td>{{ localTime(selected[k] as string | null) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="checksums">
          <h3>驗算值（SUM）</h3>
          <div v-if="typeof checksums === 'string'" class="mono ld-pre">{{ checksums }}</div>
          <div v-else-if="!checksums.length" class="muted small">無驗算欄位。</div>
          <div v-else class="table-wrap">
            <table class="plain">
              <thead><tr><th>欄位</th><th>SUM</th></tr></thead>
              <tbody><tr v-for="[k, v] in checksums" :key="k"><td class="mono">{{ k }}</td><td class="mono">{{ v }}</td></tr></tbody>
            </table>
          </div>
        </div>

        <div v-if="detailText">
          <h3>detail</h3>
          <pre class="mono ld-pre ld-box">{{ detailText }}</pre>
        </div>
      </template>
    </section>

    <DatasetsConfirmDialog :open="cancelOpen" title="確認取消匯入" confirm-label="取消此匯入" danger
                           :require-operator="false" :busy="acting" :error="cancelError"
                           @confirm="doCancel" @cancel="cancelOpen = false">
      <p>取消後此匯入不會再切換到正式資料；需要這批資料時須重新上傳。</p>
    </DatasetsConfirmDialog>
  </div>
</template>

<style scoped>
.ld-grid { height: 420px; min-width: 0; }
.ld-pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; }
.ld-box { background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 8px; max-height: 240px; overflow: auto; }
</style>

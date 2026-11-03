<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { api, ApiError, isTerminal, STATUS_LABEL, UUID_RE } from "../lib/api";
import type { DatasetSummary, Load, LoadMode, LoadStatus } from "../lib/api";
import { clearResume, uploadFile, validateFile, CHUNK_SIZE } from "../lib/upload";
import type { Progress } from "../lib/upload";

const route = useRoute();

// ---------------------------------------------------------------- 表單
const datasets = ref<DatasetSummary[]>([]);
const datasetName = ref("");
const yyyymm = ref("");
const mode = ref<LoadMode>("replace");
const periods = ref<number[]>([]);
const periodsKnown = ref(false); // 期別清單是否取得成功（端點可能暫時 404）
const file = ref<File | null>(null);
const fileError = ref("");
const dragging = ref(false);
const formError = ref("");

const MODES: { value: LoadMode; label: string; help: string }[] = [
  { value: "replace", label: "整期重傳", help: "以檔案內容整期替換該期資料；舊資料以備份保留 7 天，可回復。" },
  { value: "upsert", label: "增修", help: "依主鍵更新或新增；只更新檔案中有的欄位，其餘欄位維持原值。期別需已存在。" },
  { value: "delete", label: "依主鍵刪除", help: "檔案只能有主鍵欄位；會先預覽符合與不存在的筆數，確認後才刪除。期別需已存在。" },
];
const modeHelp = computed(() => MODES.find((m) => m.value === mode.value)?.help ?? "");

const periodValid = computed(() => {
  const m = /^(\d{4})(\d{2})$/.exec(yyyymm.value);
  if (!m) return false;
  const mon = Number(m[2]);
  return Number(m[1]) >= 1900 && mon >= 1 && mon <= 12;
});
const periodMsg = computed(() => {
  if (!yyyymm.value) return "";
  if (!/^\d{0,6}$/.test(yyyymm.value)) return "只能輸入數字";
  if (yyyymm.value.length < 6) return "請輸入 6 碼西元年月（例如 202610）";
  if (!periodValid.value) return "月份需為 01–12，年份需為西元年";
  return "";
});
const periodExists = computed(() => periods.value.includes(Number(yyyymm.value)));
const needExisting = computed(() => mode.value !== "replace");
const periodModeMsg = computed(() => {
  if (!periodValid.value || !needExisting.value || !periodsKnown.value) return "";
  return periodExists.value ? "" : "增修與刪除只能選已存在的期別；新期別請用「整期重傳」。";
});

async function loadPeriods() {
  periods.value = [];
  periodsKnown.value = false;
  if (!datasetName.value) return;
  const name = datasetName.value;
  try {
    const list = await api.periods(name);
    if (name !== datasetName.value) return;
    periods.value = list.map((p) => p.yyyymm).sort((a, b) => b - a);
    periodsKnown.value = true;
  } catch {
    /* 端點可能不存在：改為手動輸入，由後端驗證 */
  }
}
watch(datasetName, loadPeriods);

function pickFile(f: File | null | undefined) {
  fileError.value = "";
  file.value = null;
  if (!f) return;
  const bad = validateFile(f);
  if (bad) { fileError.value = bad; return; }
  file.value = f;
}
function onInput(e: Event) {
  const el = e.target as HTMLInputElement;
  pickFile(el.files?.[0]);
  el.value = "";
}
function onDrop(e: DragEvent) {
  dragging.value = false;
  pickFile(e.dataTransfer?.files?.[0]);
}

function fmtMB(n: number) {
  return (n / 1024 / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 }) + " MB";
}
function fmtSize(n: number) {
  return n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + " GB" : fmtMB(n);
}

const busy = computed(() => stage.value === "uploading" || stage.value === "creating");
const canStart = computed(() =>
  !!datasetName.value && periodValid.value && !!file.value && !busy.value
  && !(needExisting.value && periodsKnown.value && !periodExists.value));

// ---------------------------------------------------------------- 上傳與工作
type Stage = "idle" | "uploading" | "creating" | "watching";
const stage = ref<Stage>("idle");
const prog = ref<Progress | null>(null);
const hashProg = ref({ done: 0, total: 0 });
const upProg = ref({ done: 0, total: 0 });
const uploadError = ref("");
const load = ref<Load | null>(null);
const loadError = ref("");
const loadErrorFatal = ref(false);   // true：4xx，已停止輪詢；false：暫時錯誤，仍在重試
const queryError = ref("");          // 網址 ?load= 的問題（與輪詢錯誤分開）
let abortCtl: AbortController | null = null;

const confirmDlg = ref<HTMLDialogElement | null>(null);
function askStart() {
  formError.value = "";
  if (!canStart.value) return;
  confirmDlg.value?.showModal();
}
function pct(p: { done: number; total: number }) {
  return p.total > 0 ? Math.min(100, Math.floor((p.done / p.total) * 100)) : 0;
}

async function startImport() {
  confirmDlg.value?.close();
  const f = file.value;
  if (!f) return;
  uploadError.value = "";
  loadError.value = "";
  load.value = null;
  hashProg.value = { done: 0, total: f.size };
  upProg.value = { done: 0, total: 0 };
  prog.value = null;
  stage.value = "uploading";
  abortCtl = new AbortController();
  try {
    const r = await uploadFile(f, {
      signal: abortCtl.signal,
      onProgress: (p) => {
        prog.value = p;
        if (p.phase === "hash") hashProg.value = { done: p.done, total: p.total };
        else if (p.phase === "upload") upProg.value = { done: p.done, total: p.total };
      },
    });
    stage.value = "creating";
    const { load_id } = await api.createLoad({
      dataset: datasetName.value, yyyymm: Number(yyyymm.value), mode: mode.value, upload_id: r.upload_id,
    });
    clearResume(f);
    stage.value = "watching";
    watchLoad(load_id);
  } catch (e) {
    stage.value = "idle";
    if ((e as Error).name === "AbortError") uploadError.value = "上傳已中止。再次選取同一檔案並開始，會從缺少的區塊續傳。";
    else uploadError.value = errMsg(e);
  } finally {
    abortCtl = null;
  }
}
function abortUpload() { abortCtl?.abort(); }

function errMsg(e: unknown) {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e);
}

// ---------------------------------------------------------------- 輪詢
let timer: ReturnType<typeof setTimeout> | null = null;
let watchToken = 0;

function stopPolling() {
  watchToken++;
  if (timer) { clearTimeout(timer); timer = null; }
}
function waitingForUser(l: Load) {
  return l.status === "PREVIEWED";
}
async function poll(id: string, token: number) {
  try {
    const l = await api.load(id);
    if (token !== watchToken) return;
    load.value = l;
    loadError.value = "";
    loadErrorFatal.value = false;
    if (isTerminal(l.status) || waitingForUser(l)) return;
  } catch (e) {
    if (token !== watchToken) return;
    loadError.value = errMsg(e);
    // 4xx（工作不存在、ID 格式錯誤、權限）不會自行恢復，停止輪詢；網路錯誤、5xx、408、429 才繼續
    const fatal = e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429;
    loadErrorFatal.value = fatal;
    if (fatal) return;
  }
  timer = setTimeout(() => poll(id, token), 2000);
}
function watchLoad(id: string) {
  stopPolling();
  if (load.value?.load_id !== id) load.value = null;   // 換工作時不沿用上一個工作的資料
  loadError.value = "";
  loadErrorFatal.value = false;
  stage.value = "watching";
  const token = watchToken;
  poll(id, token);
}

onMounted(async () => {
  try {
    datasets.value = await api.datasets();
    if (!datasetName.value && datasets.value.length) datasetName.value = datasets.value[0].name;
  } catch (e) {
    formError.value = errMsg(e);
  }
  openLoadFromQuery(route.query.load);
});
// 網址 ?load=：合法 UUID 才輪詢；格式錯誤只顯示訊息（不呼叫 API、不輪詢，也不動目前畫面上的上傳或工作）
function openLoadFromQuery(q: unknown) {
  queryError.value = "";
  if (!q) return;                       // 回到 /import：保留從表單建立、未寫入網址的工作
  if (typeof q !== "string" || !UUID_RE.test(q)) {
    queryError.value = "網址中的匯入工作編號格式不正確";
  } else if (stage.value === "uploading" || stage.value === "creating") {
    queryError.value = "上傳進行中，完成或中止後再開啟其他匯入工作";
  } else {
    watchLoad(q);
  }
}
watch(() => route.query.load, openLoadFromQuery);
onBeforeUnmount(() => {
  stopPolling();
  abortCtl?.abort();
});

// ---------------------------------------------------------------- 時間軸
const STEPS: Record<string, LoadStatus[]> = {
  replace: ["UPLOADED", "STAGED", "VALIDATED", "BUILT", "SWAPPED", "CLEANED"],
  upsert: ["UPLOADED", "STAGED", "VALIDATED", "APPLIED", "CLEANED"],
  delete: ["UPLOADED", "STAGED", "VALIDATED", "PREVIEWED", "APPLIED", "CLEANED"],
};
const steps = computed(() => STEPS[load.value?.mode ?? "replace"] ?? STEPS.replace);
const stepIdx = computed(() => (load.value ? steps.value.indexOf(load.value.status) : -1));
function stepClass(i: number) {
  const l = load.value;
  if (!l) return "";
  if (l.status === "CLEANED") return "done";
  if (i < stepIdx.value) return "done";
  if (i === stepIdx.value) return l.status === "FAILED" || l.status === "CANCELLED" ? "" : "current";
  return "";
}
const MODE_LABEL: Record<string, string> = { replace: "整期重傳", upsert: "增修", delete: "依主鍵刪除", drop: "整期刪除" };
function badgeClass(s: LoadStatus) {
  return s === "FAILED" ? "error" : s === "CLEANED" ? "ok" : s === "CANCELLED" ? "warn" : "info";
}

const hasErrorReport = computed(() => !!load.value?.error?.includes("錯誤列報表"));
const lockTimeout = computed(() => load.value?.status === "BUILT" && !!load.value.error?.startsWith("SWAP_LOCK_TIMEOUT"));

const actionBusy = ref(false);
const actionMsg = ref("");
async function doRetry() {
  if (!load.value) return;
  actionBusy.value = true; actionMsg.value = "";
  try {
    await api.retryLoad(load.value.load_id);
    watchLoad(load.value.load_id);
  } catch (e) { actionMsg.value = errMsg(e); } finally { actionBusy.value = false; }
}
async function doCancel() {
  if (!load.value) return;
  if (!window.confirm("確定取消這次匯入？已建立的中間產物會被清除。")) return;
  actionBusy.value = true; actionMsg.value = "";
  try {
    await api.cancelLoad(load.value.load_id);
    watchLoad(load.value.load_id);
  } catch (e) { actionMsg.value = errMsg(e); } finally { actionBusy.value = false; }
}

function reset() {
  stopPolling();
  load.value = null;
  stage.value = "idle";
  uploadError.value = "";
  loadError.value = "";
  loadErrorFatal.value = false;
  queryError.value = "";
  actionMsg.value = "";
  file.value = null;
}

// ---------------------------------------------------------------- 刪除預覽與確認
interface DelDetail { period_rows: number; missing_sample: string[][]; pk: string[] }
const delDetail = computed<DelDetail | null>(() => {
  const l = load.value;
  if (!l || l.mode !== "delete" || !l.detail) return null;
  try {
    const d = JSON.parse(l.detail);
    return {
      period_rows: Number(d.period_rows ?? 0),
      missing_sample: Array.isArray(d.missing_sample) ? d.missing_sample : [],
      pk: Array.isArray(d.pk) ? d.pk : [],
    };
  } catch { return null; }
});
const isLarge = computed(() => {
  const l = load.value, d = delDetail.value;
  return !!l && !!d && (l.rows_matched ?? 0) > d.period_rows * 0.1;
});
const operator = ref("");
const ack = ref(false);
const ackLarge = ref(false);
const delBusy = ref(false);
const delMsg = ref("");
const delMsgKind = ref<"error" | "warn">("error");
const canConfirmDelete = computed(() =>
  operator.value.trim().length > 0 && ack.value && (!isLarge.value || ackLarge.value) && !delBusy.value);

async function doConfirmDelete() {
  const l = load.value;
  if (!l || !canConfirmDelete.value) return;
  delBusy.value = true; delMsg.value = "";
  try {
    load.value = await api.confirmDelete(l.load_id, operator.value.trim(), isLarge.value && ackLarge.value);
    if (!isTerminal(load.value.status)) watchLoad(l.load_id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      delMsgKind.value = "warn";
      delMsg.value = "目前無法取得資料表鎖（有長查詢或備份佔用），資料未變動，仍可稍後再按「確認刪除」。";
    } else {
      delMsgKind.value = "error";
      delMsg.value = errMsg(e);
      // 刪除失敗會讓工作變成 FAILED：重新取得狀態
      try {
        load.value = await api.load(l.load_id);
        if (!isTerminal(load.value.status) && load.value.status !== "PREVIEWED") watchLoad(l.load_id);
      } catch { /* 保留原畫面 */ }
    }
  } finally { delBusy.value = false; }
}
watch(() => load.value?.load_id, () => { operator.value = ""; ack.value = false; ackLarge.value = false; delMsg.value = ""; });
</script>

<template>
  <div class="page stack">
    <h1>匯入</h1>
    <div v-if="queryError" class="notice error">{{ queryError }}</div>

    <!-- 設定 -->
    <section v-if="stage === 'idle' && !load" class="card stack">
      <h2>匯入設定</h2>
      <div v-if="formError" class="notice error">{{ formError }}</div>
      <div class="grid-2">
        <div class="field">
          <label for="imp-ds">資料集</label>
          <select id="imp-ds" v-model="datasetName">
            <option v-for="d in datasets" :key="d.name" :value="d.name">{{ d.name }}{{ d.description ? `（${d.description}）` : "" }}</option>
          </select>
        </div>
        <div class="field">
          <label for="imp-period">期別（西元年月 YYYYMM）</label>
          <div class="row">
            <input id="imp-period" v-model.trim="yyyymm" type="text" inputmode="numeric" maxlength="6"
                   placeholder="202610" autocomplete="off" class="grow" />
            <select v-if="periods.length" aria-label="帶入現有期別" @change="yyyymm = ($event.target as HTMLSelectElement).value; ($event.target as HTMLSelectElement).selectedIndex = 0">
              <option value="">現有期別…</option>
              <option v-for="p in periods" :key="p" :value="String(p)">{{ p }}</option>
            </select>
          </div>
          <span v-if="periodMsg" class="small" style="color: var(--danger)">{{ periodMsg }}</span>
          <span v-else-if="periodModeMsg" class="small" style="color: var(--danger)">{{ periodModeMsg }}</span>
          <span v-else-if="needExisting && !periodsKnown" class="small muted">暫時無法取得現有期別清單，期別是否存在將由伺服器驗證。</span>
        </div>
      </div>

      <fieldset class="field" style="border: 0; padding: 0; margin: 0">
        <legend class="label">模式</legend>
        <div class="row">
          <label v-for="m in MODES" :key="m.value" class="row" style="gap: 4px">
            <input v-model="mode" type="radio" name="imp-mode" :value="m.value" /> {{ m.label }}
          </label>
        </div>
        <span class="small muted">{{ modeHelp }}</span>
      </fieldset>

      <div class="field">
        <span class="label">檔案</span>
        <label class="card" :style="{ borderStyle: 'dashed', borderColor: dragging ? 'var(--accent)' : undefined, background: dragging ? 'var(--accent-soft)' : undefined, cursor: 'pointer', textAlign: 'center' }"
               @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onDrop">
          <input type="file" accept=".csv,.gz,text/csv" style="display: none" @change="onInput" />
          <div v-if="file"><strong style="overflow-wrap: anywhere">{{ file.name }}</strong>
            <span class="muted">（{{ fmtSize(file.size) }}，約 {{ Math.ceil(file.size / CHUNK_SIZE) }} 個區塊）</span></div>
          <div v-else class="muted">拖放 CSV 檔到這裡，或點擊選擇檔案</div>
        </label>
        <div v-if="fileError" class="notice error">{{ fileError }}</div>
      </div>

      <details open>
        <summary class="small muted">CSV 格式規則</summary>
        <ul class="small muted" style="margin: 6px 0 0; padding-left: 20px">
          <li>UTF-8 編碼（可帶 BOM）、逗號分隔。</li>
          <li>第一列為表頭，需為欄位名稱或別名；重複、空白、未知欄位會被拒絕。</li>
          <li>未加引號的空欄與 "" 視為空值；NA、N/A 為一般字串，原樣保留。</li>
          <li>單檔 50GB 以內，可使用 .csv 或 .csv.gz。</li>
        </ul>
      </details>

      <div class="row">
        <button class="btn primary" type="button" :disabled="!canStart" @click="askStart">開始上傳並匯入</button>
      </div>
      <div v-if="uploadError" class="notice error">{{ uploadError }}</div>
    </section>

    <!-- 上傳進度 -->
    <section v-if="stage === 'uploading' || stage === 'creating'" class="card stack">
      <h2>上傳中</h2>
      <div class="muted small" style="overflow-wrap: anywhere">{{ file?.name }}</div>
      <div class="stack" style="gap: 4px">
        <div class="row"><span class="grow">1. 計算 SHA-256</span>
          <span class="small muted">{{ pct(hashProg) }}%（{{ fmtMB(hashProg.done) }} / {{ fmtMB(hashProg.total) }}）</span></div>
        <div class="progress"><span :style="{ width: pct(hashProg) + '%' }"></span></div>
      </div>
      <div class="stack" style="gap: 4px">
        <div class="row"><span class="grow">2. 上傳區塊</span>
          <span class="small muted">{{ pct(upProg) }}%（{{ fmtMB(upProg.done) }} / {{ fmtMB(upProg.total) }}）</span></div>
        <div class="progress"><span :style="{ width: pct(upProg) + '%' }"></span></div>
      </div>
      <div v-if="prog?.phase === 'complete' || stage === 'creating'" class="small muted">組合檔案並建立匯入工作…</div>
      <div class="row"><button class="btn" type="button" :disabled="stage === 'creating'" @click="abortUpload">中止上傳</button></div>
    </section>

    <!-- 工作狀態 -->
    <section v-if="load || (stage === 'watching' && loadError)" class="card stack">
      <div class="row">
        <h2 class="grow" style="margin: 0">匯入工作</h2>
        <span v-if="load" class="badge" :class="badgeClass(load.status)">{{ STATUS_LABEL[load.status] }}（{{ load.status }}）</span>
      </div>
      <div v-if="loadError" class="notice" :class="loadErrorFatal ? 'error' : 'warn'">{{ loadError }}</div>
      <template v-if="load">
        <div class="small muted" style="overflow-wrap: anywhere">
          {{ MODE_LABEL[load.mode] ?? load.mode }}　期別 {{ load.yyyymm }}　檔案 {{ load.filename ?? "—" }}<br />
          工作 ID <code>{{ load.load_id }}</code>
        </div>

        <ol class="steps" aria-label="匯入進度">
          <li v-for="(s, i) in steps" :key="s" :class="stepClass(i)">
            <span class="dot"></span><span>{{ STATUS_LABEL[s] }}</span><span class="small muted">{{ s }}</span>
          </li>
        </ol>

        <div class="row small">
          <span v-if="load.rows_csv != null">CSV 記錄數 <strong>{{ load.rows_csv.toLocaleString() }}</strong></span>
          <span v-if="load.rows_staged != null">暫存列數 <strong>{{ load.rows_staged.toLocaleString() }}</strong></span>
          <span v-if="load.rows_inserted != null">新增 <strong>{{ load.rows_inserted.toLocaleString() }}</strong></span>
          <span v-if="load.rows_updated != null">更新 <strong>{{ load.rows_updated.toLocaleString() }}</strong></span>
          <span v-if="load.rows_deleted != null">已刪除 <strong>{{ load.rows_deleted.toLocaleString() }}</strong></span>
        </div>
        <div v-if="load.checksums" class="small">
          <div class="label">驗算欄位合計</div>
          <pre class="mono" style="margin: 0; white-space: pre-wrap; overflow-wrap: anywhere">{{ load.checksums }}</pre>
        </div>

        <div v-if="load.status === 'FAILED'" class="notice error">
          <strong>匯入失敗</strong>
          <div style="white-space: pre-wrap">{{ load.error }}</div>
          <div v-if="hasErrorReport" style="margin-top: 6px">
            <a :href="api.errorReportUrl(load.load_id)" download>下載錯誤列報表（CSV）</a>
          </div>
        </div>
        <div v-else-if="load.status === 'CANCELLED'" class="notice warn">此匯入已取消。{{ load.error ?? "" }}</div>

        <div v-if="lockTimeout" class="notice warn stack">
          <div><strong>該資料集有長查詢或備份佔用</strong>，切換新資料時取鎖逾時，資料尚未更動。可稍後重試切換，或取消這次匯入。</div>
          <div class="small">{{ load.error }}</div>
          <div class="row">
            <button class="btn primary" type="button" :disabled="actionBusy" @click="doRetry">重試切換</button>
            <button class="btn danger" type="button" :disabled="actionBusy" @click="doCancel">取消匯入</button>
          </div>
        </div>
        <div v-if="actionMsg" class="notice error">{{ actionMsg }}</div>

        <!-- 刪除預覽 -->
        <div v-if="load.status === 'PREVIEWED'" class="stack">
          <h3>刪除預覽</h3>
          <div class="row">
            <span class="badge info">符合 {{ (load.rows_matched ?? 0).toLocaleString() }} 筆</span>
            <span class="badge" :class="(load.rows_unmatched ?? 0) > 0 ? 'warn' : ''">不存在 {{ (load.rows_unmatched ?? 0).toLocaleString() }} 筆</span>
            <span v-if="delDetail" class="small muted">該期現有 {{ delDetail.period_rows.toLocaleString() }} 筆</span>
          </div>
          <div v-if="delDetail?.missing_sample.length">
            <div class="label">不存在的主鍵（範例，最多顯示前幾筆）</div>
            <div class="table-wrap" style="max-height: 280px; overflow-y: auto">
              <table class="plain">
                <thead><tr><th v-for="(c, i) in delDetail.pk.length ? delDetail.pk : delDetail.missing_sample[0].map((_, k) => `欄位 ${k + 1}`)" :key="i">{{ c }}</th></tr></thead>
                <tbody><tr v-for="(r, ri) in delDetail.missing_sample" :key="ri"><td v-for="(v, ci) in r" :key="ci">{{ v }}</td></tr></tbody>
              </table>
            </div>
          </div>

          <div class="field">
            <label for="imp-op">操作人姓名</label>
            <input id="imp-op" v-model="operator" type="text" autocomplete="off" maxlength="200" />
          </div>
          <label class="row" style="gap: 6px"><input v-model="ack" type="checkbox" /> 我已確認上述預覽，要刪除符合的 {{ (load.rows_matched ?? 0).toLocaleString() }} 筆資料</label>
          <div v-if="isLarge" class="notice warn stack">
            <div>刪除筆數超過該期現有筆數的 10%。建議改用「整期重傳」，以完整檔案取代整期資料。</div>
            <label class="row" style="gap: 6px"><input v-model="ackLarge" type="checkbox" /> 我了解筆數偏高，仍要以刪除清單執行</label>
          </div>
          <div v-if="delMsg" class="notice" :class="delMsgKind">{{ delMsg }}</div>
          <div class="row">
            <button class="btn danger" type="button" :disabled="!canConfirmDelete" @click="doConfirmDelete">確認刪除</button>
            <button class="btn" type="button" :disabled="actionBusy || delBusy" @click="doCancel">取消匯入</button>
          </div>
        </div>

        <div v-if="load.status === 'CLEANED'" class="notice ok">
          匯入完成。<span v-if="load.mode === 'delete' && load.rows_deleted != null">已刪除 {{ load.rows_deleted.toLocaleString() }} 筆。</span>
        </div>
        <div v-else-if="!isTerminal(load.status) && load.status !== 'PREVIEWED'" class="small muted">每 2 秒自動更新狀態；離開本頁不影響伺服器端作業，可從匯入紀錄回來查看。</div>
      </template>
      <div class="row">
        <button v-if="!load || isTerminal(load.status)" class="btn" type="button" @click="reset">再匯入一個檔案</button>
      </div>
    </section>

    <!-- 二次確認 -->
    <dialog ref="confirmDlg" class="modal" aria-labelledby="imp-dlg-title">
      <form method="dialog" class="stack" @submit.prevent="startImport">
        <h2 id="imp-dlg-title">確認匯入</h2>
        <div class="small" style="overflow-wrap: anywhere">
          資料集 <strong>{{ datasetName }}</strong>　期別 <strong>{{ yyyymm }}</strong>　模式 <strong>{{ MODES.find((m) => m.value === mode)?.label }}</strong><br />
          檔案 <strong>{{ file?.name }}</strong>（{{ file ? fmtSize(file.size) : "" }}）
        </div>
        <div v-if="mode === 'replace'" class="notice warn">
          整期重傳會<strong>取代該期的全部資料</strong>。原資料會備份保留 7 天，但切換後查詢立即看到新資料。
        </div>
        <div v-else-if="mode === 'delete'" class="notice info">刪除不會立即執行，會先上傳清單並顯示預覽，經你再次確認後才刪除。</div>
        <div v-else class="notice info">增修會依主鍵更新或新增檔案中的資料。</div>
        <div class="row end">
          <button class="btn" type="button" @click="confirmDlg?.close()">取消</button>
          <button class="btn primary" type="submit">確認並開始</button>
        </div>
      </form>
    </dialog>
  </div>
</template>

<style scoped>
.steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.steps li { display: flex; align-items: center; gap: 8px; color: var(--text-muted); flex-wrap: wrap; }
.steps .dot { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--border); background: var(--surface); flex: none; }
.steps li.done { color: var(--text); }
.steps li.done .dot { background: var(--ok); border-color: var(--ok); }
.steps li.current { color: var(--text); font-weight: 600; }
.steps li.current .dot { background: var(--accent); border-color: var(--accent); }
</style>

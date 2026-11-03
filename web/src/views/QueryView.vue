<script setup lang="ts">
// 查詢頁（SPEC 9、10）：主表＋join、欄位挑選、計算欄位、篩選、排序 → 預覽（每頁 1,000 筆）、總筆數、複製、匯出。
import { computed, onMounted, reactive, ref, shallowRef, watch } from "vue";
import { AgGridVue } from "ag-grid-vue3";
import type { ColDef } from "ag-grid-community";
import { api, ApiError, exportCsv, type Dataset, type DatasetSummary, type PreviewResult, type QuerySpec, type WhereOp } from "../lib/api";
import { gridTheme } from "../lib/grid";
import {
  BuildError, buildSpec, emptyFilter, emptyState, isNumericType, MAX_IN_VALUES, parseList, stateFromSpec,
  suggestOn, tablesInQuery, type BuilderState,
} from "../lib/querySpec";
import QueryColumnPicker from "../components/QueryColumnPicker.vue";

const PAGE = 1000;
const st = reactive<BuilderState>(emptyState());
const summaries = ref<DatasetSummary[]>([]);
const cache = reactive<Record<string, Dataset>>({});
const periodsOf = reactive<Record<string, number[]>>({});
const loadError = ref("");

const inQuery = computed(() => tablesInQuery(st).map((n) => cache[n]).filter((d): d is Dataset => !!d));
const colOptions = computed(() =>
  inQuery.value.flatMap((d) => d.columns.filter((c) => !c.disabled).map((c) => ({ ref: `${d.name}.${c.code}`, c, ds: d }))));
const colByRef = computed(() => new Map(colOptions.value.map((o) => [o.ref, o])));

async function ensureDataset(name: string, refresh = false) {
  if (!name || (cache[name] && !refresh)) return;
  cache[name] = await api.dataset(name);
  try {
    periodsOf[name] = (await api.periods(name)).map((p) => p.yyyymm);
  } catch {
    periodsOf[name] = [];
  }
}

onMounted(async () => {
  try {
    summaries.value = await api.datasets();
  } catch (e) {
    loadError.value = (e as Error).message;
  }
});

// 選主表：載入定義，預設帶入最新期別條件（大表必須帶期別，SPEC 9.2）
async function onFromChange() {
  const name = st.from;
  Object.assign(st, { ...emptyState(), from: name });
  result.value = null;
  if (!name) return;
  try {
    // 每次選主表都重新取定義與期別（其他頁可能剛匯入新期別或新增欄位）
    await ensureDataset(name, true);
    if (st.from !== name) return;          // 等待期間使用者已改選其他資料集
    const ds = cache[name];
    const latest = periodsOf[name]?.[0];
    const f = emptyFilter(`${name}.${ds.partition_key}`);
    if (latest) f.value = String(latest);
    st.filters.push(f);
  } catch (e) {
    loadError.value = (e as Error).message;
  }
}

// ---------------------------------------------------------------- join
const joinable = computed(() => summaries.value.filter((s) => !tablesInQuery(st).includes(s.name)));
async function addJoin(name: string) {
  if (!name) return;
  await ensureDataset(name);
  st.joins.push({ table: name, type: "left", on: suggestOn(cache[st.from], cache[name]) });
}
function joinRightCols(table: string) {
  return (cache[table]?.columns ?? []).filter((c) => !c.disabled);
}
function joinLeftOptions(i: number) {
  // 左欄可引用主表與「前面已 join」的表
  const names = [st.from, ...st.joins.slice(0, i).map((j) => j.table)];
  return colOptions.value.filter((o) => names.includes(o.ds.name));
}
function removeJoin(i: number) {
  const t = st.joins[i].table;
  st.joins.splice(i, 1);
  st.select = st.select.filter((r) => !r.startsWith(`${t}.`));
  st.filters = st.filters.filter((f) => !f.col.startsWith(`${t}.`));
  st.order = st.order.filter((o) => !o.ref.startsWith(`${t}.`));
}

// ---------------------------------------------------------------- 計算欄位
function addCalc() {
  let n = st.calc.length + 1;
  while (st.calc.some((c) => c.name === `calc_${n}`)) n++;
  st.calc.push({ name: `calc_${n}`, expr: "" });
}
const numericRefs = computed(() => colOptions.value.filter((o) => isNumericType(o.c)).map((o) => o.ref));
function insertRef(i: number, r: string) {
  if (!r) return;
  const c = st.calc[i];
  c.expr = c.expr ? `${c.expr} ${r}` : r;
}

// ---------------------------------------------------------------- 篩選
const OPS: { v: WhereOp; label: string }[] = [
  { v: "=", label: "等於" }, { v: "in", label: "在清單中（可貼 Excel 整欄）" }, { v: "between", label: "介於" },
  { v: "like_prefix", label: "開頭為" }, { v: "is_null", label: "是空值" }, { v: "not_null", label: "不是空值" },
];
function opsFor(r: string) {
  const c = colByRef.value.get(r)?.c;
  return OPS.filter((o) => o.v !== "like_prefix" || (c && c.data_type.startsWith("varchar")));
}
function inputType(r: string) {
  const c = colByRef.value.get(r)?.c;
  return c?.data_type === "date" ? "date" : "text";
}
function isPartitionCol(r: string) {
  const o = colByRef.value.get(r);
  return !!o && o.c.code === o.ds.partition_key;
}

// ---------------------------------------------------------------- 排序
const orderOptions = computed(() => [
  ...st.calc.filter((c) => c.name).map((c) => ({ v: c.name, label: `計算：${c.name}` })),
  ...colOptions.value.map((o) => ({ v: o.ref, label: o.ref })),
]);

// ---------------------------------------------------------------- 執行
// 1,000 列結果不需要深層響應式（AG Grid 自己管理資料）
const result = shallowRef<PreviewResult | null>(null);
const resultsEl = ref<HTMLElement | null>(null);
const page = ref(0);
const running = ref(false);
const jobId = ref("");
const error = ref("");
const manyToManyBlocked = ref(false);
const allowMany = ref(false);
const total = ref<number | null>(null);
const counting = ref(false);
const notice = ref("");
let lastSpec: QuerySpec | null = null;

function newJobId() {
  // randomUUID 只在安全來源（HTTPS／localhost）可用；其他情況以 getRandomValues 組 UUID v4
  const c: Crypto = globalThis.crypto;
  if (typeof c.randomUUID === "function") return c.randomUUID();
  const b = c.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function currentSpec(): QuerySpec | null {
  error.value = "";
  try {
    return buildSpec(st);
  } catch (e) {
    if (e instanceof BuildError) error.value = e.message;
    else throw e;
    return null;
  }
}

function showError(e: unknown) {
  const msg = e instanceof ApiError ? e.message : (e as Error).message;
  error.value = msg;
  manyToManyBlocked.value = msg.includes("多對多");
}

async function run(p = 0) {
  // 翻頁沿用上次預覽的規格；建構器改過但沒按「預覽」時，不可拿新規格的第 N 頁
  const spec = p > 0 && lastSpec ? lastSpec : currentSpec();
  if (!spec) return;
  running.value = true;
  manyToManyBlocked.value = false;
  notice.value = "";
  jobId.value = newJobId();
  if (JSON.stringify(spec) !== JSON.stringify(lastSpec)) total.value = null;
  try {
    const r = await api.preview(spec, { offset: p * PAGE, limit: PAGE, allow_many_to_many: allowMany.value, job_id: jobId.value });
    result.value = r;
    page.value = p;
    lastSpec = spec;
    if (window.innerWidth <= 1100) {
      requestAnimationFrame(() => resultsEl.value?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  } catch (e) {
    showError(e);
  } finally {
    running.value = false;
    jobId.value = "";
  }
}

async function cancel() {
  if (!jobId.value) return;
  try {
    const r = await api.cancelQuery(jobId.value);
    notice.value = r.cancelled ? "已送出取消" : "查詢已結束或尚未開始";
  } catch (e) {
    showError(e);
  }
}

async function doCount() {
  const spec = currentSpec();
  if (!spec) return;
  counting.value = true;
  try {
    total.value = (await api.count(spec, { allow_many_to_many: allowMany.value })).count;
  } catch (e) {
    showError(e);
  } finally {
    counting.value = false;
  }
}

function retryAllowMany() {
  allowMany.value = true;
  run(0);
}

// ---------------------------------------------------------------- 結果表格
const displayName = computed(() => {
  const m: Record<string, string> = {};
  for (const o of colOptions.value) m[`${o.ds.name}__${o.c.code}`] = o.c.display_name ? `${o.c.display_name}（${o.ref}）` : o.ref;
  return m;
});
const outToRef = computed(() => new Map(colOptions.value.map((o) => [`${o.ds.name}__${o.c.code}`, o.ref])));
const columnDefs = computed<ColDef[]>(() => (result.value?.columns ?? []).map((name, i) => {
  const ref = outToRef.value.get(name) ?? null;
  const numeric = ref ? isNumericType(colByRef.value.get(ref)?.c) : true;   // 計算欄位一律數值
  return {
    headerName: displayName.value[name] ?? name,
    headerTooltip: name,
    field: String(i),
    valueGetter: (p) => (p.data as unknown[])[i],
    type: numeric ? "rightAligned" : undefined,
    minWidth: 90,
    sortable: false,
  };
}));
const rowData = computed(() => result.value?.rows ?? []);
const pageInfo = computed(() => {
  if (!result.value) return "";
  const start = page.value * PAGE + (rowData.value.length ? 1 : 0);
  const end = page.value * PAGE + rowData.value.length;
  return `第 ${start.toLocaleString()}–${end.toLocaleString()} 筆` + (total.value !== null ? ` / 共 ${total.value.toLocaleString()} 筆` : "");
});

// ---------------------------------------------------------------- 複製（SPEC 10）
const copyDialog = ref<HTMLDialogElement | null>(null);
const copyText = ref("");
const copying = ref(false);
async function doCopy() {
  const spec = currentSpec();
  if (!spec) return;
  copying.value = true;
  try {
    const tsv = await api.copyTsv(spec, { allow_many_to_many: allowMany.value });
    const rows = Math.max(tsv.split("\n").length - 2, 0);
    try {
      await navigator.clipboard.writeText(tsv);
      notice.value = `已複製 ${rows.toLocaleString()} 筆資料（另含表頭一列），可直接貼到 Excel`;
    } catch {
      // 非 HTTPS 或瀏覽器拒絕：改顯示可全選的文字框
      copyText.value = tsv;
      copyDialog.value?.showModal();
      requestAnimationFrame(() => (document.getElementById("copy-area") as HTMLTextAreaElement | null)?.select());
    }
  } catch (e) {
    showError(e);
  } finally {
    copying.value = false;
  }
}

// ---------------------------------------------------------------- 匯出
const exportDialog = ref<HTMLDialogElement | null>(null);
const exp = reactive({ encoding: "utf-8-sig" as "utf-8-sig" | "big5", gzip: false });
function openExport() {
  if (!currentSpec()) return;
  exportDialog.value?.showModal();
  if (total.value === null) doCount();
}
function doExport() {
  const spec = currentSpec();
  if (!spec) return;
  exportDialog.value?.close();
  notice.value = "已開始匯出，瀏覽器會直接下載檔案（大量資料需要一段時間）";
  exportCsv(spec, { ...exp, allow_many_to_many: allowMany.value }, (msg) => {
    notice.value = "";
    error.value = `匯出失敗：${msg}`;
  });
}

// ---------------------------------------------------------------- 我的查詢
const savedDialog = ref<HTMLDialogElement | null>(null);
const saveDialog = ref<HTMLDialogElement | null>(null);
const saved = ref<{ query_id: number; name: string; note: string; created_at: string }[]>([]);
const saveName = ref("");
const saveNote = ref("");
async function openSaved() {
  try {
    saved.value = await api.savedQueries();
    savedDialog.value?.showModal();
  } catch (e) {
    showError(e);
  }
}
async function loadSaved(id: number) {
  try {
    const q = await api.loadQuery(id);
    for (const t of [q.spec.from, ...(q.spec.joins ?? []).map((j) => j.table)]) await ensureDataset(t);
    Object.assign(st, stateFromSpec(q.spec));
    savedDialog.value?.close();
    result.value = null;
    total.value = null;
    notice.value = q.stale_columns.length
      ? ""
      : `已載入「${q.name}」`;
    if (q.stale_columns.length) {
      error.value = `「${q.name}」引用的欄位已停用或不存在：${q.stale_columns.join("、")}；已從欄位清單移除，請確認後再執行`;
      st.select = st.select.filter((r) => !q.stale_columns.includes(r));
    }
  } catch (e) {
    showError(e);
  }
}
async function doSave() {
  const spec = currentSpec();
  if (!spec || !saveName.value.trim()) return;
  try {
    await api.saveQuery(saveName.value.trim(), spec, saveNote.value.trim());
    saveDialog.value?.close();
    notice.value = `已儲存「${saveName.value.trim()}」`;
    saveName.value = "";
    saveNote.value = "";
  } catch (e) {
    showError(e);
  }
}

const specJson = computed(() => {
  try { return JSON.stringify(buildSpec(st), null, 2); } catch (e) { return `// ${(e as Error).message}`; }
});

watch(() => st.from, () => { total.value = null; });
</script>

<template>
  <div class="page query">
    <div class="row head">
      <h1 class="grow">查詢</h1>
      <button type="button" class="btn" @click="openSaved">我的查詢</button>
      <button type="button" class="btn" :disabled="!st.from" @click="saveDialog?.showModal()">儲存查詢</button>
    </div>
    <p v-if="loadError" class="notice error">{{ loadError }}</p>

    <div class="layout">
      <!-- ============ 建構器 ============ -->
      <div class="builder stack">
        <section class="card stack">
          <h2>1. 資料來源</h2>
          <div class="field">
            <label for="q-from">主資料集</label>
            <select id="q-from" v-model="st.from" @change="onFromChange">
              <option value="">請選擇</option>
              <option v-for="s in summaries" :key="s.name" :value="s.name">{{ s.name }}{{ s.description ? `（${s.description}）` : "" }}</option>
            </select>
          </div>

          <div v-for="(j, i) in st.joins" :key="j.table" class="join">
            <div class="row">
              <strong class="mono grow">{{ j.table }}</strong>
              <select v-model="j.type" aria-label="join 類型">
                <option value="left">LEFT JOIN（保留主表全部列）</option>
                <option value="inner">INNER JOIN（只留兩邊都有的列）</option>
              </select>
              <button type="button" class="btn sm ghost" @click="removeJoin(i)">移除</button>
            </div>
            <div v-for="(p, k) in j.on" :key="k" class="row on">
              <select v-model="p.left" class="grow" aria-label="左表欄位">
                <option value="">左表欄位</option>
                <option v-for="o in joinLeftOptions(i)" :key="o.ref" :value="o.ref">{{ o.ref }}</option>
              </select>
              <span>=</span>
              <select v-model="p.right" class="grow" :aria-label="`${j.table} 欄位`">
                <option value="">{{ j.table }} 欄位</option>
                <option v-for="c in joinRightCols(j.table)" :key="c.code" :value="c.code">{{ j.table }}.{{ c.code }}</option>
              </select>
              <button type="button" class="btn sm ghost" aria-label="移除對應" @click="j.on.splice(k, 1)">✕</button>
            </div>
            <button type="button" class="btn sm ghost" @click="j.on.push({ left: '', right: '' })">＋ 對應欄位</button>
          </div>

          <div v-if="st.from" class="row">
            <select aria-label="加入 join 資料集" @change="(e) => { addJoin((e.target as HTMLSelectElement).value); (e.target as HTMLSelectElement).value = ''; }">
              <option value="">＋ join 其他資料集…</option>
              <option v-for="s in joinable" :key="s.name" :value="s.name">{{ s.name }}</option>
            </select>
            <span class="muted small">預設以兩表共同的主鍵欄位對應（含期別，才能分割裁剪）</span>
          </div>
        </section>

        <section v-if="st.from" class="card stack">
          <h2>2. 篩選條件</h2>
          <p class="muted small">大表必須指定期別（{{ cache[st.from]?.partition_key }}）。LEFT JOIN 的右表期別由對應欄位帶入，不要另外加在這裡，否則會變成 INNER JOIN。</p>
          <div v-for="(f, i) in st.filters" :key="i" class="filter">
            <div class="row nowrap">
              <select v-model="f.col" class="grow" aria-label="篩選欄位">
                <option value="">欄位</option>
                <option v-for="o in colOptions" :key="o.ref" :value="o.ref">{{ o.ref }}{{ o.c.display_name ? `（${o.c.display_name}）` : "" }}</option>
              </select>
              <select v-model="f.op" aria-label="條件">
                <option v-for="o in opsFor(f.col)" :key="o.v" :value="o.v">{{ o.label }}</option>
              </select>
              <button type="button" class="btn sm ghost" aria-label="移除條件" @click="st.filters.splice(i, 1)">✕</button>
            </div>
            <div class="row">
              <template v-if="f.op === '=' || f.op === 'like_prefix'">
                <input v-if="isPartitionCol(f.col) && periodsOf[f.col.split('.')[0]]?.length"
                       v-model="f.value" class="grow" :list="`periods-${i}`" placeholder="期別 YYYYMM" aria-label="值" />
                <input v-else v-model="f.value" :type="inputType(f.col)" class="grow" placeholder="值" aria-label="值" />
                <datalist :id="`periods-${i}`">
                  <option v-for="p in periodsOf[f.col.split('.')[0]] ?? []" :key="p" :value="p" />
                </datalist>
              </template>
              <template v-else-if="f.op === 'between'">
                <input v-model="f.value" :type="inputType(f.col)" class="grow" placeholder="起" aria-label="起始值" />
                <span>～</span>
                <input v-model="f.value2" :type="inputType(f.col)" class="grow" placeholder="迄" aria-label="結束值" />
              </template>
              <template v-else-if="f.op === 'in'">
                <textarea v-model="f.list" rows="3" class="grow" placeholder="每行一個值，可直接貼上 Excel 整欄（逗號不會被當成分隔）" aria-label="值清單"></textarea>
                <span class="small" :class="parseList(f.list).length > MAX_IN_VALUES ? 'badge warn' : 'muted'">
                  {{ parseList(f.list).length.toLocaleString() }} 個值（已去重複）
                </span>
              </template>
            </div>
          </div>
          <div><button type="button" class="btn sm" @click="st.filters.push(emptyFilter())">＋ 條件</button></div>
        </section>

        <section v-if="st.from" class="card stack">
          <h2>3. 輸出欄位</h2>
          <QueryColumnPicker v-model="st.select" :datasets="inQuery" :calc-count="st.calc.length" />
        </section>

        <section v-if="st.from" class="card stack">
          <h2>4. 計算欄位</h2>
          <p class="muted small">只能用數值欄位與 + − × ÷、括號、ROUND(x, n)、ABS(x)、COALESCE(x, y)；除以 0 結果為空值。欄位寫成 <code>表.欄</code>。</p>
          <div v-for="(c, i) in st.calc" :key="i" class="calc stack">
            <div class="row">
              <input v-model="c.name" class="name" placeholder="名稱（小寫英數底線）" aria-label="計算欄位名稱" />
              <select aria-label="插入欄位" @change="(e) => { insertRef(i, (e.target as HTMLSelectElement).value); (e.target as HTMLSelectElement).value = ''; }">
                <option value="">插入數值欄位…</option>
                <option v-for="r in numericRefs" :key="r" :value="r">{{ r }}</option>
              </select>
              <button type="button" class="btn sm ghost" aria-label="移除計算欄位" @click="st.calc.splice(i, 1)">✕</button>
            </div>
            <input v-model="c.expr" class="mono" placeholder="例：claim.claim_amt - claim.paid_amt" aria-label="運算式" />
          </div>
          <div><button type="button" class="btn sm" @click="addCalc">＋ 計算欄位</button></div>
        </section>

        <section v-if="st.from" class="card stack">
          <h2>5. 排序</h2>
          <div v-for="(o, i) in st.order" :key="i" class="row">
            <select v-model="o.ref" class="grow" aria-label="排序欄位">
              <option value="">欄位</option>
              <option v-for="x in orderOptions" :key="x.v" :value="x.v">{{ x.label }}</option>
            </select>
            <select v-model="o.dir" aria-label="方向"><option value="asc">遞增</option><option value="desc">遞減</option></select>
            <select v-model="o.nulls" aria-label="空值位置"><option value="nulls_last">空值在後</option><option value="nulls_first">空值在前</option></select>
            <button type="button" class="btn sm ghost" aria-label="移除排序" @click="st.order.splice(i, 1)">✕</button>
          </div>
          <div><button type="button" class="btn sm" @click="st.order.push({ ref: '', dir: 'asc', nulls: 'nulls_last' })">＋ 排序</button></div>
          <details>
            <summary class="muted small">查詢規格（JSON）</summary>
            <pre class="json mono">{{ specJson }}</pre>
          </details>
        </section>
      </div>

      <!-- ============ 結果 ============ -->
      <div ref="resultsEl" class="results stack">
        <section class="card stack">
          <div class="row actions">
            <button type="button" class="btn primary" :disabled="!st.from || running" @click="run(0)">{{ running ? "查詢中…" : "預覽" }}</button>
            <button v-if="running" type="button" class="btn danger" @click="cancel">取消查詢</button>
            <button type="button" class="btn" :disabled="!st.from || counting" @click="doCount">{{ counting ? "計算中…" : "總筆數" }}</button>
            <button type="button" class="btn" :disabled="!st.from || copying" @click="doCopy">{{ copying ? "複製中…" : "複製（≤1 萬筆）" }}</button>
            <button type="button" class="btn" :disabled="!st.from" @click="openExport">匯出 CSV</button>
            <label class="row small"><input v-model="allowMany" type="checkbox" /> 允許多對多 join</label>
          </div>
          <p v-if="error" class="notice error">
            {{ error }}
            <button v-if="manyToManyBlocked" type="button" class="btn sm" @click="retryAllowMany">確認允許多對多並重新執行</button>
          </p>
          <p v-if="notice" class="notice ok">{{ notice }}</p>
          <p v-for="h in result?.hints ?? []" :key="h" class="notice warn small">提示：{{ h }}</p>

          <div v-if="result" class="row">
            <span class="grow small">{{ pageInfo }}</span>
            <button type="button" class="btn sm" :disabled="page === 0 || running" @click="run(page - 1)">上一頁</button>
            <span class="small">第 {{ page + 1 }} 頁</span>
            <button type="button" class="btn sm" :disabled="!result.has_more || running" @click="run(page + 1)">下一頁</button>
          </div>
          <p v-else class="muted">選擇資料集、條件與欄位後按「預覽」。預覽每頁 1,000 筆；匯出不限筆數。</p>
          <AgGridVue v-if="result" class="grid" :theme="gridTheme" :row-data="rowData" :column-defs="columnDefs"
                     :default-col-def="{ resizable: true, maxWidth: 420 }" :enable-cell-text-selection="true"
                     :auto-size-strategy="{ type: 'fitCellContents' }"
                     :suppress-column-virtualisation="false" :overlay-no-rows-template="'沒有符合條件的資料'" />
        </section>
      </div>
    </div>

    <!-- 複製失敗時的文字框 -->
    <dialog ref="copyDialog" class="modal">
      <h2>手動複製</h2>
      <p class="small muted">瀏覽器不允許直接寫入剪貼簿（需 HTTPS 或使用者操作）。請按 Ctrl+C／⌘C 複製下方內容。</p>
      <textarea id="copy-area" readonly rows="12" class="copy-area">{{ copyText }}</textarea>
      <div class="row end"><button type="button" class="btn" @click="copyDialog?.close()">關閉</button></div>
    </dialog>

    <!-- 匯出 -->
    <dialog ref="exportDialog" class="modal">
      <h2>匯出 CSV</h2>
      <div class="stack">
        <p class="small">{{ total === null ? "正在計算筆數…" : `共 ${total.toLocaleString()} 筆` }}
          <template v-if="total !== null && total > 1048576"><br /><span class="notice warn small">超過 Excel 單一工作表上限 1,048,576 列，Excel 無法完整開啟</span></template>
        </p>
        <div class="field">
          <label>編碼</label>
          <label class="row small"><input v-model="exp.encoding" type="radio" value="utf-8-sig" /> UTF-8（含 BOM，Excel 可直接開啟，建議）</label>
          <label class="row small"><input v-model="exp.encoding" type="radio" value="big5" /> Big5（無法表示的字元會讓匯出中止）</label>
        </div>
        <label class="row small"><input v-model="exp.gzip" type="checkbox" /> 壓縮成 .csv.gz（檔案約小一半）</label>
      </div>
      <div class="row end">
        <button type="button" class="btn" @click="exportDialog?.close()">取消</button>
        <button type="button" class="btn primary" @click="doExport">開始下載</button>
      </div>
    </dialog>

    <!-- 我的查詢 -->
    <dialog ref="savedDialog" class="modal">
      <h2>我的查詢</h2>
      <p v-if="!saved.length" class="muted">尚未儲存查詢</p>
      <div class="table-wrap saved">
        <table v-if="saved.length" class="plain">
          <thead><tr><th>名稱</th><th>註記</th><th>建立時間</th><th></th></tr></thead>
          <tbody>
            <tr v-for="q in saved" :key="q.query_id">
              <td>{{ q.name }}</td><td class="muted">{{ q.note }}</td>
              <td class="small">{{ new Date(q.created_at).toLocaleString() }}</td>
              <td><button type="button" class="btn sm" @click="loadSaved(q.query_id)">載入</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="row end"><button type="button" class="btn" @click="savedDialog?.close()">關閉</button></div>
    </dialog>

    <dialog ref="saveDialog" class="modal">
      <h2>儲存查詢</h2>
      <div class="stack">
        <div class="field"><label for="save-name">名稱</label><input id="save-name" v-model="saveName" maxlength="200" /></div>
        <div class="field"><label for="save-note">註記（選填）</label><input id="save-note" v-model="saveNote" maxlength="200" /></div>
        <p class="small muted">所有使用者都看得到儲存的查詢（系統沒有登入）。</p>
      </div>
      <div class="row end">
        <button type="button" class="btn" @click="saveDialog?.close()">取消</button>
        <button type="button" class="btn primary" :disabled="!saveName.trim()" @click="doSave">儲存</button>
      </div>
    </dialog>
  </div>
</template>

<style scoped>
.head h1 { margin: 0; }
.layout { display: grid; grid-template-columns: minmax(360px, 440px) minmax(0, 1fr); gap: var(--gap); align-items: start; margin-top: var(--gap); }
@media (max-width: 1100px) { .layout { grid-template-columns: minmax(0, 1fr); } }
.builder, .results { min-width: 0; }
.results { position: sticky; top: 60px; }
@media (max-width: 1100px) { .results { position: static; } }
.join, .filter, .calc { border: 1px solid var(--border); border-radius: 6px; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
.on select { min-width: 0; flex: 1 1 140px; }
.nowrap { flex-wrap: nowrap; }
.filter .nowrap select[aria-label="條件"] { flex: 0 1 9.5em; min-width: 6em; }
.filter .nowrap select[aria-label="篩選欄位"] { flex: 1 1 auto; min-width: 0; }
.calc .name { flex: 1 1 140px; }
.grid { height: min(68vh, 720px); width: 100%; }
.json { background: var(--surface-2); padding: 8px; border-radius: 6px; overflow: auto; max-height: 260px; white-space: pre; }
.copy-area { width: 100%; }
.saved { max-height: 50vh; overflow: auto; }
.actions { gap: 6px; }
</style>

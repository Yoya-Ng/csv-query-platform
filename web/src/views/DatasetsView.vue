<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { AgGridVue } from "ag-grid-vue3";
import type { ColDef, GridOptions, ICellRendererParams } from "ag-grid-community";
import { ApiError, api, type ColumnDef, type Dataset, type DatasetSummary, type PeriodInfo } from "../lib/api";
import { gridTheme } from "../lib/grid";
import DatasetsConfirmDialog from "../components/DatasetsConfirmDialog.vue";

const IDENT_RE = /^[a-z][a-z0-9_]{0,29}$/;   // 對應 app/catalog.py IDENT_RE

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
}
function writeErr(e: unknown): string {
  if (e instanceof ApiError && e.status === 409) return "資料集正被寫入或備份佔用，請稍後再試";
  return errMsg(e);
}

// ------------------------------------------------------------ 資料集清單與詳情
const list = ref<DatasetSummary[]>([]);
const listError = ref("");
const listLoading = ref(false);
const selected = ref("");
const ds = ref<Dataset | null>(null);
const dsError = ref("");
const dsLoading = ref(false);
let dsSeq = 0;

async function loadList() {
  listLoading.value = true;
  listError.value = "";
  try {
    list.value = await api.datasets();
  } catch (e) {
    listError.value = errMsg(e);
  } finally {
    listLoading.value = false;
  }
}

async function select(name: string) {
  selected.value = name;
  const seq = ++dsSeq;
  dsLoading.value = true;
  dsError.value = "";
  ds.value = null;
  focusCol.value = null;
  periods.value = [];
  periodsError.value = "";
  try {
    const d = await api.dataset(name);
    if (seq !== dsSeq) return;
    ds.value = d;
  } catch (e) {
    if (seq === dsSeq) dsError.value = errMsg(e);
  } finally {
    if (seq === dsSeq) dsLoading.value = false;
  }
  if (seq === dsSeq) void loadPeriods();
}

onMounted(loadList);

// ------------------------------------------------------------ 欄位表（AG Grid）
const quick = ref("");
const groupFilter = ref("");
const focusCol = ref<ColumnDef | null>(null);

const groups = computed(() => {
  const s = new Set<string>();
  ds.value?.columns.forEach((c) => s.add(c.group_name));
  return [...s].sort((a, b) => a.localeCompare(b, "zh-Hant"));
});
const rows = computed(() => (ds.value?.columns ?? [])
  .filter((c) => !groupFilter.value || c.group_name === groupFilter.value)
  .slice().sort((a, b) => a.sort_order - b.sort_order));
const activeCount = computed(() => ds.value?.columns.filter((c) => !c.disabled).length ?? 0);

function badgeCell(p: ICellRendererParams<ColumnDef>): HTMLElement {
  const s = document.createElement("span");
  s.className = p.data?.disabled ? "badge" : "badge ok";
  s.textContent = p.data?.disabled ? "已停用" : "使用中";
  return s;
}
const colDefs: ColDef<ColumnDef>[] = [
  { field: "code", headerName: "實體名", minWidth: 150, cellClass: "mono" },
  { field: "display_name", headerName: "顯示名稱", minWidth: 130 },
  { field: "data_type", headerName: "型別", width: 130, cellClass: "mono" },
  { field: "required", headerName: "必填", width: 80, valueFormatter: (p) => (p.value ? "是" : "") },
  { headerName: "主鍵", width: 90, valueGetter: (p) => (p.data?.is_pk ? String(p.data.pk_order) : "") },
  { field: "checksum", headerName: "驗算", width: 80, valueFormatter: (p) => (p.value ? "是" : "") },
  { field: "group_name", headerName: "群組", width: 120 },
  { headerName: "別名", minWidth: 140, valueGetter: (p) => (p.data?.aliases ?? []).join(", ") },
  { field: "description", headerName: "說明", minWidth: 180 },
  { headerName: "狀態", width: 100, cellRenderer: badgeCell, filter: false,
    valueGetter: (p) => (p.data?.disabled ? "已停用" : "使用中") },
];
const gridOptions: GridOptions<ColumnDef> = {
  defaultColDef: { sortable: true, resizable: true, filter: true },
  rowSelection: { mode: "singleRow", checkboxes: false, enableClickSelection: true },
  getRowId: (p) => p.data.code,
  rowClassRules: { "ds-disabled": (p) => !!p.data?.disabled },
  onSelectionChanged: (e) => { focusCol.value = e.api.getSelectedRows()[0] ?? null; },
  overlayNoRowsTemplate: "沒有欄位",
};

// ------------------------------------------------------------ 停用欄位
const disableReason = computed(() => {
  const c = focusCol.value;
  if (!c || !ds.value) return "";
  if (c.disabled) return "此欄位已停用。";
  if (c.is_pk) return "主鍵欄位不可停用。";
  if (c.code === ds.value.partition_key) return "分割鍵不可停用。";
  return "";
});

// ------------------------------------------------------------ 新增欄位表單
const BASE_TYPES = ["integer", "bigint", "date", "numeric", "varchar"] as const;
const form = reactive({
  code: "", base: "varchar" as (typeof BASE_TYPES)[number], p: 18, s: 2, n: 50,
  display_name: "", aliases: "", required: false, checksum: false, group_name: "", sort_order: 0, description: "",
});
const formOpen = ref(false);

const dataType = computed(() => {
  if (form.base === "numeric") return `numeric(${form.p},${form.s})`;
  if (form.base === "varchar") return `varchar(${form.n})`;
  return form.base;
});
const isNumeric = computed(() => ["integer", "bigint", "numeric"].includes(form.base));
watch(isNumeric, (v) => { if (!v) form.checksum = false; });

const codeError = computed(() => {
  if (!form.code) return "";
  if (!IDENT_RE.test(form.code)) return "需小寫英文字母開頭，僅限小寫英數與底線，最長 30 字";
  if (ds.value?.columns.some((c) => c.code === form.code)) return "欄位已存在（含已停用的欄位）";
  return "";
});
const typeError = computed(() => {
  const int = Number.isInteger;
  if (form.base === "numeric") {
    if (!int(form.p) || !int(form.s)) return "精度與小數位需為整數";
    if (form.p < 1 || form.p > 38 || form.s < 0 || form.s > form.p) return "numeric 需 1≤p≤38、0≤s≤p";
  }
  if (form.base === "varchar" && (!int(form.n) || form.n < 1 || form.n > 4000)) return "varchar 長度需 1～4000";
  return "";
});
const aliasList = computed(() => form.aliases.split(",").map((s) => s.trim()).filter(Boolean));
const aliasError = computed(() => {
  if (aliasList.value.some((a) => a.includes("|"))) return "別名不可含 |";
  if (new Set(aliasList.value).size !== aliasList.value.length) return "別名重複";
  return "";
});
const formValid = computed(() =>
  !!form.code && !codeError.value && !typeError.value && !aliasError.value && Number.isInteger(form.sort_order));

function openForm() {
  formOpen.value = !formOpen.value;
  if (formOpen.value && ds.value) form.sort_order = Math.max(0, ...ds.value.columns.map((c) => c.sort_order)) + 1;
}

// ------------------------------------------------------------ 期別
const periods = ref<PeriodInfo[]>([]);
const periodsError = ref("");
const periodsLoading = ref(false);
async function loadPeriods() {
  if (!selected.value) return;
  const name = selected.value;
  periodsLoading.value = true;
  periodsError.value = "";
  try {
    const r = await api.periods(name);
    if (name === selected.value) periods.value = r;
  } catch (e) {
    if (name === selected.value) { periods.value = []; periodsError.value = `無法取得期別清單：${errMsg(e)}`; }
  } finally {
    periodsLoading.value = false;
  }
}

// ------------------------------------------------------------ 建立資料集（上傳 JSON）
interface DefSummary { name: string; partition_key: string; cols: number; pks: string[]; exists: boolean }
const defObj = ref<unknown>(null);
const defSummary = ref<DefSummary | null>(null);
const defError = ref("");
const fileName = ref("");

async function onFile(ev: Event) {
  const f = (ev.target as HTMLInputElement).files?.[0];
  defObj.value = null; defSummary.value = null; defError.value = ""; fileName.value = f?.name ?? "";
  if (!f) return;
  if (f.size > 5 * 1024 * 1024) { defError.value = "定義檔過大（上限 5 MB）"; return; }
  try {
    const o = JSON.parse(await f.text());
    if (!o || typeof o !== "object" || Array.isArray(o)) throw new Error("內容需為 JSON 物件");
    if (typeof o.name !== "string" || !o.name) throw new Error("缺少 name");
    if (typeof o.partition_key !== "string" || !o.partition_key) throw new Error("缺少 partition_key");
    if (!Array.isArray(o.columns) || o.columns.length === 0) throw new Error("缺少 columns（需為非空陣列）");
    const bad = o.columns.find((c: unknown) => !c || typeof c !== "object"
      || typeof (c as ColumnDef).code !== "string" || typeof (c as ColumnDef).data_type !== "string");
    if (bad) throw new Error("columns 內每個欄位需有 code 與 data_type");
    const cols = o.columns as ColumnDef[];
    if (!cols.some((c) => c.code === o.partition_key)) throw new Error(`partition_key「${o.partition_key}」不在 columns 內`);
    defObj.value = o;
    defSummary.value = {
      name: o.name, partition_key: o.partition_key, cols: cols.length,
      pks: cols.filter((c) => c.is_pk).sort((a, b) => (a.pk_order ?? 0) - (b.pk_order ?? 0)).map((c) => c.code),
      exists: list.value.some((d) => d.name === o.name),
    };
  } catch (e) {
    defError.value = `定義檔無法使用：${errMsg(e)}`;
  }
}

// ------------------------------------------------------------ 二次確認與送出
type Pending =
  | { kind: "create" }
  | { kind: "add" }
  | { kind: "disable"; code: string }
  | { kind: "drop"; yyyymm: number };
const pending = ref<Pending | null>(null);
const busy = ref(false);
const dlgError = ref("");
const okMsg = ref("");
const pageError = ref("");

function ask(p: Pending) { pending.value = p; dlgError.value = ""; okMsg.value = ""; pageError.value = ""; }
function closeDlg() { if (!busy.value) pending.value = null; }

async function doConfirm(operator: string) {
  const p = pending.value;
  if (!p) return;
  busy.value = true;
  dlgError.value = "";
  try {
    if (p.kind === "create") {
      const created = await api.createDataset(defObj.value, operator);
      okMsg.value = `已建立資料集 ${created.name}（${created.columns.length} 欄）`;
      defObj.value = null; defSummary.value = null; fileName.value = "";
      await loadList();
      pending.value = null;
      await select(created.name);
    } else if (p.kind === "add" && selected.value) {
      const updated = await api.addColumn(selected.value, {
        code: form.code, data_type: dataType.value, display_name: form.display_name.trim(),
        aliases: aliasList.value, required: form.required, checksum: form.checksum,
        group_name: form.group_name.trim(), sort_order: form.sort_order, description: form.description.trim(),
        operator_note: operator,
      });
      ds.value = updated;
      okMsg.value = `已新增欄位 ${form.code}（schema_version ${updated.schema_version}）`;
      Object.assign(form, { code: "", display_name: "", aliases: "", required: false, checksum: false,
        group_name: "", description: "", sort_order: form.sort_order + 1 });
      formOpen.value = false;
      pending.value = null;
      void loadList();
    } else if (p.kind === "disable" && selected.value) {
      const updated = await api.disableColumn(selected.value, p.code, operator);
      ds.value = updated;
      focusCol.value = null;
      okMsg.value = `已停用欄位 ${p.code}`;
      pending.value = null;
      void loadList();
    } else if (p.kind === "drop" && selected.value) {
      const r = await api.dropPeriod(selected.value, p.yyyymm, operator);
      okMsg.value = `已整期刪除 ${p.yyyymm}（紀錄編號 ${r.load_id}）。分割已 DETACH 並改名 _bak，保留 7 天可還原。`;
      pending.value = null;
      void loadPeriods();
    }
  } catch (e) {
    dlgError.value = writeErr(e);
  } finally {
    busy.value = false;
  }
}

const dlgTitle = computed(() => {
  switch (pending.value?.kind) {
    case "create": return "確認建立資料集";
    case "add": return "確認新增欄位";
    case "disable": return "確認停用欄位";
    case "drop": return "確認整期刪除";
    default: return "";
  }
});
const dlgLabel = computed(() => ({ create: "建立資料集", add: "新增欄位", disable: "停用欄位", drop: "整期刪除" })[pending.value?.kind ?? "create"]);
const fmt = (n: number) => n.toLocaleString("zh-TW");
</script>

<template>
  <div class="page stack">
    <h1>資料集與欄位</h1>

    <div v-if="okMsg" class="notice ok">{{ okMsg }}</div>
    <div v-if="pageError" class="notice error">{{ pageError }}</div>

    <section class="card">
      <div class="row">
        <h2 class="grow">資料集</h2>
        <button class="btn sm" type="button" :disabled="listLoading" @click="loadList">重新整理</button>
      </div>
      <div v-if="listError" class="notice error">無法取得資料集清單：{{ listError }}</div>
      <div v-else-if="listLoading && !list.length" class="muted">載入中…</div>
      <div v-else-if="!list.length" class="muted">尚無資料集，請由下方「建立資料集」上傳定義檔。</div>
      <div class="row ds-list" role="list">
        <button v-for="d in list" :key="d.name" type="button" role="listitem" class="btn"
                :class="{ primary: d.name === selected }" :title="d.description" @click="select(d.name)">
          {{ d.name }} <span class="small">（{{ d.columns }} 欄・v{{ d.schema_version }}）</span>
        </button>
      </div>
    </section>

    <template v-if="selected">
      <div v-if="dsError" class="notice error">無法取得資料集 {{ selected }}：{{ dsError }}</div>
      <div v-else-if="dsLoading" class="muted">載入中…</div>

      <template v-if="ds">
        <section class="card stack">
          <div>
            <h2>{{ ds.name }}</h2>
            <div class="muted small">
              分割鍵 <code>{{ ds.partition_key }}</code>・schema_version {{ ds.schema_version }}・
              使用中 {{ activeCount }} 欄／共 {{ ds.columns.length }} 欄<span v-if="ds.description">・{{ ds.description }}</span>
            </div>
          </div>
          <div class="row">
            <input v-model="quick" type="search" class="grow" placeholder="搜尋欄位（實體名、顯示名稱、別名、說明…）"
                   aria-label="搜尋欄位" />
            <select v-model="groupFilter" aria-label="依群組篩選">
              <option value="">全部群組</option>
              <option v-for="g in groups" :key="g" :value="g">{{ g || "（未分群組）" }}</option>
            </select>
          </div>
          <div class="ds-grid">
            <AgGridVue :theme="gridTheme" :columnDefs="colDefs" :rowData="rows" :gridOptions="gridOptions"
                       :quickFilterText="quick" style="height: 100%; width: 100%" />
          </div>

          <div class="card ds-focus">
            <template v-if="focusCol">
              <div class="row">
                <strong class="mono">{{ focusCol.code }}</strong>
                <span class="muted">{{ focusCol.display_name }}・{{ focusCol.data_type }}</span>
                <span class="grow" />
                <button class="btn danger sm" type="button" :disabled="!!disableReason"
                        @click="ask({ kind: 'disable', code: focusCol.code })">停用此欄</button>
              </div>
              <div v-if="disableReason" class="small muted">{{ disableReason }}</div>
              <div class="small muted">只停用不刪除；停用後查詢與匯入都不再使用此欄。</div>
            </template>
            <div v-else class="small muted">點選欄位表中的一列，可停用該欄位。</div>
          </div>
        </section>

        <section class="card stack">
          <div class="row">
            <h2 class="grow">新增欄位</h2>
            <button class="btn sm" type="button" :aria-expanded="formOpen" @click="openForm">{{ formOpen ? "收合" : "展開表單" }}</button>
          </div>
          <form v-if="formOpen" class="stack" @submit.prevent="ask({ kind: 'add' })">
            <div class="grid-2">
              <div class="field">
                <label>code（實體欄名）</label>
                <input v-model="form.code" type="text" autocomplete="off" maxlength="30" class="mono" />
                <span v-if="codeError" class="small ds-err">{{ codeError }}</span>
                <span v-else class="small muted">^[a-z][a-z0-9_]{0,29}$</span>
              </div>
              <div class="field">
                <label>顯示名稱</label>
                <input v-model="form.display_name" type="text" />
              </div>
              <div class="field">
                <label>型別</label>
                <div class="row">
                  <select v-model="form.base" aria-label="型別">
                    <option v-for="t in BASE_TYPES" :key="t" :value="t">{{ t }}</option>
                  </select>
                  <template v-if="form.base === 'numeric'">
                    <input v-model.number="form.p" type="number" min="1" max="38" style="width: 76px" aria-label="精度 p" />
                    <input v-model.number="form.s" type="number" min="0" max="38" style="width: 76px" aria-label="小數位 s" />
                  </template>
                  <input v-if="form.base === 'varchar'" v-model.number="form.n" type="number" min="1" max="4000"
                         style="width: 96px" aria-label="長度 n" />
                  <code>{{ dataType }}</code>
                </div>
                <span v-if="typeError" class="small ds-err">{{ typeError }}</span>
              </div>
              <div class="field">
                <label>別名（逗號分隔，CSV 表頭比對用）</label>
                <input v-model="form.aliases" type="text" />
                <span v-if="aliasError" class="small ds-err">{{ aliasError }}</span>
              </div>
              <div class="field">
                <label>群組</label>
                <input v-model="form.group_name" type="text" list="ds-groups" />
                <datalist id="ds-groups"><option v-for="g in groups" :key="g" :value="g" /></datalist>
              </div>
              <div class="field">
                <label>排序</label>
                <input v-model.number="form.sort_order" type="number" step="1" />
              </div>
            </div>
            <div class="field">
              <label>說明</label>
              <textarea v-model="form.description" rows="2" />
            </div>
            <div class="row">
              <label><input v-model="form.required" type="checkbox" /> 必填</label>
              <label :class="{ muted: !isNumeric }">
                <input v-model="form.checksum" type="checkbox" :disabled="!isNumeric" /> 匯入驗算（SUM，僅數值型別）
              </label>
            </div>
            <div class="notice warn">
              新增欄位會取資料集獨佔鎖並 ALTER 主表與 _deleted 表；進行中的匯入在切換前會失敗，需重新上傳。
            </div>
            <div class="row end">
              <button class="btn primary" type="submit" :disabled="!formValid">送出…</button>
            </div>
          </form>
        </section>

        <section class="card stack">
          <div class="row">
            <h2 class="grow">期別</h2>
            <button class="btn sm" type="button" :disabled="periodsLoading" @click="loadPeriods">重新整理</button>
          </div>
          <div v-if="periodsError" class="notice error">{{ periodsError }}</div>
          <div v-else-if="periodsLoading" class="muted">載入中…</div>
          <div v-else-if="!periods.length" class="muted">尚無期別資料。</div>
          <div v-else class="table-wrap">
            <table class="plain">
              <thead><tr><th>期別</th><th>估計筆數</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="p in periods" :key="p.yyyymm">
                  <td class="mono">{{ p.yyyymm }}</td>
                  <td>{{ fmt(p.rows_estimate) }}</td>
                  <td><button class="btn danger sm" type="button" @click="ask({ kind: 'drop', yyyymm: p.yyyymm })">整期刪除</button></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="small muted">估計筆數取自統計資訊，非精確值。</div>
        </section>
      </template>
    </template>

    <section class="card stack">
      <h2>建立資料集</h2>
      <div class="muted small">上傳 JSON 定義檔（欄位：name、partition_key、description、columns[]；可由 tools/gen_data.py 的 --def-out 產生）。</div>
      <input type="file" accept=".json,application/json" aria-label="資料集定義檔" @change="onFile" />
      <div v-if="defError" class="notice error">{{ defError }}</div>
      <div v-if="defSummary" class="stack">
        <div class="table-wrap">
          <table class="plain">
            <tbody>
              <tr><th>檔案</th><td>{{ fileName }}</td></tr>
              <tr><th>資料集名稱</th><td class="mono">{{ defSummary.name }}</td></tr>
              <tr><th>欄位數</th><td>{{ defSummary.cols }}</td></tr>
              <tr><th>分割鍵</th><td class="mono">{{ defSummary.partition_key }}</td></tr>
              <tr><th>主鍵</th><td class="mono">{{ defSummary.pks.join(", ") || "（無）" }}</td></tr>
            </tbody>
          </table>
        </div>
        <div v-if="defSummary.exists" class="notice warn">已有同名資料集 {{ defSummary.name }}，送出時後端可能拒絕。</div>
        <div class="row end"><button class="btn primary" type="button" @click="ask({ kind: 'create' })">建立資料集…</button></div>
      </div>
    </section>

    <DatasetsConfirmDialog :open="!!pending" :title="dlgTitle" :confirm-label="dlgLabel" :busy="busy" :error="dlgError"
                           :danger="pending?.kind === 'disable' || pending?.kind === 'drop'"
                           :expect-text="pending?.kind === 'drop' ? String(pending.yyyymm) : undefined"
                           @confirm="doConfirm" @cancel="closeDlg">
      <template v-if="pending?.kind === 'create' && defSummary">
        <p>即將建立資料集 <code>{{ defSummary.name }}</code>（{{ defSummary.cols }} 欄，分割鍵 {{ defSummary.partition_key }}，主鍵 {{ defSummary.pks.join(", ") || "無" }}）。</p>
      </template>
      <template v-else-if="pending?.kind === 'add'">
        <p>在 <code>{{ selected }}</code> 新增欄位 <code>{{ form.code }}</code>（{{ dataType }}{{ form.required ? "，必填" : "" }}{{ form.checksum ? "，驗算" : "" }}）。</p>
        <div class="notice warn">新增欄位會讓進行中的匯入在切換前失敗、需重新上傳。</div>
      </template>
      <template v-else-if="pending?.kind === 'disable'">
        <p>停用 <code>{{ selected }}</code> 的欄位 <code>{{ pending.code }}</code>。</p>
        <div class="notice info">只停用不刪除；停用後查詢與匯入都不再使用此欄。</div>
      </template>
      <template v-else-if="pending?.kind === 'drop'">
        <p>整期刪除 <code>{{ selected }}</code> 的 <strong>{{ pending.yyyymm }}</strong> 期。</p>
        <div class="notice warn">此期分割會被 DETACH 並改名為 _bak，保留 7 天內可還原；期間內該期資料不再出現於查詢。</div>
      </template>
    </DatasetsConfirmDialog>
  </div>
</template>

<style scoped>
.ds-list .btn { max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
.ds-grid { height: 420px; min-width: 0; }
.ds-focus { padding: 10px 12px; background: var(--surface-2); }
.ds-err { color: var(--danger); }
:deep(.ds-disabled) { color: var(--text-muted); opacity: .6; }
</style>

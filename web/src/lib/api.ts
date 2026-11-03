// 後端 API client（對應 app/main.py）。所有請求同源、帶 cookie（cqp_sid 用於每 session 名額）。

export class ApiError extends Error {
  constructor(public status: number, message: string, public type?: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {};
  let payload: BodyInit | undefined;
  if (body instanceof Blob || body instanceof ArrayBuffer) {
    payload = body as BodyInit;
    headers["content-type"] = "application/octet-stream";
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["content-type"] = "application/json";
  }
  const res = await fetch(path, { method, body: payload, headers, credentials: "same-origin", ...init });
  if (!res.ok) throw await toError(res);
  const ct = res.headers.get("content-type") ?? "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export async function toError(res: Response): Promise<ApiError> {
  let msg = `HTTP ${res.status}`;
  let type: string | undefined;
  try {
    const j = await res.json();
    if (typeof j.error === "string") msg = j.error;
    else if (Array.isArray(j.detail)) msg = j.detail.map((d: { loc?: unknown[]; msg?: string }) =>
      `${(d.loc ?? []).join(".")}：${d.msg ?? ""}`).join("；");
    else if (typeof j.detail === "string") msg = j.detail;
    type = j.type;
  } catch { /* 非 JSON 回應 */ }
  if (res.status === 403) msg = "此操作只允許指定網段的電腦執行（寫入防護）";
  if (res.status === 429 && msg.startsWith("HTTP")) msg = "目前查詢人數已滿，請稍後再試";
  return new ApiError(res.status, msg, type);
}

// ---------------------------------------------------------------- 型別
export interface ColumnDef {
  code: string;
  data_type: string;
  display_name: string;
  aliases: string[];
  required: boolean;
  is_pk: boolean;
  pk_order: number;
  checksum: boolean;
  group_name: string;
  sort_order: number;
  description: string;
  disabled: boolean;
}
export interface DatasetSummary { dataset_id: number; name: string; schema_version: number; description: string; columns: number }
export interface Dataset {
  dataset_id: number;
  name: string;
  partition_key: string;
  schema_version: number;
  description: string;
  columns: ColumnDef[];
}
export interface PeriodInfo { yyyymm: number; rows_estimate: number }

export type LoadMode = "replace" | "upsert" | "delete";
export type LoadStatus = "UPLOADED" | "STAGED" | "VALIDATED" | "BUILT" | "SWAPPED" | "APPLIED" | "PREVIEWED"
  | "CLEANED" | "FAILED" | "CANCELLED";
export interface Load {
  load_id: string;
  dataset_id: number;
  dataset_name: string | null;
  yyyymm: number;
  mode: LoadMode | "drop";
  upload_id: string | null;
  filename: string | null;
  status: LoadStatus;
  error: string | null;
  rows_csv: number | null;
  rows_staged: number | null;
  rows_inserted: number | null;
  rows_updated: number | null;
  rows_deleted: number | null;
  rows_matched: number | null;
  rows_unmatched: number | null;
  checksums: string | null;
  detail: string | null;
  bak_table: string | null;
  operator_note: string | null;
  client_ip: string | null;
  created_at: string;
  swapped_at: string | null;
  cleaned_at: string | null;
}

export type WhereOp = "=" | "in" | "between" | "like_prefix" | "is_null" | "not_null";
export interface QuerySpec {
  from: string;
  joins?: { table: string; type: "inner" | "left"; on: [string, string][] }[];
  select: string[];
  calc?: { name: string; expr: string }[];
  where?: (string | number | unknown[])[][];
  order?: [string, "asc" | "desc", "nulls_first" | "nulls_last"][];
}
export interface QueryOptions { offset?: number; limit?: number; allow_many_to_many?: boolean; job_id?: string }
export interface PreviewResult {
  job_id: string;
  columns: string[];
  rows: (string | number | null)[][];
  has_more: boolean;
  estimate: { rows: number; cost: number; seq_scans: string[] };
  hints: string[];
}

const enc = encodeURIComponent;
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// ---------------------------------------------------------------- 資料集
export const api = {
  health: () => request<{ ok: boolean }>("GET", "/api/health"),
  datasets: () => request<DatasetSummary[]>("GET", "/api/datasets"),
  dataset: (name: string) => request<Dataset>("GET", `/api/datasets/${encodeURIComponent(name)}`),
  periods: (name: string) => request<PeriodInfo[]>("GET", `/api/datasets/${encodeURIComponent(name)}/periods`),
  createDataset: (def: unknown, note = "") =>
    request<Dataset>("POST", `/api/datasets?operator_note=${encodeURIComponent(note)}`, def),
  addColumn: (name: string, col: Partial<ColumnDef> & { code: string; data_type: string; operator_note?: string }) =>
    request<Dataset>("POST", `/api/datasets/${encodeURIComponent(name)}/columns`, col),
  disableColumn: (name: string, code: string, note = "") =>
    request<Dataset>("POST", `/api/datasets/${encodeURIComponent(name)}/columns/${encodeURIComponent(code)}/disable?operator_note=${encodeURIComponent(note)}`),

  // 上傳
  createUpload: (filename: string, total_bytes: number, chunk_sha256: string[]) =>
    request<{ upload_id: string; total_chunks: number; chunk_size: number }>("POST", "/api/upload",
      { filename, total_bytes, chunk_sha256 }),
  uploadStatus: (id: string) => request<{ upload_id: string; status: string; missing: number[]; chunk_sha256: string[] }>(
    "GET", `/api/upload/${enc(id)}`),
  putChunk: (id: string, seq: number, data: Blob, signal?: AbortSignal) =>
    request<{ received: number; missing: number[] }>("PUT", `/api/upload/${enc(id)}/${seq}`, data, { signal }),
  completeUpload: (id: string) => request<{ upload_id: string; sha256: string }>("POST", `/api/upload/${enc(id)}/complete`),

  // 匯入
  createLoad: (b: { dataset: string; yyyymm: number; mode: LoadMode; upload_id: string; operator_note?: string }) =>
    request<{ load_id: string }>("POST", "/api/loads", b),
  load: (id: string) => request<Load>("GET", `/api/loads/${enc(id)}`),
  loads: (dataset?: string, limit = 50) =>
    request<Load[]>("GET", `/api/loads?limit=${limit}${dataset ? `&dataset=${encodeURIComponent(dataset)}` : ""}`),
  errorReportUrl: (id: string) => `/api/loads/${enc(id)}/errors`,
  retryLoad: (id: string) => request<{ ok: boolean }>("POST", `/api/loads/${enc(id)}/retry`),
  cancelLoad: (id: string) => request<{ ok: boolean }>("POST", `/api/loads/${enc(id)}/cancel`),
  confirmDelete: (id: string, operator: string, confirm_large: boolean) =>
    request<Load>("POST", `/api/loads/${enc(id)}/confirm-delete`, { operator, confirm_large }),
  dropPeriod: (dataset: string, yyyymm: number, operator: string) =>
    request<{ load_id: string }>("POST", "/api/delete-period", { dataset, yyyymm, operator }),

  // 查詢
  preview: (spec: QuerySpec, o: QueryOptions = {}) =>
    request<PreviewResult>("POST", "/api/query/preview", { spec, ...o }),
  count: (spec: QuerySpec, o: QueryOptions = {}) =>
    request<{ count: number }>("POST", "/api/query/count", { spec, ...o }),
  copyTsv: (spec: QuerySpec, o: QueryOptions = {}) => request<string>("POST", "/api/query/copy", { spec, ...o }),
  cancelQuery: (jobId: string) => request<{ cancelled: boolean }>("POST", `/api/query/${enc(jobId)}/cancel`),
  saveQuery: (name: string, spec: QuerySpec, note = "") =>
    request<{ query_id: number }>("POST", "/api/saved-queries", { name, spec, note }),
  savedQueries: () => request<{ query_id: number; name: string; note: string; created_at: string }[]>("GET", "/api/saved-queries"),
  loadQuery: (id: number) =>
    request<{ query_id: number; name: string; spec: QuerySpec; stale_columns: string[] }>("GET", `/api/saved-queries/${id}`),
};

/**
 * 匯出 CSV：以表單 POST 到隱藏 iframe，由瀏覽器直接串流下載（fetch + Blob 會把整個檔案留在記憶體）。
 * 成功下載時 iframe 不會載入頁面；伺服器回錯誤（JSON）時 iframe 會 load，讀出錯誤訊息交給 onError。
 */
export function exportCsv(
  spec: QuerySpec,
  o: { encoding: "utf-8-sig" | "big5"; gzip: boolean; allow_many_to_many: boolean },
  onError: (message: string) => void,
) {
  const name = `cqp-export-${Date.now()}`;
  const iframe = document.createElement("iframe");
  iframe.name = name;
  iframe.style.display = "none";
  // 插入空白 iframe 時瀏覽器會先觸發一次 about:blank 的 load；送出表單前的 load 一律忽略，
  // 否則會在伺服器還沒回應時就移除 iframe，把下載中斷。
  let submitted = false;
  iframe.addEventListener("load", () => {
    if (!submitted) return;
    let href = "";
    try { href = iframe.contentWindow?.location.href ?? ""; } catch { /* 忽略 */ }
    if (href === "about:blank") return;
    let msg = "";
    try {
      const text = iframe.contentDocument?.body?.textContent?.trim() ?? "";
      if (text) {
        try {
          const j = JSON.parse(text);
          msg = typeof j.error === "string" ? j.error
            : Array.isArray(j.detail) ? j.detail.map((d: { msg?: string }) => d.msg ?? "").join("；")
            : text;
        } catch { msg = text.slice(0, 300); }
      }
    } catch { /* 跨來源或已下載 */ }
    // 只有伺服器回錯誤頁時 iframe 才會 load（成功的下載不會）；讀完錯誤再移除
    if (msg) {
      onError(msg);
      iframe.remove();
    }
  });
  document.body.appendChild(iframe);
  const form = document.createElement("form");
  form.method = "POST";
  form.action = "/api/query/export-form";
  form.target = name;
  form.style.display = "none";
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "payload";
  input.value = JSON.stringify({ spec, ...o });
  form.appendChild(input);
  document.body.appendChild(form);
  submitted = true;
  form.submit();
  form.remove();
  // 下載成功的 iframe 不會觸發 load；匯出逾時上限（30 分鐘）之後才清除，避免中斷長時間下載
  setTimeout(() => iframe.remove(), 31 * 60 * 1000);
}

export function isTerminal(s: LoadStatus) {
  return s === "CLEANED" || s === "FAILED" || s === "CANCELLED";
}

export const STATUS_LABEL: Record<LoadStatus, string> = {
  UPLOADED: "已上傳", STAGED: "已載入暫存", VALIDATED: "驗證通過", BUILT: "新分割已建立",
  SWAPPED: "已切換", APPLIED: "已套用", PREVIEWED: "待確認刪除", CLEANED: "完成", FAILED: "失敗",
  CANCELLED: "已取消",
};

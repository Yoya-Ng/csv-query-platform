// 分塊上傳（不依賴 Vue）。規則對應 app/services/upload.py：
// 每塊 16MB、建立時送全部區塊 SHA-256、最後一塊 = total − 16MB×(n−1)、PUT 大小必須精確。
import { api, ApiError } from "./api";

export const CHUNK_SIZE = 16 * 1024 * 1024;
export const MAX_FILE_BYTES = 50 * 1024 ** 3;
const CONCURRENCY = 3;
const MAX_RETRY = 3;
const STORE_KEY = "cqp-uploads";

export interface Progress { phase: "hash" | "upload" | "complete"; done: number; total: number }
export interface UploadOptions {
  onProgress: (p: Progress) => void;
  signal?: AbortSignal;
  resumeId?: string;
}

/** 純函式：回傳每塊 [start, end)。區塊數 = ceil(size/chunkSize)，最後一塊 = size − chunkSize×(n−1)。 */
export function chunkRanges(size: number, chunkSize: number = CHUNK_SIZE): [number, number][] {
  if (!Number.isFinite(size) || size < 0 || !Number.isInteger(chunkSize) || chunkSize <= 0) {
    throw new RangeError("size 或 chunkSize 不合法");
  }
  const out: [number, number][] = [];
  for (let s = 0; s < size; s += chunkSize) out.push([s, Math.min(s + chunkSize, size)]);
  return out;
}

/** 副檔名與大小檢查；回傳錯誤訊息，通過回 null。 */
export function validateFile(file: { name: string; size: number }): string | null {
  const n = file.name.toLowerCase();
  if (!(n.endsWith(".csv") || n.endsWith(".csv.gz"))) return "只接受 .csv 或 .csv.gz 檔案";
  if (file.size <= 0) return "檔案是空的（0 bytes）";
  if (file.size > MAX_FILE_BYTES) return "檔案超過單檔上限 50GB";
  return null;
}

function hex(buf: ArrayBuffer): string {
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

function abortError() {
  return new DOMException("上傳已中止", "AbortError");
}
function checkAbort(signal?: AbortSignal) {
  if (signal?.aborted) throw abortError();
}

// ---- 續傳紀錄（localStorage，失敗時靜默） ----
interface StoredUpload { upload_id: string; name: string; size: number; lastModified: number }
function fileKey(f: File) { return `${f.name}|${f.size}|${f.lastModified}`; }
function readStore(): Record<string, StoredUpload> {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) ?? "{}") ?? {}; } catch { return {}; }
}
function writeStore(m: Record<string, StoredUpload>) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(m)); } catch { /* 私密模式或容量 */ }
}
function saveRecord(f: File, upload_id: string) {
  const m = readStore();
  m[fileKey(f)] = { upload_id, name: f.name, size: f.size, lastModified: f.lastModified };
  writeStore(m);
}
function clearRecord(f: File) {
  const m = readStore();
  delete m[fileKey(f)];
  writeStore(m);
}
/** 取得此檔案先前未完成的 upload_id（若有）。 */
export function findResumeId(f: File): string | undefined {
  return readStore()[fileKey(f)]?.upload_id;
}

async function hashChunks(file: File, signal: AbortSignal | undefined, onProgress: (p: Progress) => void) {
  if (typeof crypto === "undefined" || !crypto.subtle) {
    throw new Error("此瀏覽器環境無法計算 SHA-256（crypto.subtle 只在 HTTPS 或 localhost 可用）。請改用 https:// 或 http://localhost 開啟本頁。");
  }
  const ranges = chunkRanges(file.size);
  const hashes: string[] = [];
  let done = 0;
  onProgress({ phase: "hash", done: 0, total: file.size });
  for (const [s, e] of ranges) {
    checkAbort(signal);
    const buf = await file.slice(s, e).arrayBuffer(); // 一次只保留一塊
    hashes.push(hex(await crypto.subtle.digest("SHA-256", buf)));
    done += e - s;
    onProgress({ phase: "hash", done, total: file.size });
  }
  return hashes;
}

function sleep(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(t); reject(abortError()); }, { once: true });
  });
}

async function putWithRetry(id: string, file: File, seq: number, signal?: AbortSignal) {
  const [s, e] = chunkRanges(file.size)[seq];
  for (let attempt = 0; ; attempt++) {
    checkAbort(signal);
    try {
      await api.putChunk(id, seq, file.slice(s, e), signal);
      return;
    } catch (err) {
      if ((err as Error).name === "AbortError") throw err;
      const st = err instanceof ApiError ? err.status : 0;
      // 4xx（SHA 不符、大小錯誤、上傳不存在、權限）重試也不會成功；只重試網路錯誤、5xx、408、429
      const retryable = st === 0 || st >= 500 || st === 408 || st === 429;
      if (!retryable || attempt >= MAX_RETRY) throw err;
      await sleep(500 * 2 ** attempt, signal); // 0.5s、1s、2s
    }
  }
}

async function sendChunks(id: string, file: File, missing: number[], signal: AbortSignal | undefined,
                          onProgress: (p: Progress) => void) {
  const ranges = chunkRanges(file.size);
  const todo = [...missing].sort((a, b) => a - b);
  const total = todo.reduce((n, i) => n + (ranges[i][1] - ranges[i][0]), 0);
  let done = 0, next = 0;
  onProgress({ phase: "upload", done: 0, total });
  const ctl = new AbortController();
  const onAbort = () => ctl.abort();
  if (signal?.aborted) ctl.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  try {
    const worker = async () => {
      while (next < todo.length) {
        const seq = todo[next++];
        await putWithRetry(id, file, seq, ctl.signal);
        done += ranges[seq][1] - ranges[seq][0];
        onProgress({ phase: "upload", done, total });
      }
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, todo.length) }, async () => {
      try { await worker(); } catch (e) { ctl.abort(); throw e; } // 一個失敗就中止其他
    }));
  } finally {
    signal?.removeEventListener("abort", onAbort);
  }
}

export async function uploadFile(file: File, opts: UploadOptions): Promise<{ upload_id: string; sha256: string }> {
  const { onProgress, signal } = opts;
  const bad = validateFile(file);
  if (bad) throw new Error(bad);
  checkAbort(signal);

  // 一律先計算本機每塊雜湊：續傳時用來確認伺服器上已收的區塊仍是這個檔案的內容
  // （只比檔名＋大小＋修改時間不夠，同名同大小的另一版本會被當成同一檔而匯入舊內容）
  const hashes = await hashChunks(file, signal, onProgress);
  checkAbort(signal);

  let id: string | undefined;
  let missing: number[] = [];
  const cand = opts.resumeId ?? findResumeId(file);
  if (cand) {
    try {
      const st = await api.uploadStatus(cand);
      const same = st.chunk_sha256.length === hashes.length && st.chunk_sha256.every((h, i) => h === hashes[i]);
      if (same && st.status === "OPEN") { id = cand; missing = st.missing; }
      // 已組檔完成但尚未建立匯入（例如上次建立匯入失敗）：內容相同就直接沿用
      else if (same && st.status === "COMPLETE") {
        onProgress({ phase: "complete", done: 1, total: 1 });
        return { upload_id: cand, sha256: "" };
      }
    } catch { /* 404 或其他：重新開始 */ }
    if (!id) clearRecord(file);
  }

  if (!id) {
    const created = await api.createUpload(file.name, file.size, hashes);
    id = created.upload_id;
    missing = hashes.map((_, i) => i);
    saveRecord(file, id);
  }

  try {
    await sendChunks(id, file, missing, signal, onProgress);
  } catch (e) {
    if (e instanceof ApiError && e.status === 400 && e.message.includes("SHA-256")) {
      clearRecord(file);    // 上傳期間檔案被修改：不要留下會一直失敗的續傳紀錄
      throw new Error("上傳期間檔案內容已變更（區塊雜湊不符），請確認檔案後重新開始。");
    }
    throw e;
  }
  checkAbort(signal);
  onProgress({ phase: "complete", done: 0, total: 1 });
  const r = await api.completeUpload(id);
  onProgress({ phase: "complete", done: 1, total: 1 });
  // 不在這裡清除續傳紀錄：建立匯入成功後由呼叫端呼叫 clearResume()，
  // 建立失敗時可沿用已完成的上傳，不必重傳整個檔案
  return r;
}

/** 匯入建立成功後清除續傳紀錄。 */
export function clearResume(file: File): void {
  clearRecord(file);
}

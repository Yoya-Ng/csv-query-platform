// 查詢建構器的狀態 → 後端 JSON 查詢規格（SPEC 9.1）。純函式，不依賴 Vue。
import type { ColumnDef, Dataset, QuerySpec, WhereOp } from "./api";

export interface JoinState { table: string; type: "inner" | "left"; on: { left: string; right: string }[] }
export interface FilterState { col: string; op: WhereOp; value: string; value2: string; list: string }
export interface CalcState { name: string; expr: string }
export interface OrderState { ref: string; dir: "asc" | "desc"; nulls: "nulls_first" | "nulls_last" }
export interface BuilderState {
  from: string;
  joins: JoinState[];
  select: string[];          // "表.欄"
  calc: CalcState[];
  filters: FilterState[];
  order: OrderState[];
}

export const MAX_OUTPUT_COLS = 50;
export const MAX_IN_VALUES = 10_000;
export const CALC_NAME_RE = /^[a-z][a-z0-9_]{0,29}$/;

export function emptyState(): BuilderState {
  return { from: "", joins: [], select: [], calc: [], filters: [], order: [] };
}

export function emptyFilter(col = ""): FilterState {
  return { col, op: "=", value: "", value2: "", list: "" };
}

/** 貼上的整欄（Excel 換行或 tab 分隔）→ 去前後空白、去重複的值清單。
 *  不以逗號分隔：值本身可能含逗號（例如地址、名稱），拆開會靜默少資料。 */
export function parseList(text: string): string[] {
  const seen = new Set<string>();
  for (const raw of text.split(/[\r\n\t]+/)) {
    const v = raw.trim();
    if (v) seen.add(v);
  }
  return [...seen];
}

export function tablesInQuery(s: BuilderState): string[] {
  return [s.from, ...s.joins.map((j) => j.table)].filter(Boolean);
}

/** 依兩表都有的主鍵欄位建議 join 條件（例如 yyyymm + policy_no）。 */
export function suggestOn(left: Dataset, right: Dataset): { left: string; right: string }[] {
  const rightCodes = new Set(right.columns.filter((c) => !c.disabled).map((c) => c.code));
  const keys = left.columns.filter((c) => c.is_pk && !c.disabled && rightCodes.has(c.code));
  const picked = keys.length ? keys : left.columns.filter((c) => c.code === left.partition_key);
  return picked.map((c) => ({ left: `${left.name}.${c.code}`, right: c.code }));
}

export class BuildError extends Error {}

/** 組成後端規格；只做前端能確定的檢查（其餘交給後端白名單，錯誤訊息以後端為準）。 */
export function buildSpec(s: BuilderState): QuerySpec {
  if (!s.from) throw new BuildError("請先選擇主資料集");
  if (!s.select.length && !s.calc.length) throw new BuildError("請至少選一個欄位或計算欄位");
  if (s.select.length + s.calc.length > MAX_OUTPUT_COLS)
    throw new BuildError(`輸出欄位最多 ${MAX_OUTPUT_COLS} 欄（目前 ${s.select.length + s.calc.length} 欄）`);
  const spec: QuerySpec = { from: s.from, select: [...s.select] };
  if (s.joins.length) {
    spec.joins = s.joins.map((j, i) => {
      if (!j.table) throw new BuildError(`第 ${i + 1} 個 join 未選資料集`);
      const on = j.on.filter((p) => p.left && p.right);
      if (!on.length) throw new BuildError(`與 ${j.table} 的 join 至少需要一組對應欄位`);
      return { table: j.table, type: j.type, on: on.map((p) => [p.left, `${j.table}.${p.right}`] as [string, string]) };
    });
  }
  if (s.calc.length) {
    spec.calc = s.calc.map((c, i) => {
      if (!CALC_NAME_RE.test(c.name)) throw new BuildError(`第 ${i + 1} 個計算欄位名稱需為小寫英數底線、字母開頭`);
      if (!c.expr.trim()) throw new BuildError(`計算欄位 ${c.name} 的運算式是空的`);
      return { name: c.name, expr: c.expr.trim() };
    });
  }
  const where: (string | number | unknown[])[][] = [];
  s.filters.forEach((f, i) => {
    if (!f.col) return;
    const at = `第 ${i + 1} 個篩選（${f.col}）`;
    switch (f.op) {
      case "is_null":
      case "not_null":
        where.push([f.col, f.op]);
        break;
      case "in": {
        const vals = parseList(f.list);
        if (!vals.length) throw new BuildError(`${at}：請貼上至少一個值`);
        if (vals.length > MAX_IN_VALUES) throw new BuildError(`${at}：最多 ${MAX_IN_VALUES} 個值（目前 ${vals.length}）`);
        where.push([f.col, "in", vals]);
        break;
      }
      case "between":
        if (!f.value.trim() || !f.value2.trim()) throw new BuildError(`${at}：請填起訖值`);
        where.push([f.col, "between", [f.value.trim(), f.value2.trim()]]);
        break;
      default:
        if (!f.value.trim()) throw new BuildError(`${at}：請填值`);
        where.push([f.col, f.op, f.value.trim()]);
    }
  });
  if (where.length) spec.where = where;
  if (s.order.length) spec.order = s.order.filter((o) => o.ref).map((o) => [o.ref, o.dir, o.nulls]);
  return spec;
}

/** 從後端規格還原建構器狀態（載入「我的查詢」）。 */
export function stateFromSpec(spec: QuerySpec): BuilderState {
  const st = emptyState();
  st.from = spec.from;
  st.joins = (spec.joins ?? []).map((j) => ({
    table: j.table, type: j.type,
    on: j.on.map(([l, r]) => ({
      left: l.includes(".") ? l : `${spec.from}.${l}`,
      right: r.includes(".") ? r.slice(r.indexOf(".") + 1) : r,
    })),
  }));
  st.select = [...spec.select];
  st.calc = (spec.calc ?? []).map((c) => ({ ...c }));
  st.filters = (spec.where ?? []).map((w) => {
    const f = emptyFilter(String(w[0]));
    f.op = w[1] as WhereOp;
    if (f.op === "in") f.list = (w[2] as unknown[]).map(String).join("\n");
    else if (f.op === "between") [f.value, f.value2] = (w[2] as unknown[]).map(String);
    else if (w.length > 2) f.value = String(w[2]);
    return f;
  });
  st.order = (spec.order ?? []).map(([ref, dir, nulls]) => ({ ref, dir, nulls }));
  return st;
}

export function isNumericType(c: ColumnDef | undefined): boolean {
  return !!c && /^(integer|bigint|numeric)/.test(c.data_type);
}

"""由真實 CSV 產生資料集定義 JSON 草稿（表頭 → 欄位；抽樣推斷型別）。

只讀表頭與前 N 列（串流，記憶體只保存每欄統計，不隨筆數成長），輸出：
  (a) 定義 JSON 草稿：格式同 app/catalog.py 的 DatasetDef / ColumnDef，可在「資料集與欄位」上傳
  (b) 推斷報告（stdout）：每欄的推斷型別、樣本非空筆數、最大長度、需人工確認的項目

推斷結果只是草稿：抽樣範圍外可能出現更長的字串、更多小數位或非數字值，匯入時會在驗證階段報
「格式不符」，屆時調整定義後重建資料集。主鍵、驗算欄、必填欄不自動猜，由參數指定。

讀檔規則與平台匯入一致（app/services/csvfile.py）：UTF-8（可帶 BOM）、逗號分隔、首列表頭、可為 gzip。
缺失值：空欄（''）視為缺失；NA、N/A 等是一般字串值，不視為缺失。
型別判斷比平台驗證更嚴格（例如數字前後有空白、整數部分有前導零 → 判為文字），寧可推成文字也不推成會被拒絕的型別。

用法範例：
  python tools/def_from_csv.py data/d005_202610.csv --name d005 --pk policy_no,cov_no --out data/d005.json
  python tools/def_from_csv.py data/x.csv.gz --name x --partition-key load_period \\
      --rename 保單號碼=policy_no --pk policy_no --checksum amt --out data/x.json
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import zlib
from datetime import date
from pathlib import Path

# 與 app/catalog.py 一致（工具不 import app，避免需要平台環境變數與依賴）
IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
NAME_MAX = 22            # 資料集名稱上限（後綴預留，見 DatasetDef.validate）
NUMERIC_MAX_P = 38
VARCHAR_MAX = 4000
DISPLAY_MAX = 200         # meta_column.display_name 為 String(200)
INT64_DIGITS = 19
SYSTEM_COLS = {"tableoid", "xmin", "cmin", "xmax", "cmax", "ctid"}   # PostgreSQL 系統欄，CREATE TABLE 會拒絕

# 一律用 fullmatch（Python 的 $ 會匹配結尾換行，比 PostgreSQL 的 ~ '^…$' 寬鬆）
INT_RE = re.compile(r"^[+-]?[0-9]+$")
NUM_RE = re.compile(r"^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$")     # 同平台 numeric 驗證
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")              # 平台 date 只接受 YYYY-MM-DD
DATE_LIKE_RE = re.compile(r"^[0-9]{4}[/.][0-9]{1,2}[/.][0-9]{1,2}$|^[0-9]{8}$")
INT32 = (-(2 ** 31), 2 ** 31 - 1)
INT64 = (-(2 ** 63), 2 ** 63 - 1)
VARCHAR_LADDER = (10, 20, 50, 100, 255, 500, 1000, 2000, VARCHAR_MAX)


class Blocking(Exception):
    """無法產生可上傳定義的問題（不寫出 JSON）。"""


class ColStats:
    __slots__ = ("nonnull", "is_int", "is_num", "is_date", "date_like", "lo", "hi", "int_digits", "scale", "max_len")

    def __init__(self) -> None:
        self.nonnull = 0
        self.is_int = self.is_num = self.is_date = self.date_like = True
        self.lo: int | None = None
        self.hi: int | None = None
        self.int_digits = 0      # numeric：整數部分最大位數（去前導正負號）
        self.scale = 0           # numeric：小數部分最大位數
        self.max_len = 0

    def add(self, v: str) -> None:
        if v == "":
            return
        self.nonnull += 1
        n = len(v)
        if n > self.max_len:
            self.max_len = n
        if self.is_int or self.is_num:
            body = v.lstrip("+-")
            ip, _, fp = body.partition(".")
            if len(ip) > 1 and ip[0] == "0":         # 前導零（代碼類，如 00123）→ 當文字，避免轉數字後遺失
                self.is_int = self.is_num = False
            else:
                if self.is_int:
                    if len(body) <= INT64_DIGITS and INT_RE.fullmatch(v):   # 超過 19 位不可能是 bigint，也避免 int() 位數上限
                        x = int(v)
                        self.lo = x if self.lo is None or x < self.lo else self.lo
                        self.hi = x if self.hi is None or x > self.hi else self.hi
                    else:
                        self.is_int = False
                if self.is_num:
                    if NUM_RE.fullmatch(v):
                        self.int_digits = max(self.int_digits, len(ip.lstrip("0")))
                        self.scale = max(self.scale, len(fp))
                    else:
                        self.is_num = False
        if self.is_date:
            if not DATE_RE.fullmatch(v):
                self.is_date = False
            else:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    self.is_date = False
        if self.date_like and not DATE_LIKE_RE.fullmatch(v):
            self.date_like = False


def infer_type(st: ColStats, headroom: int) -> tuple[str, list[str]]:
    """回傳 (data_type, 備註)。"""
    notes: list[str] = []
    if st.nonnull == 0:
        return "varchar(255)", ["樣本全為空值，型別預設 varchar(255)，請確認"]
    if st.is_date:
        return "date", notes
    if st.is_int:
        k = 10 ** headroom   # 與 numeric 相同的預留位數，避免樣本外的值超出 integer
        if INT32[0] <= st.lo * k and st.hi * k <= INT32[1]:
            if st.date_like:
                notes.append("8 位數字，若是日期（YYYYMMDD）平台不會當 date，需轉成 YYYY-MM-DD")
            return "integer", notes
        if INT64[0] <= st.lo * k and st.hi * k <= INT64[1]:
            if INT32[0] <= st.lo and st.hi <= INT32[1]:
                notes.append(f"樣本值在 integer 範圍內，但預留 {headroom} 位後改用 bigint")
            return "bigint", notes
    if st.is_num:
        need = max(st.int_digits, 1) + st.scale
        if need <= NUMERIC_MAX_P:
            p = min(need + headroom, NUMERIC_MAX_P)
            if p < need + headroom:
                notes.append(f"整數部分預留位數受 numeric 上限 {NUMERIC_MAX_P} 限制，只多留 {p - need} 位")
            if st.scale:
                notes.append(f"小數位以樣本最大 {st.scale} 位推定；超過會被拒絕（平台不四捨五入）")
            return f"numeric({p},{st.scale})", notes
        notes.append(f"數字位數超過 numeric 上限 {NUMERIC_MAX_P}，改用文字")
    if st.date_like:
        notes.append("看起來是日期但非 YYYY-MM-DD，平台會當文字；要用 date 須先轉檔")
    if st.max_len > VARCHAR_MAX:
        raise Blocking(f"最大長度 {st.max_len} 超過 varchar 上限 {VARCHAR_MAX}")
    n = next(x for x in VARCHAR_LADDER if x >= st.max_len)
    return f"varchar({n})", notes


def to_code(header: str) -> str | None:
    s = header.strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if s and s[0].isdigit():
        s = "c_" + s
    s = s[:30].rstrip("_")
    return s if IDENT_RE.fullmatch(s) else None


def _split_list(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def open_csv(path: Path):
    with path.open("rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    if gz:
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="", errors="strict")
    return path.open("r", encoding="utf-8-sig", newline="", errors="strict")


def build(a: argparse.Namespace) -> tuple[dict, list[list[str]], list[str], dict]:
    """回傳 (定義, 報告列, 全域提醒, 摘要)。有 Blocking 問題時丟 Blocking。"""
    problems: list[str] = []
    if not IDENT_RE.fullmatch(a.name) or len(a.name) > NAME_MAX:
        problems.append(f"--name 需小寫英數底線、字母開頭、≤{NAME_MAX} 字：{a.name!r}")
    if not IDENT_RE.fullmatch(a.partition_key):
        problems.append(f"--partition-key 不合法：{a.partition_key!r}")
    renames: dict[str, str] = {}
    for r in a.rename:
        old, sep, new = r.partition("=")
        if not sep or not old.strip() or not IDENT_RE.fullmatch(new.strip()):
            problems.append(f"--rename 格式需為 原表頭=code（code 小寫英數底線、字母開頭、≤30 字）：{r!r}")
            continue
        renames[old.strip().lower()] = new.strip()

    rows_read = bad_width = 0
    truncated = False
    try:
        with open_csv(a.csv) as f:
            rd = csv.reader(f)
            try:
                header = next(rd)
            except StopIteration:
                raise Blocking("檔案是空的") from None
            stats = [ColStats() for _ in header]
            width = len(header)
            for row in rd:
                if a.sample_rows and rows_read >= a.sample_rows:
                    truncated = True    # 確實還有下一列才算「只抽樣」
                    break
                rows_read += 1
                if not row and width == 1:
                    row = [""]
                if len(row) != width:
                    bad_width += 1
                    continue
                for st, v in zip(stats, row):
                    st.add(v)
    except UnicodeDecodeError as e:
        raise Blocking(f"不是 UTF-8：{e}") from None
    except csv.Error as e:
        raise Blocking(f"CSV 格式錯誤（第 {rows_read + 1} 列附近）：{e}") from None
    except (EOFError, zlib.error) as e:
        raise Blocking(f"gzip 檔案截斷或損毀（第 {rows_read + 1} 列附近）：{e}") from None
    except OSError as e:   # 含 gzip.BadGzipFile、找不到檔案、權限不足
        raise Blocking(f"無法讀取檔案：{e}") from None

    # ---- 表頭 → code
    codes: list[str] = []
    flags: list[list[str]] = [[] for _ in header]
    seen_keys: dict[str, int] = {}
    used: set[str] = set()
    for i, h in enumerate(header):
        key = h.strip().lower()
        if not key:
            problems.append(f"第 {i + 1} 欄表頭空白")
        elif key in seen_keys:
            problems.append(f"表頭重複（不分大小寫、去前後空白）：第 {seen_keys[key] + 1} 欄與第 {i + 1} 欄「{h}」")
        else:
            seen_keys[key] = i
        if "|" in h:
            problems.append(f"表頭含 '|'，無法作為別名：「{h}」")
        code = renames.pop(key, None) or to_code(h)
        if code is None:
            code = f"col_{i + 1:03d}"
            flags[i].append("表頭無法轉成 code，暫用流水號；請用 --rename 指定")
        if code in used:
            base, k = code, 2
            while code in used:
                suffix = f"_{k}"
                code = base[:30 - len(suffix)] + suffix
                k += 1
            flags[i].append("code 與其他欄重複，已加後綴；建議用 --rename 指定")
        if code in SYSTEM_COLS:
            problems.append(f"第 {i + 1} 欄 code「{code}」是 PostgreSQL 系統欄名，建立資料表會失敗，請用 --rename 改名")
        used.add(code)
        codes.append(code)
    for old in renames:
        problems.append(f"--rename 指定的表頭不存在：{old!r}")

    # 名稱 → 欄位序（code 與原表頭都可用）；同一名稱指到不同欄時視為歧義，不靜默擇一
    by_name: dict[str, set[int]] = {}
    for i, c in enumerate(codes):
        by_name.setdefault(c, set()).add(i)
    for i, h in enumerate(header):
        if h.strip():
            by_name.setdefault(h.strip().lower(), set()).add(i)

    def resolve(names: list[str], opt: str) -> list[int]:
        out = []
        for n in names:
            hit = by_name.get(n.lower(), set())
            if not hit:
                problems.append(f"{opt} 指定的欄位不存在：{n!r}")
            elif len(hit) > 1:
                problems.append(f"{opt} 的 {n!r} 同時對應多欄（code 與其他欄表頭同名），請用 --rename 區分")
            elif (i := next(iter(hit))) not in out:
                out.append(i)
        return out

    pk_idx = resolve(_split_list(a.pk), "--pk")
    chk_idx = resolve(_split_list(a.checksum), "--checksum")
    req_idx = resolve(_split_list(a.required), "--required")

    # ---- 欄位定義
    part = a.partition_key
    part_in_csv = part in codes
    cols: list[dict] = []
    report: list[list[str]] = []
    if not part_in_csv:
        cols.append(dict(code=part, data_type="integer", display_name="匯入期別", aliases=[], required=True,
                         is_pk=bool(pk_idx), pk_order=0, checksum=False, group_name="", sort_order=0,
                         description="匯入時由畫面期別帶入（不在 CSV 內）"))
        report.append([part, "（不在 CSV）", "integer", "-", "-", "獨立分割鍵，匯入時由畫面期別帶入"])

    pk_order = {i: k + 1 for k, i in enumerate(x for x in pk_idx if codes[x] != part)}
    for i, h in enumerate(header):
        st = stats[i]
        code = codes[i]
        notes = list(flags[i])
        try:
            dtype, tnotes = infer_type(st, a.numeric_headroom)
            notes += tnotes
        except Blocking as e:
            problems.append(f"欄位 {code}（「{h}」）：{e}")
            dtype = "varchar(4000)"
        is_part = code == part
        is_pk = is_part and bool(pk_idx) or i in pk_order
        required = is_part or is_pk or i in req_idx
        if is_part:
            if not (st.nonnull and st.is_int and INT32[0] <= st.lo and st.hi <= INT32[1]):
                problems.append(f"分割鍵欄 {code} 在樣本中不是 integer（推斷為 {dtype}）")
            dtype = "integer"
            notes = [n for n in notes if "改用 bigint" not in n]   # 分割鍵固定 integer，預留位數不適用
            notes.append("分割鍵：每列須等於匯入畫面輸入的期別")
        if i in chk_idx and not re.match(r"^(numeric|integer|bigint)", dtype):
            problems.append(f"--checksum 欄位 {code} 不是數值型別（推斷為 {dtype}）")
        if required and st.nonnull < rows_read - bad_width:
            notes.append(f"設為必填但樣本有 {rows_read - bad_width - st.nonnull} 筆空值，匯入會被拒絕")
        alias = h.strip()
        if len(alias) > DISPLAY_MAX:
            notes.append(f"表頭超過 {DISPLAY_MAX} 字，顯示名稱已截斷（別名保留完整表頭）")
        cols.append(dict(code=code, data_type=dtype, display_name=alias[:DISPLAY_MAX], aliases=[alias] if alias.lower() != code else [],
                         required=required, is_pk=is_pk, pk_order=0 if is_part else pk_order.get(i, 0),
                         checksum=i in chk_idx, group_name="", sort_order=i + 1, description=""))
        report.append([code, h, dtype, f"{st.nonnull}", f"{st.max_len}", "；".join(notes)])

    # 與平台 map_header 相同規則自檢：code／別名（不分大小寫、去前後空白）不可指到不同欄，
    # 否則上傳會成功，但每次匯入都被拒絕（metadata 別名衝突）
    lookup: dict[str, str] = {}
    for c in cols:
        for nm in (c["code"], *c["aliases"]):
            k = nm.strip().lower()
            if k in lookup and lookup[k] != c["code"]:
                problems.append(f"名稱「{nm}」同時對應 {lookup[k]} 與 {c['code']}（code 或別名衝突），請用 --rename 區分")
            lookup.setdefault(k, c["code"])

    if problems:
        raise Blocking("\n".join(f"- {p}" for p in problems))

    hints = []
    if not pk_idx:
        hints.append("未指定 --pk：沒有主鍵的資料集只能整期重傳（不能增修、刪除清單）")
    if bad_width:
        hints.append(f"樣本中有 {bad_width} 列欄數與表頭不符，未納入推斷；平台匯入時會拒絕這些列")
    if truncated:
        hints.append(f"只抽樣前 {a.sample_rows} 列；樣本外的值可能超出推斷的長度或精度（--sample-rows 0 可讀全檔）")
    defn = dict(name=a.name, partition_key=part, description=a.description, columns=cols)
    return defn, report, hints, dict(rows=rows_read, cols=len(cols))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="由 CSV 表頭與抽樣資料產生資料集定義 JSON 草稿")
    ap.add_argument("csv", type=Path, help="CSV 檔（UTF-8、逗號分隔、首列表頭；可為 gzip）")
    ap.add_argument("--name", required=True, help=f"資料集名稱（小寫英數底線、字母開頭、≤{NAME_MAX} 字）")
    ap.add_argument("--out", type=Path, required=True, help="定義 JSON 輸出路徑")
    ap.add_argument("--partition-key", default="yyyymm",
                    help="分割鍵欄名（預設 yyyymm）。CSV 有同名欄則以該欄為分割鍵；沒有則另建獨立分割鍵欄（匯入時由畫面期別帶入）")
    ap.add_argument("--pk", help="主鍵欄（逗號分隔，可用 code 或原表頭；分割鍵會自動放在第一個）")
    ap.add_argument("--checksum", help="匯入驗算 SUM 的欄位（逗號分隔，須為數值）")
    ap.add_argument("--required", help="必填欄（逗號分隔；主鍵與分割鍵自動必填）")
    ap.add_argument("--rename", action="append", default=[], metavar="原表頭=code",
                    help="指定 code（可重複），例如 --rename 保單號碼=policy_no；原表頭自動成為別名")
    ap.add_argument("--description", default="", help="資料集說明")
    ap.add_argument("--sample-rows", type=int, default=100_000, help="抽樣列數（預設 100000；0 = 讀全檔）")
    ap.add_argument("--numeric-headroom", type=int, default=2, help="numeric 整數部分在樣本最大位數外多留幾位（預設 2）")
    ap.add_argument("--force", action="store_true", help="輸出檔已存在時覆寫")
    a = ap.parse_args(argv)
    if a.sample_rows < 0 or a.numeric_headroom < 0:
        raise SystemExit("--sample-rows、--numeric-headroom 不可為負")
    if a.out.exists() and not a.force:
        raise SystemExit(f"輸出檔已存在：{a.out}（要覆寫請加 --force）")
    try:
        defn, report, hints, summ = build(a)
    except Blocking as e:
        print(f"無法產生定義（未寫出檔案）：\n{e}", file=sys.stderr)
        return 2

    w = csv.writer(sys.stdout, lineterminator="\n", delimiter="\t")
    w.writerow(["code", "原表頭", "推斷型別", "樣本非空", "最大長度", "備註"])
    w.writerows(report)
    for h in hints:
        print(f"提醒：{h}")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_name(a.out.name + ".part")
    tmp.write_text(json.dumps(defn, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(a.out)    # 寫暫存檔再 rename
    print(f"定義草稿：{a.out}（{summ['cols']} 欄，抽樣 {summ['rows']} 列）。請確認型別、主鍵、必填後再上傳。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

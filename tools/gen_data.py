"""合成資料產生器（全為隨機虛構資料，不含任何真實公司資料）。

產生兩樣東西：
  (a) 資料集定義 JSON：欄位名稱對應 app/catalog.py 的 DatasetDef / ColumnDef
  (b) CSV：UTF-8、逗號、首列表頭，串流逐列寫出（記憶體不隨筆數成長）

資料集：
  policy（預設）  yyyymm + policy_no 為主鍵，其餘混合型別，預設 200 欄
  claim           yyyymm + policy_no + claim_no 為主鍵，數個金額欄；policy_no 取自 policy 的前 N 筆
                  （--policy-rows 要與產生 policy 時的 --rows 一致，才能保證 join 得到）

用法範例：
  python tools/gen_data.py --out data/policy_202610.csv --def-out data/policy.json --rows 50000
  python tools/gen_data.py --dataset claim --policy-rows 50000 --rows 20000 --out data/claim_202610.csv
  python tools/gen_data.py --rows 50000 --shuffle-columns --gzip --out data/policy_shuffled.csv.gz

缺失值：非必填欄依 --null-rate 輸出「未加引號空欄」（SPEC 7.2 → NULL）。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
import sys
from pathlib import Path

DATE_BASE_ORD = 735599   # 2015-01-01 的 proleptic Gregorian ordinal
DATE_SPAN = 4000

# 非主鍵欄位型別輪替表（i 為欄位序）。比例：numeric2 25% / numeric6 15% / int 15% / bigint 10% / date 10% / varchar 25%
_PATTERN = [
    "numeric(18,2)", "numeric(18,2)", "numeric(18,6)", "integer", "varchar(20)",
    "bigint", "numeric(18,2)", "date", "varchar(50)", "numeric(18,6)",
    "integer", "numeric(18,2)", "bigint", "varchar(10)", "date",
    "numeric(18,2)", "integer", "numeric(18,6)", "varchar(100)", "numeric(18,2)",
]


def _dec(n: int, scale: int) -> str:
    """整數 n 以小數點後 scale 位表示（無浮點誤差）。"""
    sign = "-" if n < 0 else ""
    q, r = divmod(abs(n), 10 ** scale)
    return f"{sign}{q}.{r:0{scale}d}"


def build_policy_def(n_cols: int = 200, name: str = "fact_policy") -> dict:
    if n_cols < 8:
        raise SystemExit("--cols 至少 8")
    cols = [
        dict(code="yyyymm", data_type="integer", display_name="資料年月", aliases=[], required=True,
             is_pk=True, pk_order=0, checksum=False, group_name="鍵值", sort_order=0),
        dict(code="policy_no", data_type="varchar(20)", display_name="保單號碼", aliases=["POLICY_NO", "保單號碼"],
             required=True, is_pk=True, pk_order=1, checksum=False, group_name="鍵值", sort_order=1),
    ]
    n_checksum = 0
    for i in range(2, n_cols):
        t = _PATTERN[(i - 2) % len(_PATTERN)]
        base = t.split("(")[0]
        prefix = {"numeric": "amt", "integer": "cnt", "bigint": "big", "date": "dt", "varchar": "txt"}[base]
        code = f"{prefix}_{i:03d}"
        is_chk = False
        if t == "numeric(18,2)" and n_checksum < 3:   # 前 3 個 numeric(18,2) 欄做驗算欄
            is_chk = True
            n_checksum += 1
        cols.append(dict(code=code, data_type=t, display_name=f"欄位{i:03d}", aliases=[code.upper()],
                         required=False, is_pk=False, pk_order=0, checksum=is_chk,
                         group_name=f"群組{(i - 2) // 20 + 1}", sort_order=i))
    return dict(name=name, partition_key="yyyymm", description="合成保單資料（隨機產生，非真實資料）", columns=cols)


def build_claim_def(name: str = "fact_claim") -> dict:
    def num(i, code, disp, chk=False):
        return dict(code=code, data_type="numeric(18,2)", display_name=disp, aliases=[code.upper()], required=False,
                    is_pk=False, pk_order=0, checksum=chk, group_name="金額", sort_order=i)
    cols = [
        dict(code="yyyymm", data_type="integer", display_name="資料年月", aliases=[], required=True,
             is_pk=True, pk_order=0, checksum=False, group_name="鍵值", sort_order=0),
        dict(code="policy_no", data_type="varchar(20)", display_name="保單號碼", aliases=["POLICY_NO"], required=True,
             is_pk=True, pk_order=1, checksum=False, group_name="鍵值", sort_order=1),
        dict(code="claim_no", data_type="varchar(24)", display_name="理賠編號", aliases=["CLAIM_NO"], required=True,
             is_pk=True, pk_order=2, checksum=False, group_name="鍵值", sort_order=2),
        num(3, "claim_amt", "理賠金額", True),
        num(4, "paid_amt", "已付金額", True),
        num(5, "reserve_amt", "準備金"),
        dict(code="claim_dt", data_type="date", display_name="理賠日", aliases=[], required=False,
             is_pk=False, pk_order=0, checksum=False, group_name="日期", sort_order=6),
        dict(code="claim_type", data_type="varchar(10)", display_name="理賠類型", aliases=[], required=False,
             is_pk=False, pk_order=0, checksum=False, group_name="分類", sort_order=7),
    ]
    return dict(name=name, partition_key="yyyymm", description="合成理賠資料（隨機產生，非真實資料）", columns=cols)


def _make_cell(rng: random.Random, col: dict, null_rate: float):
    """回傳 (row_i -> 值) 的產生函式；None 代表 NULL。"""
    t = col["data_type"]
    base = t.split("(")[0]
    nr = 0.0 if col["required"] else null_rate
    rnd = rng.random
    if base == "numeric":
        scale = int(t.split(",")[1].rstrip(")"))
        lo, hi = -(10 ** (10 + scale)), 10 ** (11 + scale)
        ri = rng.randint
        return lambda: None if (nr and rnd() < nr) else _dec(ri(lo, hi), scale)
    if base == "integer":
        ri = rng.randint
        return lambda: None if (nr and rnd() < nr) else str(ri(-500_000, 2_000_000))
    if base == "bigint":
        ri = rng.randint
        return lambda: None if (nr and rnd() < nr) else str(ri(-10**14, 10**15))
    if base == "date":
        ri = rng.randint
        from datetime import date
        return lambda: None if (nr and rnd() < nr) else date.fromordinal(DATE_BASE_ORD + ri(0, DATE_SPAN)).isoformat()
    if base == "varchar":
        n = int(t.split("(")[1].rstrip(")"))
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
        ch = rng.choice
        ri = rng.randint
        return lambda: None if (nr and rnd() < nr) else "".join(ch(alphabet) for _ in range(ri(1, min(n, 12))))
    raise SystemExit(f"不支援型別 {t}")


def write_csv(defn: dict, kind: str, out: Path, rows: int, period: int, seed: int, null_rate: float,
              shuffle: bool, gz: bool, policy_rows: int) -> list[str]:
    cols = defn["columns"]
    rng = random.Random(f"{seed}:{period}:{kind}")
    order = list(range(len(cols)))
    if shuffle:
        random.Random(f"{seed}:shuffle").shuffle(order)
    headers = [cols[i]["code"] for i in order]
    gens = {c["code"]: _make_cell(rng, c, null_rate) for c in cols if not c["is_pk"] and c["code"] != "yyyymm"}
    out.parent.mkdir(parents=True, exist_ok=True)
    opener = (lambda p: gzip.open(p, "wt", encoding="utf-8", newline="", compresslevel=6)) if gz \
        else (lambda p: open(p, "w", encoding="utf-8", newline=""))
    tmp = out.with_name(out.name + ".part")
    with opener(tmp) as f:
        w = csv.writer(f, lineterminator="\n")   # None → 未加引號空欄
        w.writerow(headers)
        pick = rng.randrange
        for i in range(rows):
            if kind == "policy":
                keys = {"yyyymm": str(period), "policy_no": f"P{i:010d}"}   # 期別內唯一
            else:
                keys = {"yyyymm": str(period), "policy_no": f"P{pick(policy_rows):010d}",
                        "claim_no": f"C{period}{i:09d}"}                       # 主鍵 (期別, 保單, 理賠號) 唯一
            row = {**keys}
            for code, g in gens.items():
                row[code] = g()
            w.writerow([row[h] for h in headers])
    tmp.replace(out)   # 寫暫存檔再 rename
    return headers


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合成資料產生器")
    ap.add_argument("--dataset", choices=["policy", "claim"], default="policy")
    ap.add_argument("--rows", type=int, default=50_000)
    ap.add_argument("--cols", type=int, default=200, help="policy 的欄位數（含 yyyymm、policy_no），預設 200")
    ap.add_argument("--period", type=int, default=202610)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--null-rate", type=float, default=0.05, help="非必填欄的 NULL 比例")
    ap.add_argument("--shuffle-columns", action="store_true", help="打亂表頭順序（AC4）")
    ap.add_argument("--gzip", action="store_true")
    ap.add_argument("--policy-rows", type=int, default=50_000, help="claim 模式：policy 資料集的筆數（policy_no 取自其中）")
    ap.add_argument("--name", help="資料集名稱（預設 fact_policy / fact_claim）")
    ap.add_argument("--out", type=Path, required=True, help="CSV 輸出路徑（--gzip 時建議以 .csv.gz 結尾）")
    ap.add_argument("--def-out", type=Path, help="資料集定義 JSON 輸出路徑（預設與 CSV 同目錄 <dataset>.json）")
    a = ap.parse_args(argv)
    if not (0 <= a.null_rate < 1):
        raise SystemExit("--null-rate 需在 [0,1)")
    if a.dataset == "policy":
        defn = build_policy_def(a.cols, a.name or "fact_policy")
    else:
        defn = build_claim_def(a.name or "fact_claim")
    def_path = a.def_out or a.out.parent / f"{a.dataset}.json"
    def_path.parent.mkdir(parents=True, exist_ok=True)
    tmpj = def_path.with_name(def_path.name + ".part")
    tmpj.write_text(json.dumps(defn, ensure_ascii=False, indent=1), encoding="utf-8")
    tmpj.replace(def_path)
    write_csv(defn, a.dataset, a.out, a.rows, a.period, a.seed, a.null_rate, a.shuffle_columns, a.gzip, a.policy_rows)
    print(f"定義：{def_path}；CSV：{a.out}（{a.rows} 列 × {len(defn['columns'])} 欄，{a.out.stat().st_size / 1e6:.1f} MB）",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

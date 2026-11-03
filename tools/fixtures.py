"""AC7 / AC4 邊界案例 CSV（小檔）。每個函式產生一個檔並回傳其 Path；docstring 寫預期結果。

共同的測試資料集（FIXTURE_DEF）：
    yyyymm integer 必填 PK0 / policy_no varchar(20) 必填 PK1 / note varchar(100) / amt numeric(18,2)

預期結果的共同前提（SPEC 7.2）：
  - COPY 使用 FORMAT csv, NULL ''，且對非必填欄加 FORCE_NULL(...)，
    因此「未加引號空欄」與「""」都成為 NULL；NA、N/A 是一般字串，原樣保留。
  - 表頭先由 App 驗證（處理 BOM、重複、未知、缺必填），通過後才進 COPY。

用法：python tools/fixtures.py --out-dir data/fixtures    （或在測試中 import 後呼叫函式）
每個檔的機器可讀預期放在 EXPECT。
"""
from __future__ import annotations

import argparse
from pathlib import Path

FIXTURE_DEF = {
    "name": "fx_ac7",
    "partition_key": "yyyymm",
    "columns": [
        dict(code="yyyymm", data_type="integer", display_name="年月", aliases=[], required=True, is_pk=True,
             pk_order=0, checksum=False, group_name="", sort_order=0),
        dict(code="policy_no", data_type="varchar(20)", display_name="保單", aliases=[], required=True, is_pk=True,
             pk_order=1, checksum=False, group_name="", sort_order=1),
        dict(code="note", data_type="varchar(100)", display_name="備註", aliases=[], required=False, is_pk=False,
             pk_order=0, checksum=False, group_name="", sort_order=2),
        dict(code="amt", data_type="numeric(18,2)", display_name="金額", aliases=[], required=False, is_pk=False,
             pk_order=0, checksum=True, group_name="", sort_order=3),
    ],
}
HEADER = b"yyyymm,policy_no,note,amt\n"


def _w(d: Path, name: str, data: bytes) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(data)
    return p


def unquoted_empty(d: Path) -> Path:
    """未加引號空欄：`202610,P1,,` → note=NULL、amt=NULL；匯入成功，1 列。"""
    return _w(d, "unquoted_empty.csv", HEADER + b"202610,P1,,\n")


def quoted_empty(d: Path) -> Path:
    """`""`：`202610,P2,"",` → note=NULL（靠 FORCE_NULL；沒有它 PostgreSQL 會存成空字串 ''）；匯入成功。
    斷言：`note IS NULL` 為真，且 `note = ''` 為 0 列。"""
    return _w(d, "quoted_empty.csv", HEADER + b'202610,P2,"",\n')


def na_strings(d: Path) -> Path:
    """`NA`、`N/A` 是一般字串：note 分別等於 'NA'、'N/A'（不是 NULL）；匯入成功，2 列。"""
    return _w(d, "na_strings.csv", HEADER + b"202610,P3,NA,1.00\n202610,P4,N/A,2.00\n")


def tab_in_value(d: Path) -> Path:
    """欄值含 tab：note = 'a<TAB>b'，原樣保留（csv 格式 tab 不是分隔符）；匯入成功。"""
    return _w(d, "tab_in_value.csv", HEADER + b"202610,P5,a\tb,3.00\n")


def quoted_newline_comma(d: Path) -> Path:
    """引號內換行與逗號：note = 'x,y\\nz'（含逗號與換行，單一欄值）；匯入成功，仍是 1 列。
    斷言：表總列數 = 1（不能被換行拆成 2 列），且 note 完整等於 'x,y\\nz'。"""
    return _w(d, "quoted_newline_comma.csv", HEADER + b'202610,P6,"x,y\nz",4.00\n')


def bom_header(d: Path) -> Path:
    """UTF-8 BOM 開頭：App 讀表頭時去掉 BOM，第一欄仍認得為 yyyymm；匯入成功。
    （注意：若直接送進 COPY 而不處理 BOM，第一欄名稱會變 '\\ufeffyyyymm'；因此 BOM 必須由 App 在送 COPY 前剝除。）"""
    return _w(d, "bom_header.csv", b"\xef\xbb\xbf" + HEADER + b"202610,P7,bom,5.00\n")


def nul_byte(d: Path) -> Path:
    """含 NUL byte（0x00）：整檔拒絕（SPEC 7.2；PostgreSQL text 不能存 NUL）。
    斷言：拒絕訊息指出 NUL，表內 0 列新資料。"""
    return _w(d, "nul_byte.csv", HEADER + b"202610,P8,ab\x00cd,6.00\n")


def ragged_columns(d: Path) -> Path:
    """欄位數不一致（第 2 資料列少一欄）：COPY 失敗，整檔拒絕，表內 0 列新資料（原子性）。
    預期 PostgreSQL 訊息含 'missing data for column'。"""
    return _w(d, "ragged_columns.csv", HEADER + b"202610,P9,ok,7.00\n202610,P10,short\n")


def unclosed_quote(d: Path) -> Path:
    """引號未閉合：COPY 失敗（'unterminated CSV quoted field'），整檔拒絕。"""
    return _w(d, "unclosed_quote.csv", HEADER + b'202610,P11,"never closed,8.00\n')


def duplicate_header(d: Path) -> Path:
    """重複表頭（note 出現兩次）：表頭驗證階段拒絕，訊息指出重複欄名 'note'；不進 COPY。"""
    return _w(d, "duplicate_header.csv", b"yyyymm,policy_no,note,note\n202610,P12,a,b\n")


def unknown_header(d: Path) -> Path:
    """未知表頭（多出 extra_col）：表頭驗證階段拒絕，訊息指出 'extra_col' 並提示先到欄位維護新增；不進 COPY。"""
    return _w(d, "unknown_header.csv", b"yyyymm,policy_no,note,amt,extra_col\n202610,P13,a,1.00,zzz\n")


def missing_required(d: Path) -> Path:
    """缺必填欄（沒有 policy_no）：表頭驗證階段拒絕，訊息指出缺 'policy_no'。"""
    return _w(d, "missing_required.csv", b"yyyymm,note,amt\n202610,a,1.00\n")


def shuffled_header(d: Path) -> Path:
    """表頭順序與資料表不同（AC4）：欄序 amt,note,policy_no,yyyymm；匯入成功，
    每個值落在正確欄位：(yyyymm=202610, policy_no='P14', note='n14', amt=14.00)。"""
    return _w(d, "shuffled_header.csv", b"amt,note,policy_no,yyyymm\n14.00,n14,P14,202610\n")


ALL = {f.__name__: f for f in [
    unquoted_empty, quoted_empty, na_strings, tab_in_value, quoted_newline_comma, bom_header, nul_byte,
    ragged_columns, unclosed_quote, duplicate_header, unknown_header, missing_required, shuffled_header]}

# 機器可讀預期：ok=匯入成功；reject_stage=header（App 表頭驗證）/ nul（App 掃描）/ copy（COPY 失敗）
EXPECT = {
    "unquoted_empty": dict(ok=True, rows=1, note=None, amt=None),
    "quoted_empty": dict(ok=True, rows=1, note=None, amt=None),
    "na_strings": dict(ok=True, rows=2, notes=["NA", "N/A"]),
    "tab_in_value": dict(ok=True, rows=1, note="a\tb"),
    "quoted_newline_comma": dict(ok=True, rows=1, note="x,y\nz"),
    "bom_header": dict(ok=True, rows=1, note="bom"),
    "nul_byte": dict(ok=False, reject_stage="nul"),
    "ragged_columns": dict(ok=False, reject_stage="copy", msg_contains="missing data for column"),
    "unclosed_quote": dict(ok=False, reject_stage="copy", msg_contains="unterminated CSV quoted field"),
    "duplicate_header": dict(ok=False, reject_stage="header", msg_contains="note"),
    "unknown_header": dict(ok=False, reject_stage="header", msg_contains="extra_col"),
    "missing_required": dict(ok=False, reject_stage="header", msg_contains="policy_no"),
    "shuffled_header": dict(ok=True, rows=1, row=dict(yyyymm=202610, policy_no="P14", note="n14", amt="14.00")),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=Path("data/fixtures"))
    a = ap.parse_args()
    import json
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "fx_ac7.json").write_text(json.dumps(FIXTURE_DEF, ensure_ascii=False, indent=1), encoding="utf-8")
    for fn in ALL.values():
        print(fn(a.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

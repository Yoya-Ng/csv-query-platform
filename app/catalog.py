"""資料集與欄位定義（metadata 的記憶體表示）。

所有服務（匯入、查詢、匯出）都只依賴這裡的 dataclass，不直接讀 meta_* 表；
由 app.db.catalog_repo 從資料庫載入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")   # SPEC 5.2 / 13.4：小寫英數底線、≤30

# 允許的欄位型別（SPEC 5.2：NUMERIC(p,s)、VARCHAR(n)）。格式固定，便於各 adapter 轉換。
TYPE_RE = re.compile(
    r"^(?:varchar\((?P<n>\d{1,5})\)|numeric\((?P<p>\d{1,2}),(?P<s>\d{1,2})\)|integer|bigint|date)$"
)


class CatalogError(ValueError):
    pass


def check_ident(name: str) -> str:
    if not IDENT_RE.match(name):
        raise CatalogError(f"識別字不合法（需小寫英數底線、字母開頭、≤30 字）：{name!r}")
    return name


def parse_type(data_type: str) -> tuple[str, tuple[int, ...]]:
    """回傳 (基本型別, 參數)。例：'numeric(18,2)' → ('numeric', (18, 2))。"""
    m = TYPE_RE.match(data_type)
    if not m:
        raise CatalogError(f"不支援的欄位型別：{data_type!r}")
    if m.group("n"):
        n = int(m.group("n"))
        if not 1 <= n <= 4000:
            raise CatalogError(f"varchar 長度需 1～4000：{data_type!r}")
        return "varchar", (n,)
    if m.group("p"):
        p, s = int(m.group("p")), int(m.group("s"))
        if not (1 <= p <= 38 and 0 <= s <= p):
            raise CatalogError(f"numeric 精度不合法：{data_type!r}")
        return "numeric", (p, s)
    return data_type, ()


NUMERIC_BASE = {"numeric", "integer", "bigint"}


@dataclass(frozen=True)
class ColumnDef:
    code: str                       # 實體欄名
    data_type: str                  # 見 TYPE_RE
    display_name: str = ""
    aliases: tuple[str, ...] = ()   # CSV 表頭別名（比對時不分大小寫、去前後空白）
    required: bool = False
    is_pk: bool = False
    pk_order: int = 0
    checksum: bool = False          # 匯入驗算 SUM 欄位（須為數值型別）
    group_name: str = ""
    sort_order: int = 0
    description: str = ""
    disabled: bool = False

    @property
    def base_type(self) -> str:
        return parse_type(self.data_type)[0]

    @property
    def is_numeric(self) -> bool:
        return self.base_type in NUMERIC_BASE


@dataclass(frozen=True)
class DatasetDef:
    dataset_id: int
    name: str                       # 對外名稱（查詢 JSON 的 from / table），= 實體表名
    partition_key: str = "yyyymm"   # INTEGER，值為西元年月 YYYYMM
    schema_version: int = 1
    description: str = ""
    columns: tuple[ColumnDef, ...] = field(default_factory=tuple)

    @property
    def table_name(self) -> str:
        return self.name

    @property
    def deleted_table(self) -> str:
        return f"{self.name}_deleted"

    def col(self, code: str) -> ColumnDef:
        for c in self.columns:
            if c.code == code:
                return c
        raise CatalogError(f"資料集 {self.name} 沒有欄位 {code}")

    @property
    def active_columns(self) -> tuple[ColumnDef, ...]:
        return tuple(c for c in self.columns if not c.disabled)

    @property
    def pk_columns(self) -> tuple[ColumnDef, ...]:
        return tuple(sorted((c for c in self.columns if c.is_pk), key=lambda c: c.pk_order))

    def validate(self) -> None:
        check_ident(self.name)
        if len(self.name) > 22:   # 留空間給 _deleted、_p202610 等後綴，且 ≤30
            raise CatalogError("資料集名稱需 ≤22 字（後綴預留）")
        seen = set()
        for c in self.columns:
            check_ident(c.code)
            parse_type(c.data_type)
            if c.code in seen:
                raise CatalogError(f"欄位重複：{c.code}")
            seen.add(c.code)
            if any("|" in a or not a.strip() for a in c.aliases):
                raise CatalogError(f"別名不可空白或含 '|'：{c.code}")
            if c.is_pk and not c.required:
                raise CatalogError(f"主鍵欄位必須必填：{c.code}")
            if c.checksum and not c.is_numeric:
                raise CatalogError(f"驗算欄位必須是數值：{c.code}")
        pk = self.col(self.partition_key)
        if pk.data_type != "integer" or not pk.required:
            raise CatalogError("分割鍵必須是必填 integer")
        pks = self.pk_columns
        if pks and self.partition_key not in {c.code for c in pks}:
            raise CatalogError("主鍵必須包含分割鍵（SPEC 5.2）")


def partition_name(ds: DatasetDef, yyyymm: int) -> str:
    return f"{ds.name}_p{yyyymm}"


def check_period(yyyymm: int) -> int:
    if not (190001 <= yyyymm <= 299912 and 1 <= yyyymm % 100 <= 12):
        raise CatalogError(f"期別不合法：{yyyymm}")
    return yyyymm


def decimal_or_none(v) -> Decimal | None:
    return None if v is None else Decimal(v)

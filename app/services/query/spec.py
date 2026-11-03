"""JSON 查詢規格 → SQLAlchemy Select（SPEC 9.1、9.2、13.4）。

只產生 SQLAlchemy 表達式物件，值一律綁定參數；不使用 JSONB、陣列、ILIKE、:: 轉型。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import Select, and_, bindparam, func, select
from sqlalchemy.sql.elements import ColumnElement

from app.catalog import ColumnDef, DatasetDef, parse_type
from app.services.query.expr import ExprError, compile_expr
from app.services.query.tables import build_table

MAX_OUTPUT_COLS = 50          # SPEC 9.4
MAX_IN_VALUES = 10_000
MAX_TOTAL_PARAMS = 30_000      # PostgreSQL 單一陳述式綁定參數上限 65,535
MAX_PAGE = 1000
CALC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
_TOP_KEYS = {"from", "joins", "select", "calc", "where", "order", "schema_version"}
_JOIN_KEYS = {"table", "type", "on"}
_CALC_KEYS = {"name", "expr"}
_WHERE_OPS = {"=", "in", "between", "like_prefix", "is_null", "not_null"}
_PART_OPS = {"=", "in", "between"}


class QuerySpecError(ValueError):
    """查詢規格不合法（訊息為中文並指出位置）。"""


@dataclass
class CompiledQuery:
    select: Select
    columns: list[str]


@dataclass
class _T:
    ds: DatasetDef
    tbl: Any   # TableClause


def _kind(c: ColumnDef) -> str:
    b = c.base_type
    return "num" if c.is_numeric else ("date" if b == "date" else "str")


def _check_keys(obj: dict, allowed: set[str], where: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise QuerySpecError(f"{where}含未知的鍵：{sorted(unknown)}")


def _is_list(v) -> bool:
    return isinstance(v, list)


class _Ctx:
    def __init__(self, tables: dict[str, _T], base: str):
        self.tables = tables
        self.base = base

    def col(self, ref: Any, where: str, *, allow_bare: bool) -> tuple[_T, ColumnDef, ColumnElement]:
        if not isinstance(ref, str):
            raise QuerySpecError(f"{where}：欄位必須是字串")
        if "." in ref:
            tname, _, cname = ref.partition(".")
        elif allow_bare:
            tname, cname = self.base, ref
        else:
            raise QuerySpecError(f"{where}：欄位需寫成「表.欄」：{ref!r}")
        t = self.tables.get(tname)
        if t is None:
            raise QuerySpecError(f"{where}：資料表 {tname!r} 未出現在 from / joins")
        for c in t.ds.active_columns:
            if c.code == cname:
                return t, c, t.tbl.c[cname]
        raise QuerySpecError(f"{where}：資料集 {tname} 沒有可用欄位 {cname!r}（不存在或已停用）")


def _convert(c: ColumnDef, v: Any, where: str) -> Any:
    b = c.base_type
    if v is None or isinstance(v, (bool, list, dict)):
        raise QuerySpecError(f"{where}：值型別不合法：{v!r}")
    try:
        if b == "numeric":
            if isinstance(v, float):
                v = repr(v)
            d = Decimal(v if isinstance(v, str) else str(v))
            if not d.is_finite():
                raise InvalidOperation
            return d
        if b in ("integer", "bigint"):
            if isinstance(v, float):
                if not v.is_integer():
                    raise ValueError
                return int(v)
            if isinstance(v, int):
                return v
            return int(str(v).strip())
        if b == "date":
            if not isinstance(v, str):
                raise ValueError
            return date.fromisoformat(v)
        if not isinstance(v, str):
            raise ValueError
        maxlen = parse_type(c.data_type)[1][0]
        if len(v) > maxlen:
            raise QuerySpecError(f"{where}：值長度 {len(v)} 超過欄位上限 {maxlen}")
        return v
    except QuerySpecError:
        raise
    except (ValueError, InvalidOperation, TypeError):
        raise QuerySpecError(f"{where}：值 {v!r} 無法轉成 {c.data_type}") from None


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compile_query(spec: dict, catalog: Mapping[str, DatasetDef], *, big_tables: set[str]) -> CompiledQuery:
    if not isinstance(spec, dict):
        raise QuerySpecError("查詢規格必須是 JSON 物件")
    _check_keys(spec, _TOP_KEYS, "查詢規格")

    # ---- from / joins ----
    base = spec.get("from")
    if not isinstance(base, str) or base not in catalog:
        raise QuerySpecError(f"from：未知的資料集 {base!r}")
    tables: dict[str, _T] = {base: _T(catalog[base], build_table(catalog[base]))}
    joins = spec.get("joins", [])
    if not _is_list(joins):
        raise QuerySpecError("joins 必須是陣列")
    ctx = _Ctx(tables, base)
    from_clause = tables[base].tbl
    # 分割鍵相等的 join 連結 (來源表, 目標表)：來源表有期別條件時，PostgreSQL 會把常數推導到目標表做分割裁剪
    part_links: list[tuple[str, str]] = []
    for i, j in enumerate(joins):
        w = f"joins[{i}]"
        if not isinstance(j, dict):
            raise QuerySpecError(f"{w} 必須是物件")
        _check_keys(j, _JOIN_KEYS, w)
        name = j.get("table")
        if not isinstance(name, str) or name not in catalog:
            raise QuerySpecError(f"{w}.table：未知的資料集 {name!r}")
        if name in tables:
            raise QuerySpecError(f"{w}.table：資料集 {name} 重複出現（v1 不支援自我 join 或重複 join）")
        jtype = j.get("type")
        if jtype not in ("inner", "left"):
            raise QuerySpecError(f"{w}.type：只支援 inner / left，收到 {jtype!r}")
        on = j.get("on")
        if not _is_list(on) or not on:
            raise QuerySpecError(f"{w}.on 必須是非空的 [[左欄, 右欄], ...]")
        new = _T(catalog[name], build_table(catalog[name]))
        conds = []
        for k, pair in enumerate(on):
            wp = f"{w}.on[{k}]"
            if not _is_list(pair) or len(pair) != 2:
                raise QuerySpecError(f"{wp} 必須是 [左欄, 右欄]")
            lt, lc, lcol = ctx.col(pair[0], f"{wp}[0]", allow_bare=True)
            rref = pair[1]
            if isinstance(rref, str) and "." in rref:
                if rref.partition(".")[0] != name:
                    raise QuerySpecError(f"{wp}[1]：右欄必須屬於 {name}")
                cname = rref.partition(".")[2]
            else:
                cname = rref
            if not isinstance(cname, str):
                raise QuerySpecError(f"{wp}[1]：欄位必須是字串")
            rc = next((c for c in new.ds.active_columns if c.code == cname), None)
            if rc is None:
                raise QuerySpecError(f"{wp}[1]：資料集 {name} 沒有可用欄位 {cname!r}（不存在或已停用）")
            if _kind(lc) != _kind(rc):
                raise QuerySpecError(f"{wp}：join 兩欄型別不相容（{lc.data_type} vs {rc.data_type}）")
            conds.append(lcol == new.tbl.c[cname])
            if lc.code == lt.ds.partition_key and cname == new.ds.partition_key:
                part_links.append((lt.ds.name, name))          # 左 → 右：inner 與 left 都成立
                if jtype == "inner":
                    part_links.append((name, lt.ds.name))      # 右 → 左：只有 inner 成立
        from_clause = from_clause.join(new.tbl, and_(*conds), isouter=(jtype == "left"))
        tables[name] = new   # 之後的 join 才能引用它

    # ---- select / calc ----
    sel = spec.get("select", [])
    calcs = spec.get("calc", [])
    if not _is_list(sel) or not _is_list(calcs):
        raise QuerySpecError("select 與 calc 必須是陣列")
    if not sel and not calcs:
        raise QuerySpecError("select 與 calc 至少要有一欄")
    if len(sel) + len(calcs) > MAX_OUTPUT_COLS:
        raise QuerySpecError(f"輸出欄位 {len(sel) + len(calcs)} 超過上限 {MAX_OUTPUT_COLS}")
    outputs: list[ColumnElement] = []
    names: list[str] = []
    by_name: dict[str, ColumnElement] = {}
    for i, ref in enumerate(sel):
        t, c, col = ctx.col(ref, f"select[{i}]", allow_bare=False)
        out = f"{t.ds.name}__{c.code}"
        if out in by_name:
            raise QuerySpecError(f"select[{i}]：欄位 {ref} 重複")
        lab = col.label(out)
        outputs.append(lab); names.append(out); by_name[out] = lab

    def resolve(tname, cname):
        try:
            _, cdef, colexpr = ctx.col(f"{tname}.{cname}" if tname else cname, "運算式", allow_bare=True)
        except QuerySpecError as e:
            raise ExprError(str(e)) from None
        if _kind(cdef) != "num":
            # 文字相加在 SQLAlchemy 會變成字串串接、日期運算會在資料庫才失敗；一律在白名單階段拒絕
            raise ExprError(f"運算式只能使用數值欄位：{cname}（{cdef.data_type}）")
        return colexpr

    for i, cd in enumerate(calcs):
        w = f"calc[{i}]"
        if not isinstance(cd, dict):
            raise QuerySpecError(f"{w} 必須是物件")
        _check_keys(cd, _CALC_KEYS, w)
        n, ex = cd.get("name"), cd.get("expr")
        if not isinstance(n, str) or not CALC_NAME_RE.match(n):
            raise QuerySpecError(f"{w}.name 不合法（需小寫英數底線、字母開頭、≤30 字）：{n!r}")
        if n in by_name:
            raise QuerySpecError(f"{w}.name：輸出欄名重複：{n}")
        try:
            e = compile_expr(ex, resolve)
        except ExprError as err:
            raise QuerySpecError(f"{w}.expr：{err}") from None
        lab = e.label(n)
        outputs.append(lab); names.append(n); by_name[n] = lab

    # ---- where ----
    where = spec.get("where", [])
    if not _is_list(where):
        raise QuerySpecError("where 必須是陣列")
    conds: list[ColumnElement] = []
    part_ok: set[str] = set()
    in_total = 0
    for i, w_ in enumerate(where):
        w = f"where[{i}]"
        if not _is_list(w_) or not 2 <= len(w_) <= 3:
            raise QuerySpecError(f"{w} 必須是 [欄, 運算子, 值]")
        t, c, col = ctx.col(w_[0], f"{w}[0]", allow_bare=False)
        op = w_[1]
        if op not in _WHERE_OPS:
            raise QuerySpecError(f"{w}[1]：不支援的運算子 {op!r}（可用：{sorted(_WHERE_OPS)}）")
        pn = f"w{i}"
        if op in ("is_null", "not_null"):
            if len(w_) != 2:
                raise QuerySpecError(f"{w}：{op} 不接受值")
            conds.append(col.is_(None) if op == "is_null" else col.is_not(None))
            continue
        if len(w_) != 3:
            raise QuerySpecError(f"{w}：運算子 {op} 需要值")
        v = w_[2]
        wv = f"{w}[2]"
        if op == "=":
            conds.append(col == bindparam(pn, _convert(c, v, wv), type_=col.type))
        elif op == "in":
            if not _is_list(v) or not 1 <= len(v) <= MAX_IN_VALUES:
                raise QuerySpecError(f"{wv}：in 的值必須是 1～{MAX_IN_VALUES} 個元素的陣列")
            in_total += len(v)
            if in_total > MAX_TOTAL_PARAMS:
                raise QuerySpecError(f"{wv}：所有 in 條件合計超過 {MAX_TOTAL_PARAMS} 個值")
            vals = [_convert(c, x, f"{wv}[{k}]") for k, x in enumerate(v)]
            conds.append(col.in_(bindparam(pn, vals, expanding=True, type_=col.type)))
        elif op == "between":
            if not _is_list(v) or len(v) != 2:
                raise QuerySpecError(f"{wv}：between 的值必須是 2 個元素的陣列")
            lo, hi = (_convert(c, x, f"{wv}[{k}]") for k, x in enumerate(v))
            conds.append(col.between(bindparam(pn + "a", lo, type_=col.type),
                                     bindparam(pn + "b", hi, type_=col.type)))
        else:   # like_prefix
            if _kind(c) != "str":
                raise QuerySpecError(f"{w}：like_prefix 只能用於文字欄位")
            if not isinstance(v, str):
                raise QuerySpecError(f"{wv}：like_prefix 的值必須是字串")
            _convert(c, v, wv)   # 長度檢查
            conds.append(col.like(bindparam(pn, _escape_like(v) + "%", type_=col.type), escape="\\"))
        if op in _PART_OPS and c.code == t.ds.partition_key:
            part_ok.add(t.ds.name)

    # LEFT JOIN 右表的期別條件若寫在 where 會把 LEFT JOIN 變成 INNER JOIN；
    # 所以允許經由「分割鍵相等」的 join 條件取得期別（依 part_links 傳遞到不動點）
    changed = True
    while changed:
        changed = False
        for src, dst in part_links:
            if src in part_ok and dst not in part_ok:
                part_ok.add(dst)
                changed = True
    for name, t in tables.items():
        if name in big_tables and name not in part_ok:
            raise QuerySpecError(
                f"資料集 {name} 為大表，where 必須指定期別：{name}.{t.ds.partition_key} 的 =、in 或 between 條件")

    # ---- order ----
    order = spec.get("order", [])
    if not _is_list(order):
        raise QuerySpecError("order 必須是陣列")
    order_by = []
    for i, o in enumerate(order):
        w = f"order[{i}]"
        if not _is_list(o) or len(o) != 3:
            raise QuerySpecError(f"{w} 必須是 [欄或calc名, asc|desc, nulls_first|nulls_last]（需明寫 NULLS）")
        ref, direction, nulls = o
        if direction not in ("asc", "desc"):
            raise QuerySpecError(f"{w}[1]：只接受 asc / desc，收到 {direction!r}")
        if nulls not in ("nulls_first", "nulls_last"):
            raise QuerySpecError(f"{w}[2]：只接受 nulls_first / nulls_last，收到 {nulls!r}")
        if isinstance(ref, str) and "." not in ref and ref in by_name:
            target = by_name[ref]
        else:
            target = ctx.col(ref, f"{w}[0]", allow_bare=False)[2]
        e = target.asc() if direction == "asc" else target.desc()
        order_by.append(e.nulls_first() if nulls == "nulls_first" else e.nulls_last())

    stmt = select(*outputs).select_from(from_clause)
    if conds:
        stmt = stmt.where(and_(*conds))
    if order_by:
        stmt = stmt.order_by(*order_by)
    return CompiledQuery(select=stmt, columns=names)


def paginate(cq: CompiledQuery, offset: int, limit: int) -> Select:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise QuerySpecError(f"limit 需為 1～{MAX_PAGE} 的整數")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise QuerySpecError("offset 需為 ≥ 0 的整數")
    return cq.select.offset(offset).limit(limit)


def count_query(cq: CompiledQuery) -> Select:
    return select(func.count()).select_from(cq.select.order_by(None).subquery())

"""從 meta_dataset / meta_column 載入 DatasetDef，並提供資料集建立與欄位新增（SPEC 5.1、5.3）。"""
from __future__ import annotations

from sqlalchemy import insert, select, update

from app.catalog import CatalogError, ColumnDef, DatasetDef, check_ident, parse_type
from app.db.adapters.base import Adapter
from app.db.meta import audit_log, meta_column, meta_dataset
from app.db.sqlexec import execute

LOCK_TIMEOUT = "5s"


def _row_to_col(r) -> ColumnDef:
    m = r._mapping if hasattr(r, "_mapping") else r
    return ColumnDef(
        code=m["code"], data_type=m["data_type"], display_name=m["display_name"],
        aliases=tuple(a for a in m["aliases"].split("|") if a), required=m["required"],
        is_pk=m["is_pk"], pk_order=m["pk_order"], checksum=m["checksum"],
        group_name=m["group_name"], sort_order=m["sort_order"], description=m["description"],
        disabled=m["disabled"],
    )


_COLS = [meta_column.c[n] for n in (
    "code", "data_type", "display_name", "aliases", "required", "is_pk", "pk_order", "checksum",
    "group_name", "sort_order", "description", "disabled")]
_COL_NAMES = [c.name for c in _COLS]


def load_dataset(conn, name_or_id) -> DatasetDef:
    cond = (meta_dataset.c.dataset_id == name_or_id) if isinstance(name_or_id, int) \
        else (meta_dataset.c.name == name_or_id)
    rows = execute(conn, select(meta_dataset).where(cond)).fetchall()
    if not rows:
        raise CatalogError(f"資料集不存在：{name_or_id}")
    d = dict(zip(["dataset_id", "name", "partition_key", "description", "schema_version", "created_at"], rows[0]))
    cols = execute(conn, select(*_COLS).where(meta_column.c.dataset_id == d["dataset_id"])
                   .order_by(meta_column.c.sort_order, meta_column.c.code)).fetchall()
    return DatasetDef(
        dataset_id=d["dataset_id"], name=d["name"], partition_key=d["partition_key"],
        schema_version=d["schema_version"], description=d["description"],
        columns=tuple(_row_to_col(dict(zip(_COL_NAMES, r))) for r in cols),
    )


def load_all(conn) -> dict[str, DatasetDef]:
    names = [r[0] for r in execute(conn, select(meta_dataset.c.name)).fetchall()]
    return {n: load_dataset(conn, n) for n in names}


def _col_values(dataset_id: int, c: ColumnDef) -> dict:
    return dict(dataset_id=dataset_id, code=c.code, data_type=c.data_type, display_name=c.display_name,
                aliases="|".join(c.aliases), required=c.required, is_pk=c.is_pk, pk_order=c.pk_order,
                checksum=c.checksum, group_name=c.group_name, sort_order=c.sort_order,
                description=c.description, disabled=c.disabled)


def dataset_from_json(d: dict) -> DatasetDef:
    allowed = set(ColumnDef.__dataclass_fields__)
    if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not isinstance(d.get("columns"), list) \
            or not all(isinstance(c, dict) for c in d["columns"]):
        raise CatalogError("資料集定義需包含 name（字串）與 columns（物件陣列）")
    unknown_top = set(d) - {"name", "partition_key", "description", "columns"}
    if unknown_top:
        raise CatalogError(f"資料集定義有未知鍵：{sorted(unknown_top)}")
    cols = []
    for c in d["columns"]:
        unknown = set(c) - allowed
        if unknown:
            raise CatalogError(f"欄位定義有未知鍵：{sorted(unknown)}")
        c = dict(c)
        c["aliases"] = tuple(c.get("aliases", ()))
        if not isinstance(c.get("code"), str) or not isinstance(c.get("data_type"), str):
            raise CatalogError(f"欄位定義需包含 code 與 data_type：{c}")
        cols.append(ColumnDef(**c))
    ds = DatasetDef(dataset_id=0, name=d["name"], partition_key=d.get("partition_key", "yyyymm"),
                    description=d.get("description", ""), columns=tuple(cols))
    ds.validate()
    return ds


def create_dataset(conn, adapter: Adapter, ds: DatasetDef, *, note: str = "", ip: str = "") -> DatasetDef:
    """在同一交易內寫入 metadata 並建立實體表（PostgreSQL 交易式 DDL）。"""
    ds.validate()
    with conn.transaction():
        ds_id = execute(conn, insert(meta_dataset).values(
            name=ds.name, partition_key=ds.partition_key, description=ds.description,
            schema_version=1).returning(meta_dataset.c.dataset_id)).fetchall()[0][0]
        for c in ds.columns:
            execute(conn, insert(meta_column).values(**_col_values(ds_id, c)))
        adapter.create_table(conn, ds)
        execute(conn, insert(audit_log).values(action="create_dataset", dataset_id=ds_id,
                                               detail=ds.name, operator_note=note, client_ip=ip))
    return load_dataset(conn, ds_id)


def add_column(conn, adapter: Adapter, dataset: str, col: ColumnDef, *, note: str = "", ip: str = "") -> DatasetDef:
    """新增欄位（SPEC 5.3）：資料集層獨佔鎖 → ALTER 主表與 _deleted → schema_version+1，同一交易。"""
    check_ident(col.code)
    parse_type(col.data_type)
    if col.is_pk:
        raise CatalogError("不可新增主鍵欄位")
    if col.checksum and not col.is_numeric:
        raise CatalogError("驗算欄位必須是數值")
    with conn.transaction():
        ds = load_dataset(conn, dataset)
        adapter.set_lock_timeout(conn, LOCK_TIMEOUT)   # 先設逾時再取鎖：有寫入佔用時快速失敗（LockBusy）
        adapter.xact_lock_dataset(conn, ds.dataset_id, exclusive=True)
        ds = load_dataset(conn, ds.dataset_id)   # 取鎖後重讀
        if any(c.code == col.code for c in ds.columns):
            raise CatalogError(f"欄位已存在：{col.code}")
        execute(conn, insert(meta_column).values(**_col_values(ds.dataset_id, col)))
        adapter.add_column(conn, ds, col)
        execute(conn, update(meta_dataset).where(meta_dataset.c.dataset_id == ds.dataset_id)
                .values(schema_version=meta_dataset.c.schema_version + 1))
        execute(conn, insert(audit_log).values(action="add_column", dataset_id=ds.dataset_id,
                                               detail=f"{col.code} {col.data_type}", operator_note=note, client_ip=ip))
    return load_dataset(conn, ds.dataset_id)


def disable_column(conn, adapter: Adapter, dataset: str, code: str, *, note: str = "", ip: str = "") -> DatasetDef:
    """只停用不 DROP（SPEC 5.3）。停用不改實體結構，但查詢與匯入表頭會排除該欄，所以 schema_version+1。"""
    with conn.transaction():
        ds = load_dataset(conn, dataset)
        adapter.set_lock_timeout(conn, LOCK_TIMEOUT)
        adapter.xact_lock_dataset(conn, ds.dataset_id, exclusive=True)
        ds = load_dataset(conn, ds.dataset_id)   # 取鎖後重讀
        c = ds.col(code)
        if c.is_pk or c.code == ds.partition_key:
            raise CatalogError("主鍵與分割鍵不可停用")
        execute(conn, update(meta_column).where(meta_column.c.dataset_id == ds.dataset_id,
                                                meta_column.c.code == code).values(disabled=True))
        execute(conn, update(meta_dataset).where(meta_dataset.c.dataset_id == ds.dataset_id)
                .values(schema_version=meta_dataset.c.schema_version + 1))
        execute(conn, insert(audit_log).values(action="disable_column", dataset_id=ds.dataset_id,
                                               detail=code, operator_note=note, client_ip=ip))
    return load_dataset(conn, ds.dataset_id)

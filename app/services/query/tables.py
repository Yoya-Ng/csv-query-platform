"""由 DatasetDef 建立 SQLAlchemy 輕量 TableClause（不需要 MetaData、不連資料庫）。"""
from __future__ import annotations

from sqlalchemy import BigInteger, Date, Integer, Numeric, String, column, table
from sqlalchemy.sql.expression import TableClause

from app.catalog import ColumnDef, DatasetDef, parse_type


def sa_type(c: ColumnDef):
    base, args = parse_type(c.data_type)
    if base == "varchar":
        return String(args[0])
    if base == "numeric":
        return Numeric(args[0], args[1])
    return {"integer": Integer(), "bigint": BigInteger(), "date": Date()}[base]


def build_table(ds: DatasetDef) -> TableClause:
    """只含啟用欄位；停用欄位不會出現在 table.c。"""
    cols = [column(c.code, sa_type(c)) for c in ds.active_columns]
    return table(ds.table_name, *cols)

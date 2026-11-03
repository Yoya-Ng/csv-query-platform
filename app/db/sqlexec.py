"""在 psycopg 連線上執行 SQLAlchemy Core 陳述式（中繼資料表存取用；不建立 SQLAlchemy Engine）。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ClauseElement

# psycopg2 dialect 只用來編譯（pyformat、不加 ::TYPE，SPEC 13.4）；執行仍用 psycopg 3
_DIALECT = postgresql.psycopg2.dialect()


def compile_pg(stmt: ClauseElement) -> tuple[str, dict[str, Any]]:
    c = stmt.compile(dialect=_DIALECT, compile_kwargs={"render_postcompile": True})
    return str(c), dict(c.params)


class Row(tuple):
    """tuple 子類別：可用位置索引，也可用 row._mapping["欄名"]（與 SQLAlchemy Row 用法一致）。"""
    _fields: tuple[str, ...] = ()

    @property
    def _mapping(self) -> dict[str, Any]:
        return dict(zip(self._fields, self))


def _row_factory(cursor):
    names = tuple(d.name for d in (cursor.description or ()))
    cls = type("Row", (Row,), {"_fields": names})
    return lambda values: cls(values)


def execute(conn, stmt: ClauseElement):
    sql, params = compile_pg(stmt)
    return conn.cursor(row_factory=_row_factory).execute(sql, params)

"""匯出與複製（SPEC 10）。stream_export 的資料庫測試需 CQP_TEST_DSN。"""
import gzip as gz
import os
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import column, select, table
from sqlalchemy.dialects import postgresql

from app.services.export import CopyLimitError, ExportEncodingError, stream_export, to_copy_sql, to_tsv


def test_tsv_basic_and_none():
    out = to_tsv(["a", "b"], [(1, None), ("x", date(2026, 10, 1))])
    assert out == "a\tb\n1\t\nx\t2026-10-01\n"


def test_tsv_escaping():
    out = to_tsv(["h"], [("a\tb",), ("l1\nl2",), ("w\r\nx",), ('say "hi"',), ("cr\rx",)])
    lines = out.split("\n", 1)[1]
    assert lines == '"a\tb"\n"l1\nl2"\n"w\r\nx"\n"say ""hi"""\n"cr\rx"\n'


def test_tsv_header_escaped_too():
    assert to_tsv(['a"b'], []) == '"a""b"\n'


def test_tsv_decimal_no_scientific():
    out = to_tsv(["d"], [(Decimal("1E+3"),), (Decimal("0.0000001"),), (Decimal("-12.50"),)])
    assert out == "d\n1000\n0.0000001\n-12.50\n"


def test_tsv_row_limit():
    to_tsv(["a"], [(i,) for i in range(5)], max_rows=5)
    with pytest.raises(CopyLimitError, match="筆數"):
        to_tsv(["a"], [(i,) for i in range(6)], max_rows=5)


def test_tsv_byte_limit_uses_utf8_bytes():
    to_tsv(["a"], [("x" * 10,)], max_bytes=100)
    with pytest.raises(CopyLimitError, match="大小"):
        to_tsv(["a"], [("中" * 10,)], max_bytes=30)   # 30 個 UTF-8 bytes + 換行 > 30


def test_tsv_row_limit_stops_lazy_iterator():
    consumed = []

    def gen():
        for i in range(10_000_000):
            consumed.append(i)
            yield (i,)
    with pytest.raises(CopyLimitError):
        to_tsv(["a"], gen(), max_rows=3)
    assert len(consumed) == 4


def test_to_copy_sql_uses_pyformat_params_without_casts():
    t = table("policy", column("plan"), column("yyyymm"))
    stmt = select(t.c.plan).where(t.c.yyyymm.in_([202610, 202611]), t.c.plan.like("a%"))
    sql, params = to_copy_sql(stmt)
    assert sql.startswith("COPY (SELECT") and sql.endswith(") TO STDOUT (FORMAT csv, HEADER)")
    assert "::" not in sql and "POSTCOMPILE" not in sql
    assert sorted(params.values(), key=str) == [202610, 202611, "a%"]
    for k in params:
        assert f"%({k})s" in sql


class _FakeCopy:
    def __init__(self, chunks):
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.chunks)


class _FakeCur:
    def __init__(self, chunks, log):
        self.chunks, self.log = chunks, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def copy(self, sql, params):
        self.log.append((sql, params))
        return _FakeCopy(self.chunks)


class _FakeConn:
    def __init__(self, chunks):
        self.chunks, self.log, self.cancelled = chunks, [], 0

    def cursor(self):
        return _FakeCur(self.chunks, self.log)

    def cancel_safe(self):
        self.cancelled += 1


STMT = select(column("a")).select_from(table("t"))


def test_stream_utf8_bom_and_split_multibyte():
    data = "a,b\n中文,é\n".encode("utf-8")
    chunks = [data[:5], data[5:6], data[6:]]   # 把「中」的 3 bytes 切開
    out = b"".join(stream_export(_FakeConn(chunks), STMT, chunk=1))
    assert out == b"\xef\xbb\xbf" + data


def test_stream_big5_incremental_decode():
    data = "a,b\n中文,測試\n".encode("utf-8")
    chunks = [data[:5], data[5:6], data[6:]]
    out = b"".join(stream_export(_FakeConn(chunks), STMT, encoding="big5", chunk=1))
    assert out == "a,b\n中文,測試\n".encode("big5")
    assert not out.startswith(b"\xef\xbb\xbf")


def test_stream_big5_unrepresentable_raises():
    data = "x,😀\n".encode("utf-8")
    with pytest.raises(ExportEncodingError, match="U\\+1F600"):
        b"".join(stream_export(_FakeConn([data]), STMT, encoding="big5"))


def test_stream_gzip_roundtrip():
    data = ("a\n" + "中文\n" * 1000).encode("utf-8")
    out = b"".join(stream_export(_FakeConn([data[:100], data[100:]]), STMT, gzip=True))
    assert gz.decompress(out) == b"\xef\xbb\xbf" + data


def test_stream_early_close_cancels():
    conn = _FakeConn([b"x\n"] * 1000)
    g = stream_export(conn, STMT, chunk=1)
    next(g)
    g.close()
    assert conn.cancelled == 1


def test_stream_rejects_unknown_encoding():
    with pytest.raises(ValueError):
        list(stream_export(_FakeConn([]), STMT, encoding="latin-1"))


# ---------- 需要資料庫 ----------
DSN = os.environ.get("CQP_TEST_DSN")


@pytest.mark.db
@pytest.mark.skipif(not DSN, reason="未設定 CQP_TEST_DSN")
def test_stream_export_real_db():
    import psycopg
    from sqlalchemy import bindparam, literal_column

    with psycopg.connect(DSN) as conn:
        t = select(literal_column("g").label("n")).select_from(
            select(literal_column("generate_series(1, 5)").label("g")).subquery())
        stmt = t.where(literal_column("g") > bindparam("lo", 2))
        out = b"".join(stream_export(conn, stmt))
        assert out == b"\xef\xbb\xbfn\n3\n4\n5\n"
        # 提早關閉：不應拋例外，連線仍可用
        big = select(literal_column("g").label("n")).select_from(
            select(literal_column("generate_series(1, 50000000)").label("g")).subquery())
        g = stream_export(conn, big, chunk=1)
        next(g)
        g.close()
        conn.rollback()
        assert conn.execute("select 1").fetchall() == [(1,)]

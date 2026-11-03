"""/code-review 發現問題的回歸測試（每條對應一個發現，斷言通過的理由）。"""
from __future__ import annotations

import gzip
import uuid
from decimal import Decimal

import psycopg
import pytest

from app.catalog import ColumnDef, DatasetDef
from app.db import catalog_repo
from app.services import csvfile, importer, upload
from app.services.importer import SimulatedCrash
from app.services.query.expr import ExprError
from app.services.query.runner import check_joins
from app.services.query.spec import QuerySpecError, compile_query
from tests.conftest import small_dataset
from tests.test_import import BASE, P, load, seed, snapshot, write

LIM = dict(max_ratio=20, max_bytes=1 << 30)


# ---- 2. 多成員 gzip 不可只讀第一段；截斷的 gzip 要拒絕 ----
def test_multi_member_gzip_reads_all_members(tmp_path):
    p = tmp_path / "m.csv.gz"
    p.write_bytes(gzip.compress(b"h\n1\n") + gzip.compress(b"2\n3\n"))
    assert b"".join(csvfile.iter_raw(p, block=3, **LIM)) == b"h\n1\n2\n3\n"


def test_truncated_gzip_rejected(tmp_path):
    p = tmp_path / "t.csv.gz"
    p.write_bytes(gzip.compress(b"h\n" + b"1234567890\n" * 1000)[:-12])
    with pytest.raises(csvfile.CsvRejected, match="不完整"):
        b"".join(csvfile.iter_raw(p, block=64, **LIM))


def test_garbage_after_gzip_rejected(tmp_path):
    p = tmp_path / "g.csv.gz"
    p.write_bytes(gzip.compress(b"h\n1\n") + b"not gzip")
    with pytest.raises(csvfile.CsvRejected, match="gzip"):
        b"".join(csvfile.iter_raw(p, block=64, **LIM))


# ---- 4. check_joins 接受 表.欄 形式的右欄 ----
def _two():
    a, b = small_dataset("a"), small_dataset("b")
    return {"a": a, "b": b}


def test_check_joins_qualified_right_refs_cover_pk():
    spec = {"from": "a", "joins": [{"table": "b", "type": "inner",
                                    "on": [["yyyymm", "b.yyyymm"], ["policy_no", "b.policy_no"]]}]}
    assert check_joins(spec, _two(), allow_many_to_many=False) == []


def test_check_joins_many_to_many_still_rejected():
    spec = {"from": "a", "joins": [{"table": "b", "type": "inner", "on": [["note", "b.note"]]}]}
    with pytest.raises(QuerySpecError, match="多對多"):
        check_joins(spec, _two(), allow_many_to_many=False)


# ---- 8. 運算式只接受數值欄位 ----
@pytest.mark.parametrize("expr,col", [("a.policy_no + a.note", "policy_no"), ("a.eff_date / 2", "eff_date")])
def test_calc_rejects_non_numeric_columns(expr, col):
    spec = {"from": "a", "select": ["a.policy_no"], "calc": [{"name": "x", "expr": expr}],
            "where": [["a.yyyymm", "=", 202610]]}
    with pytest.raises((ExprError, QuerySpecError), match=f"只能使用數值欄位：{col}"):
        compile_query(spec, _two(), big_tables=set())


# ---- 以下需要資料庫 ----
@pytest.mark.db
def test_swap_keeps_single_pk_index(conn, adapter, settings, dataset, tmp_path):
    """3. 預建的主鍵要是限制（ADD CONSTRAINT），ATTACH 才會掛接而不在交易內另建索引。"""
    seed(conn, adapter, settings, dataset, tmp_path)
    _, st = load(conn, adapter, settings, dataset, write(tmp_path, "v2.csv", BASE))
    assert st == "CLEANED"
    part = f"{dataset.name}_p{P}"
    idx = adapter.partition_indexes(conn, part)
    assert len(idx) == 1, idx                              # 只有一個主鍵索引，沒有重複的
    parent_idx = conn.execute("""SELECT i.indexrelid::regclass::text FROM pg_inherits h
                                 JOIN pg_index i ON i.indexrelid = h.inhrelid
                                 WHERE h.inhparent = to_regclass(%s)""", (f"{dataset.name}_pkey",)).fetchall()
    assert [r[0] for r in parent_idx] == idx               # 該索引已掛為父表主鍵索引的子索引


@pytest.mark.db
def test_delete_list_with_required_non_pk_column(conn, adapter, settings, tmp_path):
    """1. 資料集有必填的非主鍵欄位時，只含主鍵的刪除清單仍可使用。"""
    base = small_dataset("x_" + uuid.uuid4().hex[:8])
    cols = tuple(ColumnDef(**{**c.__dict__, "required": True}) if c.code == "note" else c for c in base.columns)
    ds = catalog_repo.create_dataset(conn, adapter, DatasetDef(dataset_id=0, name=base.name, columns=cols))
    try:
        _, st = load(conn, adapter, settings, ds, write(tmp_path, "b.csv",
                     "yyyymm,policy_no,note\n202610,A,x\n202610,B,y\n202610,C,z\n202610,D,w\n"
                     "202610,E,v\n202610,F,u\n202610,G,t\n202610,H,s\n202610,I,r\n202610,J,q\n202610,K,p\n"))
        assert st == "CLEANED"
        lid, st = load(conn, adapter, settings, ds, write(tmp_path, "d.csv", "yyyymm,policy_no\n202610,A\n"),
                       mode="delete")
        assert st == "PREVIEWED", importer.get_load(conn, lid)["error"]
        done = importer.confirm_delete(conn, adapter, lid, operator="x", settings=settings)
        assert done["rows_deleted"] == 1
    finally:
        for (t,) in conn.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s", (ds.name + "%",)).fetchall():
            conn.execute(psycopg.sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(t)))
        for t in ("load_log", "audit_log", "meta_column", "meta_dataset"):
            conn.execute(psycopg.sql.SQL("DELETE FROM {} WHERE dataset_id = %s").format(psycopg.sql.Identifier(t)),
                         (ds.dataset_id,))


@pytest.mark.db
def test_restore_keeps_backup_of_conflicting_rows(conn, adapter, settings, dataset, tmp_path):
    """5. 還原時因主鍵已被重新寫入而略過的列，備份要留在 _deleted。"""
    seed(conn, adapter, settings, dataset, tmp_path)
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "d.csv",
                   "yyyymm,policy_no\n202610,A001\n202610,A002\n"), mode="delete")
    importer.confirm_delete(conn, adapter, lid, operator="x", confirm_large=True, settings=settings)
    _, st = load(conn, adapter, settings, dataset, write(tmp_path, "u.csv", "policy_no,amt\nA001,5\n"), mode="upsert")
    assert st == "CLEANED"
    restored, skipped = adapter.restore_deleted(conn, dataset, lid)
    assert (restored, skipped) == (1, 1)
    left = conn.execute(psycopg.sql.SQL("SELECT policy_no, amt FROM {} WHERE _load_id = %s").format(
        psycopg.sql.Identifier(dataset.deleted_table)), (lid,)).fetchall()
    assert left == [("A001", Decimal("100.50"))]           # 被略過的 A001 舊值仍可追溯
    assert snapshot(conn, dataset)[0] == 3


@pytest.mark.db
def test_crash_during_staging_counts_attempts(conn, adapter, settings, dataset, tmp_path, monkeypatch):
    """6. COPY 中途程序死亡也要累計嘗試次數，達上限即 FAILED，不會無限重試。"""
    from tests.conftest import DSN

    def die(*a, **k):
        raise SimulatedCrash()
    monkeypatch.setattr(adapter, "bulk_load", die)
    up = upload.register_local_file(conn, settings, write(tmp_path, "a.csv", BASE))
    lid = importer.create_load(conn, adapter, dataset=dataset.name, yyyymm=P, mode="replace",
                               upload_id=up["upload_id"])
    for i in range(1, importer.MAX_ATTEMPTS + 1):
        c = psycopg.connect(DSN, autocommit=True)
        with pytest.raises(SimulatedCrash):
            importer.run_load(c, adapter, lid, settings=settings)
        c.close()
        assert importer.get_load(conn, lid)["attempts"] == i
        todo = importer.recover(conn, adapter, settings=settings)
        if i < importer.MAX_ATTEMPTS:
            assert lid in todo
    ld = importer.get_load(conn, lid)
    assert ld["status"] == "FAILED" and f"重試 {importer.MAX_ATTEMPTS} 次" in ld["error"]


@pytest.mark.db
def test_export_stream_releases_when_never_iterated(conn, adapter, settings, dataset, tmp_path):
    """7. 匯出物件建立後從未迭代就被丟棄，連線與名額也要釋放（同 session 可再次匯出）。"""
    import gc

    from app.db.pool import close_all
    from app.services.query import runner
    seed(conn, adapter, settings, dataset, tmp_path)
    spec = {"from": dataset.name, "select": [f"{dataset.name}.policy_no"],
            "where": [[f"{dataset.name}.yyyymm", "=", P]]}
    cat = {dataset.name: dataset}
    try:
        for _ in range(6):                      # 匯出連線池 4、每 session 名額 1：有遺留就會失敗
            s = runner.export(spec, cat, adapter, session_id="sess-1", client_ip="10.0.0.1")
            del s
            gc.collect()
        s = runner.export(spec, cat, adapter, session_id="sess-1", client_ip="10.0.0.1")
        body = b"".join(s)
        assert body == ("﻿" + f"{dataset.name}__policy_no\nA001\nA002\nA003\n").encode()
    finally:
        close_all()

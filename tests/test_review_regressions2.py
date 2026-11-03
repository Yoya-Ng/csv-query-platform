"""code-reviewer agent 第二批發現的回歸測試。"""
from __future__ import annotations

import time
import uuid

import psycopg
import pytest

from app.catalog import CatalogError, ColumnDef, DatasetDef
from app.db import catalog_repo
from app.db.adapters.base import LockBusy
from app.services import importer, upload
from app.services.query.spec import QuerySpecError, compile_query
from tests.conftest import small_dataset
from tests.test_import import P, load, seed, write


def _drop(conn, ds):
    for (t,) in conn.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s", (ds.name + "%",)).fetchall():
        conn.execute(psycopg.sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(t)))
    for t in ("load_log", "audit_log", "meta_column", "meta_dataset"):
        conn.execute(psycopg.sql.SQL("DELETE FROM {} WHERE dataset_id = %s").format(psycopg.sql.Identifier(t)),
                     (ds.dataset_id,))


@pytest.mark.db
def test_error_report_with_many_columns(conn, adapter, settings, tmp_path):
    """R1：120 欄（條件超過 100 個）時錯誤報表仍可產生，不會撞到函式參數上限。"""
    name = "w_" + uuid.uuid4().hex[:8]
    cols = [ColumnDef("yyyymm", "integer", required=True, is_pk=True, pk_order=0),
            ColumnDef("k", "varchar(10)", required=True, is_pk=True, pk_order=1)]
    cols += [ColumnDef(f"n{i:03d}", "integer", sort_order=i + 2) for i in range(120)]
    ds = catalog_repo.create_dataset(conn, adapter, DatasetDef(dataset_id=0, name=name, columns=tuple(cols)))
    try:
        header = "yyyymm,k," + ",".join(f"n{i:03d}" for i in range(120))
        row = "202610,A," + ",".join("x" if i == 119 else "1" for i in range(120))
        lid, st = load(conn, adapter, settings, ds, write(tmp_path, "w.csv", f"{header}\n{row}\n"))
        err = importer.get_load(conn, lid)["error"]
        assert st == "FAILED" and "1 列格式錯誤" in err and "錯誤列報表" in err, err
        assert "n119:integer 格式不符" in (settings.error_report_dir / f"{lid}.csv").read_text()
    finally:
        _drop(conn, ds)


class _Recorder:
    """包住連線，記錄 adapter 送出的 SQL，事後用 EXPLAIN 檢查分割裁剪。"""

    def __init__(self, conn):
        self._c, self.sqls = conn, []

    def execute(self, q, params=None):
        self.sqls.append((q.as_string(self._c) if hasattr(q, "as_string") else q, params))
        return self._c.execute(q, params)

    def __getattr__(self, n):
        return getattr(self._c, n)


@pytest.mark.db
def test_delete_and_upsert_scan_single_partition(conn, adapter, settings, dataset, tmp_path):
    """R7：刪除預覽、刪除、upsert 前計數的計畫只出現指定期別的分割（另一期 202609 不得出現）。"""
    seed(conn, adapter, settings, dataset, tmp_path)
    _, st = load(conn, adapter, settings, dataset, write(tmp_path, "p9.csv", "yyyymm,policy_no\n202609,A001\n"),
                 period=202609)
    assert st == "CLEANED"
    stg = "stg_t" + uuid.uuid4().hex[:6]
    adapter.create_staging(conn, stg, ["yyyymm", "policy_no"])
    conn.execute(psycopg.sql.SQL("INSERT INTO {} (yyyymm, policy_no) VALUES ('202610', 'A001')").format(
        psycopg.sql.Identifier(stg)))
    plans = []
    try:
        with conn.transaction():
            rec = _Recorder(conn)
            adapter.delete_preview(rec, dataset, stg, 10, P)
            adapter.delete_by_keys(rec, dataset, stg, str(uuid.uuid4()), "x", P)
            adapter.upsert(rec, dataset, stg, ["yyyymm", "policy_no"], P)
            for q, params in rec.sqls:
                if q.lstrip().upper().startswith(("SELECT", "WITH", "INSERT")):
                    plans.append("\n".join(r[0] for r in conn.execute("EXPLAIN " + q, params).fetchall()))
            raise psycopg.Rollback()
    finally:
        adapter.drop_table(conn, stg)
    assert len(plans) >= 4
    # 正向斷言：計畫確實列出分割名稱（避免「沒有分割名稱」而誤判通過）
    assert sum(f"{dataset.name}_p202610" in pl for pl in plans) >= 3, plans
    for pl in plans:
        assert f"{dataset.name}_p202609" not in pl, pl


@pytest.mark.db
def test_upload_cannot_be_reused(conn, adapter, settings, dataset, tmp_path):
    """R15：同一上傳只能對應一筆匯入（第一筆清理時會刪檔）。"""
    seed(conn, adapter, settings, dataset, tmp_path)
    up = upload.register_local_file(conn, settings, write(tmp_path, "u.csv", "policy_no\nA001\n"))
    importer.create_load(conn, adapter, dataset=dataset.name, yyyymm=P, mode="upsert", upload_id=up["upload_id"])
    with pytest.raises(importer.LoadError, match="上傳尚未完成"):
        importer.create_load(conn, adapter, dataset=dataset.name, yyyymm=202609, mode="replace",
                             upload_id=up["upload_id"])


@pytest.mark.db
def test_add_column_fails_fast_under_lock(conn, adapter, dataset, conn2):
    """R6：不依賴呼叫端的 lock_timeout，欄位維護遇到鎖會在約 5 秒內以 LockBusy 結束。"""
    adapter.session_lock_dataset_exclusive(conn2, dataset.dataset_id)
    try:
        t0 = time.monotonic()
        with pytest.raises(LockBusy):
            catalog_repo.add_column(conn, adapter, dataset.name, ColumnDef("x3", "integer"))
        assert time.monotonic() - t0 < 7
    finally:
        adapter.session_unlock_dataset_exclusive(conn2, dataset.dataset_id)


def _cat():
    return {"a": small_dataset("a")}


def test_in_total_limit():
    spec = {"from": "a", "select": ["a.policy_no"],
            "where": [["a.yyyymm", "=", 202610]] + [["a.qty", "in", list(range(10_000))] for _ in range(4)]}
    with pytest.raises(QuerySpecError, match="合計超過"):
        compile_query(spec, _cat(), big_tables=set())


def test_alias_with_pipe_rejected():
    ds = small_dataset("a")
    bad = DatasetDef(dataset_id=0, name="a", columns=tuple(
        ColumnDef(**{**c.__dict__, "aliases": ("x|y",)}) if c.code == "note" else c for c in ds.columns))
    with pytest.raises(CatalogError, match="別名"):
        bad.validate()

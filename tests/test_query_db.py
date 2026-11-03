"""查詢在真實 PostgreSQL 上的行為：分割裁剪、LEFT JOIN 語意、唯讀角色名額與取消（AC10 的裁剪斷言、AC13 的取消）。"""
from __future__ import annotations

import os
import threading
import time
import uuid

import psycopg
import pytest

from app.db import catalog_repo
from app.db.sqlexec import compile_pg
from app.services import importer, upload
from app.services.query.spec import QuerySpecError, compile_query
from tests.conftest import small_dataset

pytestmark = pytest.mark.db
DSN_RO = os.environ.get("CQP_TEST_DSN_RO")


@pytest.fixture
def two(conn, adapter, settings, tmp_path):
    """兩個資料集各 2 期（202609、202610）；右表在 202610 只有部分保單。"""
    made = []
    for name in ("l_" + uuid.uuid4().hex[:8], "r_" + uuid.uuid4().hex[:8]):
        made.append(catalog_repo.create_dataset(conn, adapter, small_dataset(name)))
    left, right = made
    data = {
        (left, 202609): ["A", "B"], (left, 202610): ["A", "B", "C"],
        (right, 202609): ["A"], (right, 202610): ["A"],
    }
    for (ds, p), keys in data.items():
        f = tmp_path / f"{ds.name}_{p}.csv"
        f.write_text("yyyymm,policy_no,amt\n" + "".join(f"{p},{k},1\n" for k in keys))
        up = upload.register_local_file(conn, settings, f)
        lid = importer.create_load(conn, adapter, dataset=ds.name, yyyymm=p, mode="replace",
                                   upload_id=up["upload_id"])
        assert importer.run_load(conn, adapter, lid, settings=settings) == "CLEANED"
    yield left, right
    for ds in made:
        for (t,) in conn.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s", (ds.name + "%",)).fetchall():
            conn.execute(psycopg.sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(t)))
        for tbl in ("load_log", "meta_column", "meta_dataset"):
            conn.execute(psycopg.sql.SQL("DELETE FROM {} WHERE dataset_id = %s").format(psycopg.sql.Identifier(tbl)),
                         (ds.dataset_id,))


def _spec(left, right, *, on_period=True):
    on = [["policy_no", "policy_no"]] + ([["yyyymm", "yyyymm"]] if on_period else [])
    return {"from": left.name, "joins": [{"table": right.name, "type": "left", "on": on}],
            "select": [f"{left.name}.policy_no", f"{right.name}.amt"],
            "where": [[f"{left.name}.yyyymm", "=", 202610]],
            "order": [[f"{left.name}.policy_no", "asc", "nulls_last"]]}


def test_left_join_prunes_right_partitions_and_keeps_rows(conn, two):
    left, right = two
    cat = {left.name: left, right.name: right}
    cq = compile_query(_spec(left, right), cat, big_tables={left.name, right.name})
    sql_text, params = compile_pg(cq.select)
    plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + sql_text, params).fetchall())
    # 兩表都只出現 202610 的分割（PostgreSQL 由等價類推導出右表的期別常數）
    assert f"{left.name}_p202610" in plan and f"{right.name}_p202610" in plan, plan
    assert "202609" not in plan, plan
    rows = conn.execute(sql_text, params).fetchall()
    assert [r[0] for r in rows] == ["A", "B", "C"]          # LEFT JOIN 保留左表全部 3 筆
    assert [r[1] is None for r in rows] == [False, True, True]


def test_big_right_table_without_period_link_rejected(two):
    left, right = two
    cat = {left.name: left, right.name: right}
    with pytest.raises(QuerySpecError, match=f"資料集 {right.name} 為大表"):
        compile_query(_spec(left, right, on_period=False), cat, big_tables={left.name, right.name})


@pytest.mark.skipif(not DSN_RO, reason="未設定 CQP_TEST_DSN_RO")
def test_readonly_role_slots_and_cancel(adapter, two):
    """app_ro（default_transaction_read_only）可取得 advisory 名額；取消 API 依 job tag 找 PID。"""
    a = psycopg.connect(DSN_RO, autocommit=True)
    b = psycopg.connect(DSN_RO, autocommit=True)
    try:
        k = adapter.try_slot(a, "session-query", "s1", 1)
        assert k is not None and adapter.try_slot(b, "session-query", "s1", 1) is None   # 同 session 第 2 個被拒
        adapter.release_slot(a, k)
        assert adapter.try_slot(b, "session-query", "s1", 1) is not None
        job = str(uuid.uuid4())
        adapter.tag_connection(a, f"cqp-q-{job}")
        err = {}

        def long_query():
            try:
                a.execute("SELECT pg_sleep(10)")
            except psycopg.errors.QueryCanceled as e:
                err["e"] = e
        th = threading.Thread(target=long_query)
        th.start()
        time.sleep(0.5)
        assert adapter.cancel_tagged(b, f"cqp-q-{uuid.uuid4()}") is False       # 別的 job_id 取消不到
        assert adapter.cancel_tagged(b, f"cqp-q-{job}") is True
        th.join(5)
        assert "e" in err
        # 先被 default_transaction_read_only 擋；即使關掉旗標也會被權限擋（tools/verify_db.py 已驗證）
        with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
            b.execute("CREATE TABLE should_fail (x int)")
    finally:
        a.close()
        b.close()

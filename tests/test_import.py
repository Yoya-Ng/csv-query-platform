"""匯入、增修、刪除整合測試（SPEC 14：AC4～AC9）。每條斷言通過的理由（值、列數、SUM、錯誤訊息）。"""
from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from app.catalog import ColumnDef, partition_name
from app.db import catalog_repo
from app.services import importer, upload
from app.services.importer import SimulatedCrash
from tests.conftest import DSN

pytestmark = pytest.mark.db
P = 202610


def write(tmp_path: Path, name: str, content: bytes | str) -> Path:
    p = tmp_path / name
    p.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return p


def load(conn, adapter, settings, ds, path, *, mode="replace", period=P, hooks=None):
    up = upload.register_local_file(conn, settings, path)
    lid = importer.create_load(conn, adapter, dataset=ds.name, yyyymm=period, mode=mode,
                               upload_id=up["upload_id"], operator_note="test")
    st = importer.run_load(conn, adapter, lid, settings=settings, hooks=hooks)
    return lid, st


def rows(conn, ds, order="policy_no"):
    return conn.execute(psycopg.sql.SQL("SELECT policy_no, amt, qty, eff_date, note FROM {} ORDER BY {}").format(
        psycopg.sql.Identifier(ds.name), psycopg.sql.Identifier(order))).fetchall()


def snapshot(conn, ds):
    return conn.execute(psycopg.sql.SQL("SELECT count(*), COALESCE(SUM(amt),0) FROM {}").format(
        psycopg.sql.Identifier(ds.name))).fetchall()[0]


BASE = ("yyyymm,policy_no,amt,qty,eff_date,note\n"
        "202610,A001,100.50,1,2026-10-01,x\n"
        "202610,A002,-20.25,2,2026-10-02,y\n"
        "202610,A003,,3,,\n")


def seed(conn, adapter, settings, ds, tmp_path):
    lid, st = load(conn, adapter, settings, ds, write(tmp_path, "base.csv", BASE))
    assert st == "CLEANED", importer.get_load(conn, lid)["error"]
    return lid


# ---------------------------------------------------------------- 正常流程
def test_replace_happy_path(conn, adapter, settings, dataset, tmp_path):
    lid = seed(conn, adapter, settings, dataset, tmp_path)
    ld = importer.get_load(conn, lid)
    assert (ld["rows_staged"], ld["rows_inserted"]) == (3, 3)
    assert json.loads(ld["checksums"]) == {"amt": "80.25"}
    assert snapshot(conn, dataset) == (3, Decimal("80.25"))
    assert ld["bak_table"] is None                      # 第一次匯入沒有舊分割
    # 第二次重傳：舊分割改名為 _bak，正式資料換成新檔
    lid2, st = load(conn, adapter, settings, dataset, write(tmp_path, "v2.csv",
                    "yyyymm,policy_no,amt\n202610,B001,1.00\n"))
    assert st == "CLEANED"
    bak = importer.get_load(conn, lid2)["bak_table"]
    assert bak and adapter.table_exists(conn, bak)
    assert snapshot(conn, dataset) == (1, Decimal("1.00"))
    assert conn.execute(psycopg.sql.SQL("SELECT count(*) FROM {}").format(
        psycopg.sql.Identifier(bak))).fetchall()[0][0] == 3
    # staging 與新分割暫存名稱都已清除
    n = importer.names(dataset, lid2)
    assert not adapter.table_exists(conn, n["stg"]) and not adapter.table_exists(conn, n["newp"])


def test_attach_uses_check_without_scan(conn, adapter, settings, dataset, tmp_path):
    """ATTACH 時有等價 CHECK 就不掃描：DEBUG1 會出現 'is implied by existing constraints'。"""
    seed(conn, adapter, settings, dataset, tmp_path)
    notices = []
    conn.add_notice_handler(lambda d: notices.append(d.message_primary))
    conn.execute("SET client_min_messages = debug1")
    try:
        _, st = load(conn, adapter, settings, dataset, write(tmp_path, "v2.csv", BASE))
    finally:
        conn.execute("RESET client_min_messages")
    assert st == "CLEANED"
    assert any("implied by existing constraints" in m for m in notices), notices


# ---------------------------------------------------------------- AC4 欄位對應
def test_ac4_header_order_and_alias(conn, adapter, settings, dataset, tmp_path):
    csv = ("note,金額,保單號碼,yyyymm,qty\n"
           "hello,12.30,Z9,202610,7\n")
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "a.csv", csv))
    assert st == "CLEANED", importer.get_load(conn, lid)["error"]
    assert importer.get_load(conn, lid)["header_match"] is False      # 用別名 → HEADER true + 欄位清單
    assert rows(conn, dataset) == [("Z9", Decimal("12.30"), 7, None, "hello")]


def test_ac4_header_match_when_exact(conn, adapter, settings, dataset, tmp_path):
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "a.csv",
                   "qty,policy_no,yyyymm\n5,K1,202610\n"))
    assert st == "CLEANED"
    assert importer.get_load(conn, lid)["header_match"] is True
    assert rows(conn, dataset) == [("K1", None, 5, None, None)]


@pytest.mark.parametrize("header,msg", [
    ("yyyymm,policy_no,unknown_col", "未知欄位「unknown_col」"),
    ("yyyymm,policy_no,amt,金額", "欄位重複「金額」"),
    ("yyyymm,amt", "缺少必填欄位 policy_no"),
    ("yyyymm,,policy_no", "第 2 欄表頭空白"),
])
def test_ac4_header_rejected(conn, adapter, settings, dataset, tmp_path, header, msg):
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "h.csv", header + "\n202610,A,1\n"))
    assert st == "FAILED"
    assert msg in importer.get_load(conn, lid)["error"]


# ---------------------------------------------------------------- AC7 缺失值與格式
def test_ac7_missing_values_and_quoting(conn, adapter, settings, dataset, tmp_path):
    csv = ("﻿yyyymm,policy_no,amt,note\n"
           '202610,P1,,""\n'                 # 未加引號空欄、"" → NULL
           "202610,P2,1,NA\n"                # NA 原樣
           "202610,P3,2,N/A\n"               # N/A 原樣
           '202610,P4,3,"a\tb"\n'            # tab
           '202610,P5,4,"line1\nline2"\n'    # 引號內換行
           '202610,P6,5,"x,y ""q"""\n')      # 引號內逗號與雙引號
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "m.csv", csv))
    assert st == "CLEANED", importer.get_load(conn, lid)["error"]
    got = {r[0]: (r[1], r[4]) for r in rows(conn, dataset)}
    assert got == {"P1": (None, None), "P2": (Decimal(1), "NA"), "P3": (Decimal(2), "N/A"),
                   "P4": (Decimal(3), "a\tb"), "P5": (Decimal(4), "line1\nline2"),
                   "P6": (Decimal(5), 'x,y "q"')}
    assert importer.get_load(conn, lid)["header_match"] is True     # BOM 已去除才可能 MATCH


def test_ac7_nul_byte_rejected(conn, adapter, settings, dataset, tmp_path):
    lid, st = load(conn, adapter, settings, dataset,
                   write(tmp_path, "n.csv", b"yyyymm,policy_no,note\n202610,A,x\x00y\n"))
    assert st == "FAILED" and "NUL byte" in importer.get_load(conn, lid)["error"]


@pytest.mark.parametrize("body,needle", [
    ("202610,A,1,extra\n", "欄位數"),
    ('202610,A,"unterminated\n', "COPY 失敗"),
])
def test_ac7_structure_errors_with_location(conn, adapter, settings, dataset, tmp_path, body, needle):
    lid, st = load(conn, adapter, settings, dataset,
                   write(tmp_path, "s.csv", "yyyymm,policy_no,amt\n202610,OK,1\n" + body))
    err = importer.get_load(conn, lid)["error"]
    assert st == "FAILED" and needle in err and "診斷" in err


def test_type_errors_produce_report_and_reject_whole_file(conn, adapter, settings, dataset, tmp_path):
    seed(conn, adapter, settings, dataset, tmp_path)
    before = snapshot(conn, dataset)
    csv = ("yyyymm,policy_no,amt,qty,eff_date,note\n"
           "202610,G1,1.234,1,2026-10-01,ok\n"         # 小數位數超過 2 → 拒絕（不四捨五入）
           "202610,G2,1,abc,2026-02-30,ok\n"           # 整數與日期都錯
           "202609,G3,1,1,2026-10-01,ok\n"             # 期別不符
           "202610,G4,1,1,2026-10-01," + "z" * 51 + "\n")
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "e.csv", csv))
    ld = importer.get_load(conn, lid)
    assert st == "FAILED" and "4 列格式錯誤" in ld["error"]
    report = (settings.error_report_dir / f"{lid}.csv").read_text()
    assert "amt:numeric(18,2) 格式不符" in report and "qty:integer 格式不符" in report
    assert "eff_date:date 格式不符" in report and "yyyymm:期別不符" in report and "note:varchar(50)" in report
    assert snapshot(conn, dataset) == before                    # 整檔拒絕，正式資料不變


def test_duplicate_pk_rejected(conn, adapter, settings, dataset, tmp_path):
    lid, st = load(conn, adapter, settings, dataset,
                   write(tmp_path, "d.csv", "yyyymm,policy_no\n202610,A\n202610,A\n"))
    assert st == "FAILED" and "主鍵重複 1 組" in importer.get_load(conn, lid)["error"]


def test_gzip_and_ratio_limit(conn, adapter, settings, dataset, tmp_path):
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "g.csv.gz", gzip.compress(BASE.encode())))
    assert st == "CLEANED" and snapshot(conn, dataset)[0] == 3
    bomb = "yyyymm,policy_no,note\n" + "".join(f"202610,B{i},{'a' * 4000}\n" for i in range(5_000))
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "bomb.csv.gz", gzip.compress(bomb.encode(), 9)))
    assert st == "FAILED" and "解壓後大小超過限制" in importer.get_load(conn, lid)["error"]


# ---------------------------------------------------------------- AC5 原子性
@pytest.mark.parametrize("state", ["STAGED", "VALIDATED", "BUILT"])
def test_ac5_failure_at_each_state_keeps_data(conn, adapter, settings, dataset, tmp_path, state):
    seed(conn, adapter, settings, dataset, tmp_path)
    before = snapshot(conn, dataset)

    def boom():
        raise RuntimeError(f"injected at {state}")
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "v2.csv", "yyyymm,policy_no,amt\n202610,N,9\n"),
                   hooks={state: boom})
    ld = importer.get_load(conn, lid)
    assert st == "FAILED" and f"injected at {state}" in ld["error"]
    assert snapshot(conn, dataset) == before
    n = importer.names(dataset, lid)
    assert not adapter.table_exists(conn, n["stg"]) and not adapter.table_exists(conn, n["newp"])


def test_ac5_failure_inside_swap_transaction_rolls_back(conn, adapter, settings, dataset, tmp_path, monkeypatch):
    """DETACH + RENAME 已執行後才失敗：整個交易回滾，舊分割仍掛在父表上。"""
    seed(conn, adapter, settings, dataset, tmp_path)
    before = snapshot(conn, dataset)
    real = adapter.swap_period

    def half_swap(c, ds, p, newp, bak, chk):
        c.execute(psycopg.sql.SQL("ALTER TABLE {} DETACH PARTITION {}").format(
            psycopg.sql.Identifier(ds.name), psycopg.sql.Identifier(partition_name(ds, p))))
        raise RuntimeError("crash after detach")
    monkeypatch.setattr(adapter, "swap_period", half_swap)
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "v2.csv", "yyyymm,policy_no\n202610,N\n"))
    monkeypatch.setattr(adapter, "swap_period", real)
    assert st == "FAILED" and "crash after detach" in importer.get_load(conn, lid)["error"]
    assert adapter.partition_exists(conn, dataset, P)
    assert snapshot(conn, dataset) == before


@pytest.mark.parametrize("state", ["STAGED", "VALIDATED", "BUILT"])
def test_crash_recovery_resumes(conn, adapter, settings, dataset, tmp_path, state):
    """程序在某狀態後當掉（連線中斷，advisory lock 釋放）→ recover 接手並完成。"""
    seed(conn, adapter, settings, dataset, tmp_path)

    def crash():
        raise SimulatedCrash()
    path = write(tmp_path, "v2.csv", "yyyymm,policy_no,amt\n202610,R1,5\n202610,R2,6\n")
    up = upload.register_local_file(conn, settings, path)
    lid = importer.create_load(conn, adapter, dataset=dataset.name, yyyymm=P, mode="replace",
                               upload_id=up["upload_id"])
    c1 = psycopg.connect(DSN, autocommit=True)
    with pytest.raises(SimulatedCrash):
        importer.run_load(c1, adapter, lid, settings=settings, hooks={state: crash})
    assert importer.get_load(conn, lid)["status"] == state
    assert snapshot(conn, dataset) == (3, Decimal("80.25"))       # 正式資料尚未改變
    # 持有者還活著時 recover 不得接手
    assert lid not in importer.recover(conn, adapter, settings=settings)
    c1.close()                                                     # 模擬程序死亡
    todo = importer.recover(conn, adapter, settings=settings)
    assert lid in todo
    assert importer.run_load(conn, adapter, lid, settings=settings) == "CLEANED"
    assert snapshot(conn, dataset) == (2, Decimal("11"))


# ---------------------------------------------------------------- AC6 冪等
def test_ac6_swap_not_repeated(conn, adapter, settings, dataset, tmp_path):
    seed(conn, adapter, settings, dataset, tmp_path)

    def crash():
        raise SimulatedCrash()
    path = write(tmp_path, "v2.csv", "yyyymm,policy_no\n202610,S1\n")
    up = upload.register_local_file(conn, settings, path)
    lid = importer.create_load(conn, adapter, dataset=dataset.name, yyyymm=P, mode="replace",
                               upload_id=up["upload_id"])
    with pytest.raises(SimulatedCrash):                            # SWAP 已提交、清理前中斷
        importer.run_load(conn, adapter, lid, settings=settings, hooks={"SWAPPED": crash})
    bak = importer.get_load(conn, lid)["bak_table"]
    assert importer.run_load(conn, adapter, lid, settings=settings) == "CLEANED"
    assert importer.run_load(conn, adapter, lid, settings=settings) == "CLEANED"   # 再跑一次不重做
    baks = conn.execute("SELECT count(*) FROM pg_tables WHERE tablename LIKE %s",
                        (f"{dataset.name}_bak_%",)).fetchall()[0][0]
    assert baks == 1 and importer.get_load(conn, lid)["bak_table"] == bak
    assert snapshot(conn, dataset)[0] == 1


def test_one_active_load_per_period(conn, adapter, settings, dataset, tmp_path):
    p1 = upload.register_local_file(conn, settings, write(tmp_path, "a.csv", BASE))
    p2 = upload.register_local_file(conn, settings, write(tmp_path, "b.csv", BASE))
    importer.create_load(conn, adapter, dataset=dataset.name, yyyymm=P, mode="replace", upload_id=p1["upload_id"])
    with pytest.raises(importer.LoadError, match="已有進行中的工作"):
        importer.create_load(conn, adapter, dataset=dataset.name, yyyymm=P, mode="replace",
                             upload_id=p2["upload_id"])


# ---------------------------------------------------------------- 8.1 增修
def test_upsert_counts_and_partial_columns(conn, adapter, settings, dataset, tmp_path):
    seed(conn, adapter, settings, dataset, tmp_path)
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "u.csv",
                   "policy_no,amt\nA001,999.99\nNEW1,1\n"), mode="upsert")   # 無期別欄 → 用 load 期別
    ld = importer.get_load(conn, lid)
    assert st == "CLEANED", ld["error"]
    assert (ld["rows_inserted"], ld["rows_updated"]) == (1, 1)
    got = {r[0]: r for r in rows(conn, dataset)}
    assert got["A001"][1] == Decimal("999.99") and got["A001"][2] == 1 and got["A001"][4] == "x"  # 未給欄位保留
    assert got["NEW1"][1] == Decimal(1) and len(got) == 4


def test_upsert_requires_existing_period(conn, adapter, settings, dataset, tmp_path):
    up = upload.register_local_file(conn, settings, write(tmp_path, "u.csv", "policy_no\nA\n"))
    with pytest.raises(importer.LoadError, match="期別 202610 不存在"):
        importer.create_load(conn, adapter, dataset=dataset.name, yyyymm=P, mode="upsert",
                             upload_id=up["upload_id"])


# ---------------------------------------------------------------- AC8 刪除一致性
def test_ac8_delete_preview_confirm_restore(conn, adapter, settings, dataset, tmp_path):
    seed(conn, adapter, settings, dataset, tmp_path)
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "del.csv",
                   "yyyymm,policy_no\n202610,A001\n202610,NOPE\n"), mode="delete")
    ld = importer.get_load(conn, lid)
    assert st == "PREVIEWED", ld["error"]
    assert (ld["rows_matched"], ld["rows_unmatched"]) == (1, 1)
    assert json.loads(ld["detail"])["missing_sample"] == [["202610", "NOPE"]]
    with pytest.raises(importer.LoadError, match="操作人"):
        importer.confirm_delete(conn, adapter, lid, operator=" ", settings=settings)
    # 1/3 > 10%，需要第二次確認
    with pytest.raises(importer.LoadError, match="10%"):
        importer.confirm_delete(conn, adapter, lid, operator="王小明", settings=settings)
    done = importer.confirm_delete(conn, adapter, lid, operator="王小明", confirm_large=True, settings=settings)
    assert done["status"] == "CLEANED" and done["rows_deleted"] == 1
    deleted = conn.execute(psycopg.sql.SQL("SELECT policy_no, amt, _operator_note FROM {} WHERE _load_id=%s").format(
        psycopg.sql.Identifier(dataset.deleted_table)), (lid,)).fetchall()
    assert deleted == [("A001", Decimal("100.50"), "王小明")]       # _deleted 列數 = 刪除數 = 預覽數
    assert snapshot(conn, dataset)[0] == 2
    restored, conflicts = adapter.restore_deleted(conn, dataset, lid)
    assert (restored, conflicts) == (1, 0) and snapshot(conn, dataset) == (3, Decimal("80.25"))


def test_ac8_mismatch_rolls_back(conn, adapter, settings, dataset, tmp_path, conn2):
    """預覽後有人先刪掉一筆 → 實際刪除數 ≠ 預覽數 → 回滾，_deleted 不留紀錄。"""
    seed(conn, adapter, settings, dataset, tmp_path)
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "del.csv",
                   "yyyymm,policy_no\n202610,A001\n202610,A002\n"), mode="delete")
    assert st == "PREVIEWED"
    conn2.execute(psycopg.sql.SQL("DELETE FROM {} WHERE policy_no='A002'").format(psycopg.sql.Identifier(dataset.name)))
    with pytest.raises(importer.LoadError, match="已回滾"):
        importer.confirm_delete(conn, adapter, lid, operator="x", confirm_large=True, settings=settings)
    assert snapshot(conn, dataset)[0] == 2                         # A001 仍在
    assert conn.execute(psycopg.sql.SQL("SELECT count(*) FROM {}").format(
        psycopg.sql.Identifier(dataset.deleted_table))).fetchall()[0][0] == 0
    assert importer.get_load(conn, lid)["status"] == "FAILED"


# ---------------------------------------------------------------- AC9 互斥
def test_ac9_backup_lock_blocks_swap(conn, adapter, settings, dataset, tmp_path, conn2):
    seed(conn, adapter, settings, dataset, tmp_path)
    adapter.session_lock_dataset_exclusive(conn2, dataset.dataset_id)      # 模擬備份中
    try:
        lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "v2.csv", "yyyymm,policy_no\n202610,W\n"))
        ld = importer.get_load(conn, lid)
        assert st == "BUILT" and ld["error"].startswith(importer.SWAP_WAIT)  # 等鎖逾時，保留 BUILT 等操作人
        assert snapshot(conn, dataset)[0] == 3
    finally:
        adapter.session_unlock_dataset_exclusive(conn2, dataset.dataset_id)
    importer.retry_swap(conn, lid)
    assert importer.run_load(conn, adapter, lid, settings=settings) == "CLEANED"
    assert snapshot(conn, dataset)[0] == 1


def test_ac9_add_column_after_built_fails_swap(conn, adapter, settings, dataset, tmp_path, conn2):
    seed(conn, adapter, settings, dataset, tmp_path)
    before = snapshot(conn, dataset)

    def add_col():
        catalog_repo.add_column(conn2, adapter, dataset.name, ColumnDef("extra", "varchar(10)", sort_order=9))
    lid, st = load(conn, adapter, settings, dataset, write(tmp_path, "v2.csv", "yyyymm,policy_no\n202610,W\n"),
                   hooks={"BUILT": add_col})
    err = importer.get_load(conn, lid)["error"]
    assert st == "FAILED" and "SchemaChanged" in err and "schema_version" in err
    assert snapshot(conn, dataset) == before
    cols = [c for c, _ in adapter.table_columns(conn, dataset.name)]
    assert "extra" in cols and "extra" in [c for c, _ in adapter.table_columns(conn, dataset.deleted_table)]


def test_add_column_waits_for_lock(conn, adapter, dataset, conn2):
    """欄位維護取資料集獨佔鎖：備份（獨佔）持有中 → lock_timeout 內取不到而失敗。"""
    adapter.session_lock_dataset_exclusive(conn2, dataset.dataset_id)
    try:
        conn.execute("SET lock_timeout = '300ms'")
        with pytest.raises(Exception, match="lock timeout|canceling statement due to lock timeout"):
            catalog_repo.add_column(conn, adapter, dataset.name, ColumnDef("x2", "integer"))
    finally:
        conn.execute("RESET lock_timeout")
        adapter.session_unlock_dataset_exclusive(conn2, dataset.dataset_id)


def test_drop_period(conn, adapter, settings, dataset, tmp_path):
    seed(conn, adapter, settings, dataset, tmp_path)
    lid = importer.drop_period(conn, adapter, dataset=dataset.name, yyyymm=P, operator="x", settings=settings)
    bak = importer.get_load(conn, lid)["bak_table"]
    assert not adapter.partition_exists(conn, dataset, P) and adapter.table_exists(conn, bak)
    assert snapshot(conn, dataset)[0] == 0


def test_primary_key_check(conn, adapter, dataset):
    adapter.check_primary_key(conn, dataset)
    conn.execute(psycopg.sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
        psycopg.sql.Identifier(dataset.name), psycopg.sql.Identifier(dataset.name + "_pkey")))
    with pytest.raises(Exception, match="主鍵不一致"):
        adapter.check_primary_key(conn, dataset)

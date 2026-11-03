"""驗證 sql/setup_roles.sql 設定的角色與權限，並列出 PostgreSQL 主要參數。

斷言：app_rw 能建表寫入；app_ro 能 SELECT、不能 INSERT/CREATE，且自動能讀 app_rw 新建的表。
連線取自環境變數 CQP_DSN_RW／CQP_DSN_RO（由 tools/withenv.py 從 .env 帶入）。
用法：python tools/withenv.py -- python tools/verify_db.py
"""
from __future__ import annotations

import os

import psycopg
from psycopg import errors


def require_dsn(kind: str) -> str:
    dsn = os.environ.get(f"CQP_DSN_{kind}")
    if not dsn:
        raise SystemExit(f"未設定 CQP_DSN_{kind}；請用 python tools/withenv.py -- ... 執行")
    return dsn


def main() -> int:
    rw = psycopg.connect(require_dsn("RW"), autocommit=True)
    ro = psycopg.connect(require_dsn("RO"), autocommit=True)
    rw.execute("DROP TABLE IF EXISTS verify_t")
    rw.execute("CREATE TABLE verify_t (a int)")
    rw.execute("INSERT INTO verify_t VALUES (1)")
    print("OK app_rw 建表、寫入")
    assert ro.execute("SELECT a FROM verify_t").fetchall() == [(1,)]
    print("OK app_ro 自動可 SELECT app_rw 新建的表（ALTER DEFAULT PRIVILEGES）")
    for sql, label in [("INSERT INTO verify_t VALUES (2)", "INSERT"), ("CREATE TABLE verify_x (a int)", "CREATE TABLE"),
                       ("DELETE FROM verify_t", "DELETE")]:
        try:
            ro.execute(sql)
        except errors.InsufficientPrivilege as e:
            print(f"OK app_ro {label} 被拒：{str(e).splitlines()[0]}")
        except errors.ReadOnlySqlTransaction as e:
            print(f"OK app_ro {label} 被拒（唯讀交易）：{str(e).splitlines()[0]}")
        else:
            raise AssertionError(f"app_ro 竟可執行 {label}")
    ro_flag = ro.execute("SHOW default_transaction_read_only").fetchone()[0]
    assert ro_flag == "on", ro_flag
    print("OK app_ro default_transaction_read_only = on")
    # 把唯讀旗標關掉後，仍應被「權限」擋下（證明防線不只靠旗標）
    ro.execute("SET default_transaction_read_only = off")
    for sql, label in [("INSERT INTO verify_t VALUES (2)", "INSERT"), ("CREATE TABLE verify_x (a int)", "CREATE TABLE")]:
        try:
            ro.execute(sql)
        except errors.InsufficientPrivilege as e:
            print(f"OK app_ro 關閉唯讀旗標後 {label} 仍被權限擋下：{str(e).splitlines()[0]}")
        else:
            raise AssertionError(f"app_ro 關閉唯讀旗標後竟可執行 {label}")
    for p in ["shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem",
              "max_parallel_workers_per_gather", "max_wal_size", "max_connections", "hash_mem_multiplier"]:
        print(f"   {p} = {rw.execute('SELECT current_setting(%s)', (p,)).fetchone()[0]}")
    rw.execute("DROP TABLE verify_t")
    print("全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

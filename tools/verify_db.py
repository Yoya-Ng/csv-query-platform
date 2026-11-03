"""驗證 initdb 的角色與權限（需先 up -d db，使用 env/local.env 與 127.0.0.1:55432）。

斷言：app_rw 能建表寫入；app_ro 能 SELECT、不能 INSERT/CREATE，且自動能讀 app_rw 新建的表；
      PostgreSQL 參數已由 env 帶入。
用法：.venv/bin/python tools/verify_db.py [--env env/local.env] [--port 55432]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import psycopg
from psycopg import errors

ROOT = Path(__file__).resolve().parent.parent


def load_env(p: Path) -> dict[str, str]:
    d = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=str(ROOT / "env" / "local.env"))
    ap.add_argument("--port", type=int, default=55432)
    a = ap.parse_args()
    env = load_env(Path(a.env))
    rw = psycopg.connect(host="127.0.0.1", port=a.port, dbname="cqp", user="app_rw",
                         password=env["CQP_RW_PASSWORD"], autocommit=True)
    ro = psycopg.connect(host="127.0.0.1", port=a.port, dbname="cqp", user="app_ro",
                         password=env["CQP_RO_PASSWORD"], autocommit=True)
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

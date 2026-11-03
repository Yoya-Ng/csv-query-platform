"""AC1（匯入速度）／AC2（匯出速度）：同一檔案各跑 N 次（預設 3）取中位數，以 MB/s 輸出比較表。

比較項目
  匯入：psql \\copy（本機檔案 / 以 stdin 管線送入）、psycopg 3 cursor.copy() 串流（8MB 區塊）、
        JDBC CopyManager、JDBC batch insert（reWriteBatchedInserts）
  匯出：psql \\copy 到本機檔案 / 到 stdout 管線、psycopg 3 迭代 copy、psycopg 3 copy.read()、JDBC copyOut

量測偏差（務必看）
  - psql 每次呼叫含行程啟動與連線建立成本，對小檔影響很大、對 GB 級檔案可忽略；本腳本另量
    「psql -c 'select 1'」基線，並同時輸出「扣除基線」的 psql 數字。
  - psql、psycopg、JDBC 都在執行本腳本的主機上，經同一條網路路徑連 CQP_DSN_RW 指向的 DB。
  - 2 萬筆結果只證明腳本可用，不代表 AC 驗收。

用法（需主機上有 psql）
  python tools/withenv.py -- python bench/ac1_ac2.py --rows 20000 [--repeats 3]
壓測表為 bench_policy，結束（含失敗）一定 DROP。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import gen_data  # noqa: E402
from verify_db import require_dsn  # noqa: E402

TABLE = "bench_policy"
BLOCK = 8 * 1024 * 1024


def ddl(defn: dict) -> str:
    cols = ", ".join(f'{c["code"]} {c["data_type"]}' + (" NOT NULL" if c["required"] else "") for c in defn["columns"])
    return f"CREATE TABLE {TABLE} ({cols})"


def timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def med_runs(fn, repeats: int, before=None) -> list[float]:
    out = []
    for _ in range(repeats):
        if before:
            before()
        out.append(timed(fn))
    return out


def build_jdbc_url(params: dict) -> str | None:
    """由 libpq 連線參數組 pgjdbc URL，保留 sslmode；多主機與 Unix socket 無法對應時回 None。"""
    host = str(params.get("host") or "127.0.0.1")
    port = str(params.get("port") or "5432")
    if host.startswith("/") or "," in host or "," in port:
        return None
    if ":" in host:                                   # IPv6 位址要加中括號
        host = f"[{host}]"
    url = f"jdbc:postgresql://{host}:{port}/{quote(str(params.get('dbname') or 'cqp'), safe='')}"
    q = {"reWriteBatchedInserts": "true"}
    if params.get("sslmode"):
        q["sslmode"] = str(params["sslmode"])
    return f"{url}?{urlencode(q)}"


def find_jdbc_jar() -> Path | None:
    base = Path.home() / ".m2/repository/org/postgresql/postgresql"
    if not base.exists():
        return None
    jars = sorted(base.glob("*/postgresql-*.jar"), key=lambda p: [int(x) if x.isdigit() else 0 for x in p.parent.name.split(".")])
    return jars[-1] if jars else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--workdir", type=Path, default=ROOT / "data" / "bench")
    ap.add_argument("--skip-jdbc", action="store_true")
    a = ap.parse_args()

    dsn = require_dsn("RW")
    params = conninfo_to_dict(dsn)
    pw = params.pop("password", None) or ""
    a.workdir.mkdir(parents=True, exist_ok=True)
    src = a.workdir / f"bench_{a.rows}.csv"
    defn = gen_data.build_policy_def(200, TABLE)
    gen_data.write_csv(defn, "policy", src, a.rows, 202610, 7, 0.05, False, False, a.rows)
    size = src.stat().st_size
    mb = size / 1e6
    print(f"測試檔：{src.name}，{a.rows} 列 × 200 欄，{mb:.1f} MB；每項 {a.repeats} 次取中位數", file=sys.stderr)

    conn = psycopg.connect(dsn, autocommit=True)
    # 密碼走 PGPASSWORD，不放進命令列參數（避免出現在行程清單）；DSN 沒有密碼時沿用使用者原本的設定（.pgpass 等）
    penv = dict(os.environ)
    if pw:
        penv["PGPASSWORD"] = pw
    # 選項放在連線字串前面：舊版 Windows psql 的 getopt 不會重排參數
    psql = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", make_conninfo(**params)]

    def run_psql(extra: list[str], **kw):
        return subprocess.run(psql + extra, env=penv, check=True, **kw)
    results: dict[str, list[float]] = {}
    notes: dict[str, str] = {}

    def trunc():
        conn.execute(f"TRUNCATE {TABLE}")

    def count() -> int:
        return conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]

    try:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(ddl(defn))

        # ---- 基線：psql 啟動與連線成本
        base = med_runs(lambda: run_psql(["-c", "select 1"], capture_output=True), a.repeats)
        base_med = statistics.median(base)
        print(f"基線 psql 啟動成本（中位數）：{base_med:.3f}s", file=sys.stderr)

        # ---- 匯入（\copy 的檔名用 psql 字面量引號；單引號加倍）
        src_lit = src.resolve().as_posix().replace("'", "''")

        def psql_file_in():
            run_psql(["-c", f"\\copy {TABLE} from '{src_lit}' csv header null ''"], capture_output=True)
        results["匯入 psql \\copy（本機檔案）"] = med_runs(psql_file_in, a.repeats, trunc)
        assert count() == a.rows, count()

        def psql_stdin_in():
            with open(src, "rb") as f:
                run_psql(["-c", f"\\copy {TABLE} from pstdin csv header null ''"], stdin=f, capture_output=True)
        results["匯入 psql \\copy（stdin 管線）"] = med_runs(psql_stdin_in, a.repeats, trunc)
        assert count() == a.rows, count()

        def psycopg_in():
            with conn.cursor() as cur, open(src, "rb") as f:
                with cur.copy(f"COPY {TABLE} FROM STDIN (FORMAT csv, HEADER true, NULL '')") as cp:
                    while chunk := f.read(BLOCK):
                        cp.write(chunk)
        results["匯入 psycopg3 cursor.copy()（8MB 區塊）"] = med_runs(psycopg_in, a.repeats, trunc)
        assert count() == a.rows, count()

        jar = None if a.skip_jdbc else find_jdbc_jar()
        jdbc_url = build_jdbc_url(params)
        jdbc_ok = False
        if a.skip_jdbc:
            notes["JDBC"] = "已用 --skip-jdbc 跳過"
        elif jdbc_url is None:
            notes["JDBC"] = "DSN 為多主機或 Unix socket，JDBC URL 無法對應，跳過"
        elif subprocess.run(["java", "-version"], capture_output=True).returncode != 0:
            notes["JDBC"] = "本機無 java，跳過"
        elif jar is None:
            notes["JDBC"] = "找不到 ~/.m2 內的 pgjdbc jar（本機也無 mvn），跳過"
        else:
            jdbc_ok = True
            notes["JDBC"] = f"java + {jar.name}（單檔原始碼啟動 bench/JdbcBench.java，未用 mvn）"

        def jdbc(mode: str, f: Path) -> list[float]:
            # 密碼經 penv 的 PGPASSWORD 帶入，第 3 個參數固定 "-"
            r = subprocess.run(["java", "-cp", str(jar), str(ROOT / "bench" / "JdbcBench.java"), jdbc_url,
                                params.get("user", "app_rw"), "-",
                                mode, TABLE, str(f), str(a.repeats)], env=penv, check=True, capture_output=True,
                               text=True)
            return [float(x.split()[1]) for x in r.stdout.splitlines() if x.startswith("RUN")]

        if jdbc_ok:
            results["匯入 JDBC CopyManager.copyIn"] = jdbc("copy_in", src)
            assert count() == a.rows, count()
            results["匯入 JDBC batch insert（1000/批）"] = jdbc("batch_in", src)
            assert count() == a.rows, count()

        # ---- 匯出（先確保表內有完整資料）
        trunc()
        psycopg_in()
        assert count() == a.rows

        out_file_lit = (a.workdir / "out_psql_file.csv").resolve().as_posix().replace("'", "''")

        def psql_file_out():
            run_psql(["-c", f"\\copy (select * from {TABLE}) to '{out_file_lit}' csv header"], capture_output=True)
        results["匯出 psql \\copy（本機檔案）"] = med_runs(psql_file_out, a.repeats)

        outp = a.workdir / "out_psql.csv"

        def psql_stdout_out():
            with open(outp, "wb") as f:
                run_psql(["-c", f"\\copy (select * from {TABLE}) to pstdout csv header"], stdout=f)
        results["匯出 psql \\copy（stdout 管線）"] = med_runs(psql_stdout_out, a.repeats)
        out_size = outp.stat().st_size

        outq = a.workdir / "out_psycopg.csv"

        def psycopg_iter_out():
            with conn.cursor() as cur, open(outq, "wb") as f:
                with cur.copy(f"COPY (SELECT * FROM {TABLE}) TO STDOUT (FORMAT csv, HEADER true)") as cp:
                    for data in cp:
                        f.write(data)
        results["匯出 psycopg3 迭代 copy"] = med_runs(psycopg_iter_out, a.repeats)

        def psycopg_read_out():
            with conn.cursor() as cur, open(outq, "wb") as f:
                with cur.copy(f"COPY (SELECT * FROM {TABLE}) TO STDOUT (FORMAT csv, HEADER true)") as cp:
                    while data := cp.read():
                        f.write(data)
        results["匯出 psycopg3 copy.read()"] = med_runs(psycopg_read_out, a.repeats)
        assert outq.stat().st_size == out_size, (outq.stat().st_size, out_size)

        if jdbc_ok:
            results["匯出 JDBC CopyManager.copyOut"] = jdbc("copy_out", a.workdir / "jdbc_target.csv")

        # ---- 輸出
        print()
        print(f"檔案 {mb:.1f} MB（匯入）/ {out_size / 1e6:.1f} MB（匯出）；{a.rows} 列；每項 {a.repeats} 次取中位數")
        print(f"{'項目':<44}{'中位數(s)':>10}{'MB/s':>9}{'扣基線 MB/s':>13}   各次(s)")
        for k, v in results.items():
            m = statistics.median(v)
            sz = mb if k.startswith("匯入") else out_size / 1e6
            adj = f"{sz / max(m - base_med, 1e-9):>13.1f}" if "psql" in k else f"{'-':>13}"
            print(f"{k:<{44 - sum(1 for ch in k if ord(ch) > 0x2e80)}}{m:>10.3f}{sz / m:>9.1f}{adj}   "
                  f"{', '.join(f'{x:.3f}' for x in v)}")
        print(f"\n基線（psql select 1，中位數）：{base_med:.3f}s")
        for k, v in notes.items():
            print(f"{k}：{v}")
        print(json.dumps({k: statistics.median(v) for k, v in results.items()}, ensure_ascii=False), file=sys.stderr)
    finally:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

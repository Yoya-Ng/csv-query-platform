"""把 fixtures 直接用 COPY（FORMAT csv, HEADER true, NULL '', FORCE_NULL）餵進臨時表，核對 EXPECT 中屬於 COPY 層的預期。
表頭驗證類（duplicate/unknown/missing）與 NUL 屬 App 層，這裡只確認 COPY 本身的行為。
用法：.venv/bin/python tools/check_fixtures_copy.py   （需 db 容器在 127.0.0.1:55432 運作）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402
from verify_db import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def load(cur, path: Path, cols: str) -> str | None:
    cur.execute("TRUNCATE fx_t")
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]                      # App 層剝除 BOM
    body = data.split(b"\n", 1)[1]           # 表頭已由 App 驗證，這裡只送資料列並給明確欄位清單
    try:
        with cur.copy(f"COPY fx_t ({cols}) FROM STDIN (FORMAT csv, NULL '', FORCE_NULL (note, amt))") as cp:
            cp.write(body)
    except psycopg.Error as e:
        return str(e).splitlines()[0]
    return None


def main() -> int:
    env = load_env(ROOT / "env" / "local.env")
    conn = psycopg.connect(host="127.0.0.1", port=55432, dbname="cqp", user="app_rw", password=env["CQP_RW_PASSWORD"],
                           autocommit=True)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS fx_t")
    cur.execute("CREATE TABLE fx_t (yyyymm integer NOT NULL, policy_no varchar(20) NOT NULL, note varchar(100), "
                "amt numeric(18,2))")
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for name, fn in fixtures.ALL.items():
            ex = fixtures.EXPECT[name]
            if ex.get("reject_stage") in ("header", "nul"):
                continue
            p = fn(d)
            cols = "amt, note, policy_no, yyyymm" if name == "shuffled_header" else "yyyymm, policy_no, note, amt"
            err = load(cur, p, cols)
            if ex["ok"]:
                rows = cur.execute("SELECT yyyymm, policy_no, note, amt, note IS NULL FROM fx_t ORDER BY policy_no").fetchall()
                good = err is None and len(rows) == ex["rows"]
                if good and "note" in ex:
                    good = rows[0][2] == ex["note"] and (ex["note"] is not None or rows[0][4] is True)
                if good and "notes" in ex:
                    good = sorted(r[2] for r in rows) == sorted(ex["notes"])
                if good and "row" in ex:
                    good = (rows[0][0], rows[0][1], rows[0][2], str(rows[0][3])) == tuple(ex["row"].values())
                if good and name == "quoted_empty":
                    good = cur.execute("SELECT count(*) FROM fx_t WHERE note = ''").fetchone()[0] == 0
                print(("OK  " if good else "FAIL"), name, rows if good else (err, rows))
            else:
                good = err is not None and ex["msg_contains"] in err and \
                    cur.execute("SELECT count(*) FROM fx_t").fetchone()[0] == 0
                print(("OK  " if good else "FAIL"), name, err)
            bad += not good
    cur.execute("DROP TABLE fx_t")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

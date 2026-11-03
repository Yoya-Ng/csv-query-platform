"""查詢執行（SPEC 9.2、9.4、9.5）：預覽、總筆數、複製（TSV）、匯出（串流）。

資源保護一律用 advisory lock 名額實作，因此在多個 uvicorn worker 之間是全域的：
- 大查詢併發 4、匯出併發 2
- 每瀏覽器 session 同時 1 查詢 + 1 匯出；每 IP 同時最多 3
名額持有在執行查詢的那條連線上，連線歸還或斷線即釋放。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from app.catalog import DatasetDef
from app.config import Settings, get_settings
from app.db.adapters.base import Adapter
from app.db.pool import get_pool
from app.db.sqlexec import compile_pg
from app.services.export import stream_export, to_tsv
from app.services.query.spec import CompiledQuery, QuerySpecError, compile_query, count_query, paginate

BIG_TABLE_ROWS = 10_000_000


class Busy(RuntimeError):
    """名額已滿（HTTP 429）。"""


_EST_TTL = 60.0
_est_cache: dict[str, tuple[float, int]] = {}


def big_tables(conn, adapter: Adapter, catalog: dict[str, DatasetDef], names: set[str] | None = None) -> set[str]:
    """筆數估計值 > 1,000 萬的資料集。只估查詢用到的表，結果快取 60 秒（reltuples 只在 ANALYZE 後變動）。"""
    now = time.monotonic()
    out = set()
    for n in (names if names is not None else catalog):
        hit = _est_cache.get(n)
        if hit is None or now - hit[0] > _EST_TTL:
            hit = (now, adapter.estimate_rows(conn, catalog[n]))
            _est_cache[n] = hit
        if hit[1] > BIG_TABLE_ROWS:
            out.add(n)
    return out


def _used_tables(spec: dict, catalog) -> set[str]:
    names = {spec.get("from")} | {j.get("table") for j in spec.get("joins", []) if isinstance(j, dict)}
    return {n for n in names if isinstance(n, str) and n in catalog}


def check_joins(spec: dict, catalog: dict[str, DatasetDef], *, allow_many_to_many: bool) -> list[str]:
    """以 metadata 主鍵判斷 join 基數：兩側 join 鍵都沒涵蓋各自主鍵 → 多對多，需使用者確認。"""
    warnings = []
    base = spec.get("from")
    for j in spec.get("joins", []):
        right = catalog[j["table"]]
        lkeys = set()
        rkeys = {str(r).rpartition(".")[2] for _, r in j["on"]}    # 右欄可寫 表.欄 或 欄
        for l, _ in j["on"]:
            t, _, c = l.rpartition(".")
            if (t or base) == base:
                lkeys.add(c)
        left = catalog[base]
        r_unique = {c.code for c in right.pk_columns} <= rkeys if right.pk_columns else False
        l_unique = {c.code for c in left.pk_columns} <= lkeys if left.pk_columns else False
        if not (r_unique or l_unique):
            msg = f"{base} 與 {right.name} 的 join 鍵在兩側都不唯一（多對多），結果筆數可能暴增"
            if not allow_many_to_many:
                raise QuerySpecError(msg + "；確認後請勾選允許多對多")
            warnings.append(msg)
    return warnings


@contextmanager
def _slots(conn, adapter: Adapter, wants: list[tuple[str, str, int]], *, wait: float):
    """依序取得多個名額；任一取不到就釋放已取得者。wait > 0 時輪詢等待（排隊）。"""
    got: list[int] = []
    deadline = time.monotonic() + wait
    try:
        for ns, ident, n in wants:
            while (k := adapter.try_slot(conn, ns, ident, n)) is None:
                if time.monotonic() > deadline:
                    raise Busy(f"{ns} 名額已滿，請稍後再試")
                time.sleep(0.2)
            got.append(k)
        yield
    finally:
        for k in got:
            adapter.release_slot(conn, k)


def _jsonable(v):
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _prepare(spec, catalog, adapter, allow_many) -> tuple[CompiledQuery, list[str], bool]:
    if not isinstance(spec, dict):
        raise QuerySpecError("查詢規格必須是 JSON 物件")
    with get_pool("system").connection() as c:
        bigs = big_tables(c, adapter, catalog, _used_tables(spec, catalog))
    cq = compile_query(spec, catalog, big_tables=bigs)
    warnings = check_joins(spec, catalog, allow_many_to_many=allow_many)
    return cq, warnings, bool(bigs)


def _stable_order(spec, catalog):
    """預覽以 OFFSET 分頁：沒有 ORDER BY 時 PostgreSQL 不保證順序，頁與頁可能重複或遺漏。
    使用者沒指定排序時，以主表主鍵排序（只用於預覽，不改變匯出與儲存的規格）。"""
    if not isinstance(spec, dict) or spec.get("order") or spec.get("from") not in catalog:
        return spec
    ds = catalog[spec["from"]]
    if not ds.pk_columns:
        return spec
    return {**spec, "order": [[f"{ds.name}.{c.code}", "asc", "nulls_last"] for c in ds.pk_columns]}


def _query_slots(s: Settings, session_id: str, client_ip: str, is_big: bool):
    """每 session 1、每 IP 3；只有用到大表的查詢才佔「大查詢」名額（併發 4，排隊最多 60 秒）。"""
    personal = [("session-query", session_id, 1), ("ip", client_ip, 3)]
    big = [("big-query", "all", s.big_query_concurrency)] if is_big else []
    return personal, big


def preview(spec: dict, catalog: dict[str, DatasetDef], adapter: Adapter, *, session_id: str, client_ip: str,
            offset: int = 0, limit: int = 1000, allow_many: bool = False, job_id: str | None = None,
            settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    spec = _stable_order(spec, catalog)
    cq, warnings, is_big = _prepare(spec, catalog, adapter, allow_many)
    paginate(cq, offset, limit)                      # 檢查 offset / limit 範圍
    stmt = cq.select.offset(offset).limit(limit + 1)  # 多取 1 筆判斷 has_more
    job_id = job_id or str(uuid.uuid4())
    sql_text, params = compile_pg(stmt)
    with get_pool("query").connection() as conn:
        conn.autocommit = True
        personal, big = _query_slots(s, session_id, client_ip, is_big)
        with _slots(conn, adapter, personal, wait=0), _slots(conn, adapter, big, wait=60):
            adapter.tag_connection(conn, f"cqp-q-{job_id}")
            try:
                with conn.transaction():
                    adapter.set_timeout(conn, s.query_statement_timeout_ms)
                    est = adapter.explain_estimate(conn, sql_text, params)
                    rows = conn.execute(sql_text, params).fetchall()
            finally:
                adapter.tag_connection(conn, "cqp-query")
    hints = list(warnings)
    if est["seq_scans"]:
        hints.append(f"執行計畫含全表掃描：{', '.join(t for t in est['seq_scans'] if t)}")
    return {"job_id": job_id, "columns": cq.columns, "rows": [[_jsonable(v) for v in r] for r in rows[:limit]],
            "has_more": len(rows) > limit, "estimate": est, "hints": hints}


def count(spec, catalog, adapter, *, session_id, client_ip, allow_many=False, settings=None) -> int:
    s = settings or get_settings()
    cq, _, is_big = _prepare(spec, catalog, adapter, allow_many)
    sql_text, params = compile_pg(count_query(cq))
    with get_pool("query").connection() as conn:
        conn.autocommit = True
        personal, big = _query_slots(s, session_id, client_ip, is_big)
        with _slots(conn, adapter, personal, wait=0), _slots(conn, adapter, big, wait=60):
            with conn.transaction():
                adapter.set_timeout(conn, s.query_statement_timeout_ms)
                return conn.execute(sql_text, params).fetchall()[0][0]


def copy_tsv(spec, catalog, adapter, *, session_id, client_ip, allow_many=False, settings=None,
             max_rows=10_000) -> str:
    s = settings or get_settings()
    cq, _, is_big = _prepare(spec, catalog, adapter, allow_many)
    sql_text, params = compile_pg(cq.select.limit(max_rows + 1))
    with get_pool("query").connection() as conn:
        conn.autocommit = True
        personal, big = _query_slots(s, session_id, client_ip, is_big)
        with _slots(conn, adapter, personal, wait=0), _slots(conn, adapter, big, wait=60):
            with conn.transaction():
                adapter.set_timeout(conn, s.query_statement_timeout_ms)
                rows = conn.execute(sql_text, params).fetchall()
    return to_tsv(cq.columns, rows, max_rows=max_rows)


def export(spec, catalog, adapter, *, session_id, client_ip, encoding="utf-8-sig", gzip=False,
           allow_many=False, settings=None) -> Iterator[bytes]:
    """回傳 generator；呼叫端（StreamingResponse）結束或斷線時釋放連線與名額。"""
    s = settings or get_settings()
    cq, _, is_big = _prepare(spec, catalog, adapter, allow_many)
    return ExportStream(get_pool("export"), adapter, cq, s, session_id=session_id, client_ip=client_ip,
                        encoding=encoding, gzip=gzip)


class ExportStream:
    """匯出串流：建立時就取得連線與名額（取不到可回 429），由 close() 釋放。
    StreamingResponse 迭代結束、客戶端斷線（generator 關閉）或物件被回收時都會呼叫 close()，
    即使串流從未開始也不會遺留連線或名額。"""

    def __init__(self, pool, adapter: Adapter, cq: CompiledQuery, s: Settings, *, session_id: str,
                 client_ip: str, encoding: str, gzip: bool):
        self._pool, self._adapter, self._closed = pool, adapter, False
        self._conn = pool.getconn()
        try:
            self._slots = _slots(self._conn, adapter, [("session-export", session_id, 1), ("ip", client_ip, 3),
                                                       ("export", "all", s.export_concurrency)], wait=0)
            self._slots.__enter__()
        except BaseException:
            pool.putconn(self._conn)
            self._closed = True
            raise
        self._gen = self._run(cq, s, encoding, gzip)

    def _run(self, cq, s, encoding, gzip):
        try:
            self._adapter.set_session_timeout(self._conn, s.export_statement_timeout_ms)
            yield from stream_export(self._conn, cq.select, encoding=encoding, gzip=gzip)
        finally:
            self.close()

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return next(self._gen)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._gen.close()          # 若串流中途，觸發 stream_export 的取消
        except Exception:  # noqa: BLE001
            pass
        try:
            if not self._conn.closed:
                self._adapter.set_session_timeout(self._conn, None)
            self._slots.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - 連線已壞就交給 pool 丟棄
            pass
        self._pool.putconn(self._conn)

    def __del__(self):
        self.close()


def cancel(adapter: Adapter, job_id: str) -> bool:
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise QuerySpecError("job_id 格式錯誤") from None
    with get_pool("query").connection() as conn:
        conn.autocommit = True
        return adapter.cancel_tagged(conn, f"cqp-q-{job_id}")

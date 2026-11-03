"""連線池（SPEC 9.5：查詢 12、匯入 2、匯出 4、系統 2）。查詢與匯出用唯讀角色 app_ro。"""
from __future__ import annotations

import threading

from psycopg_pool import ConnectionPool

from app.config import get_settings

_pools: dict[str, ConnectionPool] = {}
_lock = threading.Lock()


def _make(name: str) -> ConnectionPool:
    s = get_settings()
    dsn, size = {
        "query": (s.dsn_ro, s.pool_query),
        "export": (s.dsn_ro, s.pool_export),
        "import": (s.dsn_rw, s.pool_import),
        "system": (s.dsn_rw, s.pool_system),
    }[name]
    return ConnectionPool(
        dsn, min_size=0, max_size=size, open=True, name=name, reset=_reset,
        kwargs={"application_name": f"cqp-{name}", "autocommit": True},
    )


def _reset(conn) -> None:
    """歸還連線時釋放所有 session advisory lock（工作擁有權、名額）並還原設定，避免例外路徑殘留。"""
    conn.execute("SELECT pg_advisory_unlock_all()")
    conn.execute("RESET ALL")


def get_pool(name: str) -> ConnectionPool:
    with _lock:
        if name not in _pools:
            _pools[name] = _make(name)
        return _pools[name]


def close_all() -> None:
    with _lock:
        for p in _pools.values():
            p.close()
        _pools.clear()

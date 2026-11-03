"""背景工作執行器（SPEC 2 job runner）：匯入、切換、清理；不依附 HTTP 請求，前端輪詢 load_log。

每個 uvicorn worker 各有一個執行緒池（大小 = 匯入連線池 2）。工作擁有權由 advisory lock 決定，
所以同一 load 不會被兩個 worker 同時執行；維護執行緒定期呼叫 recover() 接手孤兒工作。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings
from app.db.adapters.postgres import get_adapter
from app.db.pool import get_pool
from app.services import importer, upload

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_stop = threading.Event()
_maint: threading.Thread | None = None


def _run(load_id: str) -> None:
    adapter = get_adapter()
    try:
        with get_pool("import").connection() as conn:
            importer.run_load(conn, adapter, load_id)
    except Exception:  # noqa: BLE001
        log.exception("job %s crashed", load_id)


def submit(load_id: str) -> None:
    assert _executor is not None, "job runner 尚未啟動"
    _executor.submit(_run, load_id)


def _maintenance(interval: float) -> None:
    adapter = get_adapter()
    ticks = 0
    while not _stop.wait(interval):
        ticks += 1
        try:
            with get_pool("system").connection() as conn:
                for lid in importer.recover(conn, adapter):
                    submit(lid)
                if ticks % 60 == 0:      # 約每小時
                    importer.purge_backups(conn, adapter)
                    importer.purge_deleted(conn, adapter)
                    upload.expire_stale(conn, get_settings())
        except Exception:  # noqa: BLE001
            log.exception("maintenance failed")


def start(interval: float = 60.0) -> None:
    global _executor, _maint
    s = get_settings()
    _executor = ThreadPoolExecutor(max_workers=s.pool_import, thread_name_prefix="cqp-job")
    _stop.clear()
    _maint = threading.Thread(target=_maintenance, args=(interval,), daemon=True, name="cqp-maint")
    _maint.start()


def stop() -> None:
    _stop.set()
    if _executor:
        _executor.shutdown(wait=False, cancel_futures=True)

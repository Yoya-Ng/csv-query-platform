"""匯入狀態機（SPEC 7.4、8）。

整期重傳：UPLOADED → STAGED → VALIDATED → BUILT → SWAPPED（唯一提交點）→ CLEANED
增修    ：UPLOADED → STAGED → VALIDATED → APPLIED（單一交易）→ CLEANED
依鍵刪除：UPLOADED → STAGED → VALIDATED → PREVIEWED →（操作人確認）APPLIED → CLEANED

- 同一資料集同一期別同時只有一筆進行中（load_log partial unique index）。
- 兩層鎖只在提交交易內以 xact 鎖取得，順序固定：資料集（共享）→ 期別（獨佔）。
- 工作擁有權：執行中的工作持有 session advisory lock；crash 後連線斷開即釋放，
  recover() 只接手沒人持有的工作（多個 uvicorn worker 不會互搶）。
- service 層不寫資料庫專屬語法，一律透過 adapter。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import errors
from sqlalchemy import insert, select, update

from app.catalog import DatasetDef, check_period, partition_name
from app.config import Settings, get_settings
from app.db import catalog_repo
from app.db.adapters.base import Adapter, AdapterError, LockBusy, SchemaChanged
from app.db.meta import ACTIVE_STATES, load_log, upload_session
from app.db.sqlexec import execute
from app.services import csvfile, upload

log = logging.getLogger(__name__)

TERMINAL = ("CLEANED", "FAILED", "CANCELLED")
SWAP_WAIT = "SWAP_LOCK_TIMEOUT"
MAX_ATTEMPTS = 3


class LoadError(RuntimeError):
    pass


class SimulatedCrash(BaseException):
    """測試用：模擬程序在某狀態之後當掉（不走 FAILED 處理）。"""


Hooks = dict[str, Callable[[], None]]


def _now():
    return datetime.now(timezone.utc)


def names(ds: DatasetDef, load_id: str) -> dict[str, str]:
    k = load_id.replace("-", "")[:8]
    return {"stg": f"stg_{k}", "newp": f"{ds.name}_n_{k}", "chk": f"cqp_chk_{k}", "key": k}


def get_load(conn, load_id: str) -> dict:
    rows = execute(conn, select(load_log).where(load_log.c.load_id == load_id)).fetchall()
    if not rows:
        raise LoadError("load_id 不存在")
    return dict(rows[0]._mapping)


def _set(conn, load_id: str, **values) -> None:
    execute(conn, update(load_log).where(load_log.c.load_id == load_id).values(**values))


# --------------------------------------------------------------------------- 建立
def create_load(conn, adapter: Adapter, *, dataset: str, yyyymm: int, mode: str, upload_id: str,
                operator_note: str = "", client_ip: str = "") -> str:
    if mode not in ("replace", "upsert", "delete"):
        raise LoadError("mode 只能是 replace / upsert / delete")
    check_period(yyyymm)
    ds = catalog_repo.load_dataset(conn, dataset)
    if mode != "replace":
        if not ds.pk_columns:
            raise LoadError("沒有主鍵的資料集只能整期重傳")
        if not adapter.partition_exists(conn, ds, yyyymm):
            raise LoadError(f"期別 {yyyymm} 不存在，無法增修或刪除")
    rows = execute(conn, select(upload_session).where(upload_session.c.upload_id == upload_id)).fetchall()
    if not rows or rows[0]._mapping["status"] != "COMPLETE":
        raise LoadError("上傳尚未完成")
    up = rows[0]._mapping
    load_id = str(uuid.uuid4())
    try:
        with conn.transaction():
            used = execute(conn, update(upload_session).where(
                upload_session.c.upload_id == upload_id, upload_session.c.status == "COMPLETE")
                .values(status="USED")).rowcount
            if used != 1:
                raise LoadError("此上傳已被其他匯入使用，請重新上傳")
            execute(conn, insert(load_log).values(
                load_id=load_id, dataset_id=ds.dataset_id, yyyymm=yyyymm, mode=mode,
                upload_id=upload_id, filename=up["filename"], file_sha256=up["file_sha256"],
                status="UPLOADED", operator_note=operator_note[:200], client_ip=client_ip[:45]))
    except errors.UniqueViolation:
        raise LoadError(f"{ds.name} 期別 {yyyymm} 已有進行中的工作，請等待完成或取消") from None
    return load_id


# --------------------------------------------------------------------------- 執行
def run_load(conn, adapter: Adapter, load_id: str, *, settings: Settings | None = None,
             hooks: Hooks | None = None) -> str:
    """執行到下一個需要人為介入的狀態或終止狀態。conn 必須為 autocommit 模式且專供此工作使用。
    回傳最後狀態。已是 SWAPPED 以後的工作不重做（冪等）。"""
    s = settings or get_settings()
    hooks = hooks or {}
    if not adapter.try_claim_job(conn, load_id):
        return "BUSY"
    try:
        result = _drive(conn, adapter, load_id, s, hooks)
    except SimulatedCrash:
        raise                       # 模擬程序死亡：不釋放擁有權（真實 crash 由連線中斷釋放）
    except BaseException:
        if not conn.closed:
            adapter.release_job(conn, load_id)
        raise
    adapter.release_job(conn, load_id)
    return result


def _drive(conn, adapter, load_id, s, hooks) -> str:
    while True:
        ld = get_load(conn, load_id)
        st = ld["status"]
        if st in TERMINAL or st == "PREVIEWED" or (st == "BUILT" and (ld["error"] or "").startswith(SWAP_WAIT)):
            return st
        ds = catalog_repo.load_dataset(conn, ld["dataset_id"])
        try:
            if st == "UPLOADED":
                _stage(conn, adapter, ds, ld, s)
            elif st == "STAGED":
                _validate(conn, adapter, ds, ld, s)
            elif st == "VALIDATED":
                {"replace": _build, "upsert": _apply_upsert, "delete": _preview}[ld["mode"]](conn, adapter, ds, ld, s)
            elif st == "BUILT":
                _swap(conn, adapter, ds, ld, s)
            elif st in ("SWAPPED", "APPLIED"):
                _cleanup(conn, adapter, ds, ld, s, final="CLEANED")
            else:
                raise LoadError(f"未知狀態 {st}")
            new = get_load(conn, load_id)["status"]
            if (h := hooks.get(new)) is not None:
                h()
        except SimulatedCrash:
            raise
        except Exception as e:  # noqa: BLE001 - 任何錯誤都要讓工作進入 FAILED 並清掉中間產物
            log.exception("load %s failed at %s", load_id, st)
            fail(conn, adapter, load_id, f"{type(e).__name__}: {e}", settings=s)
            return "FAILED"


def fail(conn, adapter, load_id: str, message: str, *, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        conn.rollback()
    ld = get_load(conn, load_id)
    if ld["status"] in ("SWAPPED", "APPLIED", "CLEANED"):
        # 已提交的工作不能改成 FAILED；只記錄清理錯誤
        _set(conn, load_id, cleanup_status="ERROR", error=message[:4000])
        return
    _set(conn, load_id, status="FAILED", error=message[:4000])
    ds = catalog_repo.load_dataset(conn, ld["dataset_id"])
    _drop_intermediates(conn, adapter, ds, load_id)
    upload.remove_file(s, ld["upload_id"])
    _set(conn, load_id, cleanup_status="DONE", cleaned_at=_now())


def _drop_intermediates(conn, adapter, ds, load_id):
    n = names(ds, load_id)
    adapter.drop_table(conn, n["stg"])
    adapter.drop_table(conn, n["newp"])


# --------------------------------------------------------------------------- 各狀態
def _stage(conn, adapter, ds, ld, s):
    path = upload.file_path(s, ld["upload_id"])
    if not path.exists():
        raise LoadError("上傳檔不存在")
    if csvfile.sha256_file(path) != ld["file_sha256"]:   # COPY 前重驗雜湊（TOCTOU）
        raise LoadError("上傳檔 SHA-256 與登記值不符")
    # 先記一次嘗試：COPY 中途程序死亡也會計入，recover 達上限即 FAILED（避免無限重試）
    _set(conn, ld["load_id"], attempts=ld["attempts"] + 1)
    lim = dict(max_ratio=s.max_gzip_ratio, max_bytes=s.max_upload_bytes)
    hm = csvfile.map_header(ds, csvfile.read_header(path, **lim), require_required=ld["mode"] != "delete")
    if ld["mode"] == "delete":
        pk = [c.code for c in ds.pk_columns]
        if sorted(hm.columns) != sorted(pk):
            raise LoadError(f"刪除清單只能包含主鍵欄位 {pk}")
    stg = names(ds, ld["load_id"])["stg"]
    adapter.create_staging(conn, stg, hm.columns)
    try:
        rows = adapter.bulk_load(conn, stg, hm.columns, csvfile.iter_copy_chunks(path, block=s.copy_block, **lim),
                                 header_match=hm.header_match)
    except psycopg.Error as e:
        diag = e.diag
        where = csvfile.locate_error(path, **lim)
        raise LoadError(f"COPY 失敗：{diag.message_primary}；{diag.context or ''}；診斷：{where}") from None
    adapter.analyze(conn, stg)          # staging 從未有統計，後續驗證／增修／刪除的計畫才合理
    _set(conn, ld["load_id"], status="STAGED", rows_csv=rows, rows_staged=rows, staged_at=_now(),
         columns=json.dumps(hm.columns), header_match=hm.header_match,
         schema_version=ds.schema_version)


def _columns(ld) -> list[str]:
    return json.loads(ld["columns"])


def _validate(conn, adapter, ds, ld, s):
    stg = names(ds, ld["load_id"])["stg"]
    cols = _columns(ld)
    if ds.schema_version != ld["schema_version"]:
        raise SchemaChanged("匯入期間欄位定義已變更，請重新上傳")
    with conn.transaction():
        adapter.set_read_only(conn)
        req = ld["mode"] != "delete"          # 刪除清單只含主鍵，不檢查其他必填欄
        res = adapter.validate_staging(conn, ds, stg, cols, ld["yyyymm"], require_required=req)
        if not res.ok:
            report = ""
            if res.error_rows:
                s.error_report_dir.mkdir(parents=True, exist_ok=True)
                rp = s.error_report_dir / f"{ld['load_id']}.csv"
                tmp = rp.with_suffix(".tmp")
                with open(tmp, "wb") as f:      # 寫暫存檔 + rename，下載端不會讀到寫一半的報表
                    adapter.write_error_report(conn, ds, stg, cols, ld["yyyymm"], f, limit=10_000,
                                               require_required=req)
                os.replace(tmp, rp)
                report = f"；錯誤列報表 {rp.name}"
            msg = "；".join(res.messages + ([f"{res.error_rows} 列格式錯誤"] if res.error_rows else []))
            raise LoadError(f"驗證失敗，整檔拒絕：{msg}{report}")
        cks = adapter.checksums(conn, ds, stg, cols, from_text=True)
    _set(conn, ld["load_id"], status="VALIDATED", rows_rejected=0, checksums=json.dumps(cks),
         validated_at=_now())


def _build(conn, adapter, ds, ld, s):
    n = names(ds, ld["load_id"])
    cols = _columns(ld)
    rows = adapter.build_partition(conn, ds, n["stg"], cols, ld["yyyymm"], n["newp"], n["chk"])
    if rows != ld["rows_staged"]:
        raise LoadError(f"新分割列數 {rows} ≠ staging {ld['rows_staged']}")
    cks = adapter.checksums(conn, ds, n["newp"], cols, from_text=False)
    if cks != json.loads(ld["checksums"]):
        raise LoadError(f"驗算欄位 SUM 不符：staging={ld['checksums']} new={cks}")
    _set(conn, ld["load_id"], status="BUILT", built_at=_now())


def _locked(conn, adapter, ds, ld, s, fn):
    """在單一交易內依固定順序取鎖後執行 fn；取鎖逾時以指數退避重試，總時間上限 swap_retry_total_seconds。"""
    deadline = time.monotonic() + s.swap_retry_total_seconds
    delay = 0.5
    while True:
        try:
            with conn.transaction():
                adapter.set_lock_timeout(conn, s.swap_lock_timeout)
                adapter.xact_lock_dataset(conn, ds.dataset_id, exclusive=False)
                adapter.xact_lock_period(conn, ds.dataset_id, ld["yyyymm"])
                cur = execute(conn, select(load_log.c.status).where(load_log.c.load_id == ld["load_id"])
                              .with_for_update()).fetchall()[0][0]
                if cur != ld["status"]:
                    return False            # 已被其他執行者推進（冪等）
                fresh = catalog_repo.load_dataset(conn, ds.dataset_id)
                if fresh.schema_version != ld["schema_version"]:
                    raise SchemaChanged("BUILT 之後欄位定義已變更（schema_version 不符），請重新上傳")
                fn(fresh)
                return True
        except (LockBusy, errors.LockNotAvailable):
            if time.monotonic() + delay > deadline:
                raise LockBusy("取鎖逾時") from None
            time.sleep(delay)
            delay = min(delay * 2, 15)


def _swap(conn, adapter, ds, ld, s):
    n = names(ds, ld["load_id"])
    bak = f"{ds.name}_bak_{ld['yyyymm']}_{n['key']}"

    def do(fresh):
        had_old = adapter.swap_period(conn, fresh, ld["yyyymm"], n["newp"], bak, n["chk"])
        _set(conn, ld["load_id"], status="SWAPPED", swapped_at=_now(), error=None,
             bak_table=bak if had_old else None, rows_inserted=ld["rows_staged"])
    try:
        _locked(conn, adapter, ds, ld, s, do)
    except LockBusy as e:
        _set(conn, ld["load_id"], error=f"{SWAP_WAIT}：該資料集有長查詢或備份佔用，請稍後重試或取消（{e}）")


def _apply_upsert(conn, adapter, ds, ld, s):
    stg = names(ds, ld["load_id"])["stg"]

    def do(fresh):
        ins, upd = adapter.upsert(conn, fresh, stg, _columns(ld), ld["yyyymm"])
        _set(conn, ld["load_id"], status="APPLIED", rows_inserted=ins, rows_updated=upd, swapped_at=_now())
    try:
        _locked(conn, adapter, ds, ld, s, do)
    except LockBusy as e:
        raise LoadError(f"取鎖逾時：{e}") from None


def _preview(conn, adapter, ds, ld, s):
    stg = names(ds, ld["load_id"])["stg"]
    with conn.transaction():
        adapter.set_read_only(conn)
        matched, unmatched, missing = adapter.delete_preview(conn, ds, stg, sample=1000, yyyymm=ld["yyyymm"])
        total = adapter.row_count(conn, partition_name(ds, ld["yyyymm"]))
    detail = {"period_rows": total, "missing_sample": [[str(v) for v in r] for r in missing],
              "pk": [c.code for c in ds.pk_columns]}
    _set(conn, ld["load_id"], status="PREVIEWED", rows_matched=matched, rows_unmatched=unmatched,
         detail=json.dumps(detail, ensure_ascii=False))


def confirm_delete(conn, adapter, load_id: str, *, operator: str, confirm_large: bool = False,
                   settings: Settings | None = None) -> dict:
    """SPEC 8.2 步驟 3～5：填操作人 + 確認；超過該期 10% 需再確認；實際刪除數 ≠ 預覽數即回滾。"""
    s = settings or get_settings()
    if not operator.strip():
        raise LoadError("請填寫操作人姓名")
    if not adapter.try_claim_job(conn, load_id):
        raise LoadError("工作執行中")
    try:
        ld = get_load(conn, load_id)
        if ld["status"] != "PREVIEWED":
            raise LoadError(f"狀態為 {ld['status']}，不可確認刪除")
        ds = catalog_repo.load_dataset(conn, ld["dataset_id"])
        stg = names(ds, load_id)["stg"]
        if not adapter.table_exists(conn, stg):
            fail(conn, adapter, load_id, "刪除清單暫存已遺失（資料庫重啟），請重新上傳並預覽", settings=s)
            raise LoadError("刪除清單暫存已遺失，請重新上傳")
        period_rows = json.loads(ld["detail"])["period_rows"]
        if ld["rows_matched"] > period_rows * 0.10 and not confirm_large:
            raise LoadError("刪除筆數超過該期 10%，需再確認一次（建議改用整期重傳）")
        ld["status"] = "PREVIEWED"

        def do(fresh):
            n = adapter.delete_by_keys(conn, fresh, stg, load_id, operator.strip()[:200], ld["yyyymm"])
            if n != ld["rows_matched"]:
                raise LoadError(f"實際刪除 {n} 筆 ≠ 預覽 {ld['rows_matched']} 筆，已回滾")
            _set(conn, load_id, status="APPLIED", rows_deleted=n, operator_note=operator.strip()[:200],
                 swapped_at=_now())
        try:
            _locked(conn, adapter, ds, ld, s, do)
        except LockBusy:
            raise                           # 保留 PREVIEWED，API 回 409，操作人稍後重按確認
        except (LoadError, SchemaChanged) as e:
            fail(conn, adapter, load_id, str(e), settings=s)
            raise LoadError(str(e)) from None
        _cleanup(conn, adapter, ds, get_load(conn, load_id), s, final="CLEANED")
        return get_load(conn, load_id)
    finally:
        adapter.release_job(conn, load_id)


def retry_swap(conn, load_id: str) -> None:
    """操作人選擇稍後重試：清除等待標記，交由 run_load 再跑一次。"""
    ld = get_load(conn, load_id)
    if ld["status"] != "BUILT":
        raise LoadError("只有 BUILT 狀態可重試切換")
    _set(conn, load_id, error=None)


def cancel(conn, adapter, load_id: str, *, settings: Settings | None = None) -> None:
    if not adapter.try_claim_job(conn, load_id):
        raise LoadError("工作執行中，無法取消")
    try:
        ld = get_load(conn, load_id)
        if ld["status"] not in ACTIVE_STATES:
            raise LoadError(f"狀態為 {ld['status']}，不可取消")
        fail(conn, adapter, load_id, "操作人取消", settings=settings)
        _set(conn, load_id, status="CANCELLED")
    finally:
        adapter.release_job(conn, load_id)


def _cleanup(conn, adapter, ds, ld, s, *, final):
    try:
        adapter.drop_table(conn, names(ds, ld["load_id"])["stg"])
        upload.remove_file(s, ld["upload_id"])
        _set(conn, ld["load_id"], status=final, cleanup_status="DONE", cleaned_at=_now())
    except Exception as e:  # noqa: BLE001
        _set(conn, ld["load_id"], cleanup_status="ERROR", error=f"清理失敗：{e}")
        raise


# --------------------------------------------------------------------------- 整期刪除
def drop_period(conn, adapter, *, dataset: str, yyyymm: int, operator: str, client_ip: str = "",
                settings: Settings | None = None) -> str:
    """SPEC 8.3：DETACH 並改名 _bak_…，保留期間內可重新 ATTACH。"""
    s = settings or get_settings()
    if not operator.strip():
        raise LoadError("請填寫操作人姓名")
    ds = catalog_repo.load_dataset(conn, dataset)
    load_id = str(uuid.uuid4())
    n = names(ds, load_id)
    bak = f"{ds.name}_bak_{yyyymm}_{n['key']}"
    try:
        with conn.transaction():
            execute(conn, insert(load_log).values(
                load_id=load_id, dataset_id=ds.dataset_id, yyyymm=yyyymm, mode="drop", status="UPLOADED",
                schema_version=ds.schema_version, operator_note=operator[:200], client_ip=client_ip[:45]))
    except errors.UniqueViolation:
        raise LoadError(f"期別 {yyyymm} 有進行中的工作") from None
    ld = get_load(conn, load_id)

    def do(fresh):
        adapter.drop_period(conn, fresh, yyyymm, bak)
        _set(conn, load_id, status="CLEANED", bak_table=bak, swapped_at=_now(), cleanup_status="DONE")
    try:
        _locked(conn, adapter, ds, ld, s, do)
    except Exception as e:
        _set(conn, load_id, status="FAILED", error=str(e)[:4000])
        raise
    return load_id


# --------------------------------------------------------------------------- 恢復與排程清理
def recover(conn, adapter, *, settings: Settings | None = None) -> list[str]:
    """啟動／定期執行：接手沒人持有的進行中工作。
    - BUILT 且新分割還在：直接重試切換（新分割是一般表，crash 後仍在）
    - 其他 < SWAPPED：清掉中間產物，從 UPLOADED 重跑（UNLOGGED staging 在 crash 後會被清空）
    - SWAPPED / APPLIED 尚未清理：重跑清理
    - 嘗試次數達上限：FAILED
    回傳需要重新執行的 load_id。"""
    s = settings or get_settings()
    rows = execute(conn, select(load_log.c.load_id, load_log.c.mode, load_log.c.created_at).where(
        load_log.c.status.in_(("UPLOADED", "STAGED", "VALIDATED", "BUILT", "SWAPPED", "APPLIED")))).fetchall()
    todo = []
    for load_id, mode, created_at in rows:
        if mode == "drop":
            # 整期刪除在單一交易內完成；仍停在 UPLOADED 代表交易已回滾，標 FAILED 釋放期別
            if created_at < _now() - timedelta(minutes=10):
                _set(conn, load_id, status="FAILED", error="整期刪除未完成（交易已回滾）")
            continue
        if not adapter.try_claim_job(conn, load_id):
            continue                       # 其他 worker 正在執行
        try:
            ld = get_load(conn, load_id)
            ds = catalog_repo.load_dataset(conn, ld["dataset_id"])
            st = ld["status"]
            if st in ("SWAPPED", "APPLIED"):
                todo.append(load_id)
            elif st == "BUILT" and adapter.table_exists(conn, names(ds, load_id)["newp"]):
                if not (ld["error"] or "").startswith(SWAP_WAIT):
                    todo.append(load_id)
            elif ld["attempts"] >= MAX_ATTEMPTS:
                fail(conn, adapter, load_id, f"重試 {MAX_ATTEMPTS} 次仍未完成", settings=s)
            else:
                _drop_intermediates(conn, adapter, ds, load_id)
                _set(conn, load_id, status="UPLOADED")
                todo.append(load_id)
        finally:
            adapter.release_job(conn, load_id)
    return todo


def purge_backups(conn, adapter, *, settings: Settings | None = None) -> list[str]:
    """_bak 保留 7 天、同期最多 2 份（SPEC 7.4 CLEANED）。"""
    s = settings or get_settings()
    rows = execute(conn, select(load_log.c.load_id, load_log.c.dataset_id, load_log.c.yyyymm,
                                load_log.c.bak_table, load_log.c.swapped_at)
                   .where(load_log.c.bak_table.is_not(None))
                   .order_by(load_log.c.dataset_id, load_log.c.yyyymm, load_log.c.swapped_at.desc())).fetchall()
    cutoff = _now() - timedelta(days=s.bak_keep_days)
    seen: dict[tuple, int] = {}
    dropped = []
    for load_id, ds_id, p, bak, at in rows:
        if not adapter.table_exists(conn, bak):
            continue
        k = (ds_id, p)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > s.bak_keep_per_period or (at and at < cutoff):
            adapter.drop_table(conn, bak)
            dropped.append(bak)
    return dropped


def purge_deleted(conn, adapter, *, settings: Settings | None = None) -> int:
    s = settings or get_settings()
    total = 0
    for ds in catalog_repo.load_all(conn).values():
        total += adapter.purge_deleted(conn, ds, s.deleted_keep_days)
    return total


__all__ = ["create_load", "run_load", "confirm_delete", "cancel", "retry_swap", "drop_period",
           "recover", "purge_backups", "purge_deleted", "LoadError", "SimulatedCrash", "AdapterError"]

"""FastAPI 入口。寫入類路徑由 Caddy IP allowlist 保護（SPEC 11）；查詢一律用唯讀角色。"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import insert, select

from app.catalog import CatalogError, ColumnDef
from app.config import get_settings
from app.db import catalog_repo
from app.db.adapters.base import AdapterError, LockBusy
from app.db.adapters.postgres import get_adapter
from app.db.meta import load_log, saved_query
from app.db.pool import close_all, get_pool
from app.db.sqlexec import execute
from app.services import importer, jobs, upload
from app.services.csvfile import CsvRejected
from app.services.export import CopyLimitError, ExportEncodingError
from app.services.query import runner
from app.services.query.expr import ExprError
from app.services.query.spec import QuerySpecError

log = logging.getLogger(__name__)
adapter = get_adapter()
SID = "cqp_sid"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    with get_pool("system").connection() as conn:
        for ds in catalog_repo.load_all(conn).values():
            adapter.check_primary_key(conn, ds)      # 不一致即啟動失敗（SPEC 5.2）
    jobs.start()
    yield
    jobs.stop()
    close_all()


app = FastAPI(title="CSV 查詢平台", lifespan=lifespan)


@app.middleware("http")
async def session_cookie(request: Request, call_next):
    sid = request.cookies.get(SID)
    try:
        uuid.UUID(sid or "")
    except ValueError:
        sid = None
    request.state.sid = sid or str(uuid.uuid4())
    resp = await call_next(request)
    if not sid:
        resp.set_cookie(SID, request.state.sid, httponly=True, samesite="strict", secure=True)
    return resp


def client_ip(request: Request) -> str:
    # Caddy 以 header_up 覆寫 X-Forwarded-For（不信任客戶端帶入的值），app port 不對外
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()


@app.exception_handler(CatalogError)
@app.exception_handler(QuerySpecError)
@app.exception_handler(ExprError)
@app.exception_handler(CsvRejected)
@app.exception_handler(upload.UploadError)
@app.exception_handler(importer.LoadError)
@app.exception_handler(AdapterError)
@app.exception_handler(CopyLimitError)
@app.exception_handler(ExportEncodingError)
async def bad_request(_req, exc):
    return JSONResponse({"error": str(exc), "type": type(exc).__name__}, status_code=400)


@app.exception_handler(LockBusy)
async def lock_busy(_req, exc):
    return JSONResponse({"error": f"資料集正被其他寫入或備份佔用，請稍後再試（{exc}）"}, status_code=409)


@app.exception_handler(runner.Busy)
async def busy(_req, exc):
    return JSONResponse({"error": str(exc)}, status_code=429)


def _sys():
    return get_pool("system").connection()


def _ser(d: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}


# ------------------------------------------------------------------ 資料集與欄位
@app.get("/api/health")
def health():
    with _sys() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/datasets")
def list_datasets():
    with _sys() as c:
        return [{"name": d.name, "schema_version": d.schema_version, "description": d.description,
                 "columns": len(d.active_columns)} for d in catalog_repo.load_all(c).values()]


def _ds_json(ds):
    return {"dataset_id": ds.dataset_id, "name": ds.name, "partition_key": ds.partition_key,
            "schema_version": ds.schema_version, "description": ds.description,
            "columns": [c.__dict__ | {"aliases": list(c.aliases)} for c in ds.columns]}


@app.get("/api/datasets/{name}")
def get_dataset(name: str):
    with _sys() as c:
        return _ds_json(catalog_repo.load_dataset(c, name))


@app.post("/api/datasets")
def create_dataset(request: Request, body: dict = Body(...), operator_note: str = ""):
    ds = catalog_repo.dataset_from_json(body)
    with _sys() as c:
        return _ds_json(catalog_repo.create_dataset(c, adapter, ds, note=operator_note, ip=client_ip(request)))


class NewColumn(BaseModel):
    code: str
    data_type: str
    display_name: str = ""
    aliases: list[str] = []
    required: bool = False
    checksum: bool = False
    group_name: str = ""
    sort_order: int = 0
    description: str = ""
    operator_note: str = ""


@app.post("/api/datasets/{name}/columns")
def add_column(name: str, col: NewColumn, request: Request):
    cd = ColumnDef(code=col.code, data_type=col.data_type, display_name=col.display_name,
                   aliases=tuple(col.aliases), required=col.required, checksum=col.checksum,
                   group_name=col.group_name, sort_order=col.sort_order, description=col.description)
    with _sys() as c:
        return _ds_json(catalog_repo.add_column(c, adapter, name, cd, note=col.operator_note, ip=client_ip(request)))


@app.post("/api/datasets/{name}/columns/{code}/disable")
def disable_column(name: str, code: str, request: Request, operator_note: str = ""):
    with _sys() as c:
        return _ds_json(catalog_repo.disable_column(c, adapter, name, code, note=operator_note, ip=client_ip(request)))


# ------------------------------------------------------------------ 上傳
class NewUpload(BaseModel):
    filename: str
    total_bytes: int
    chunk_sha256: list[str]


@app.post("/api/upload")
def upload_create(u: NewUpload):
    with _sys() as c:
        return upload.create(c, get_settings(), filename=u.filename, total_bytes=u.total_bytes,
                             chunk_sha256=u.chunk_sha256)


@app.put("/api/upload/{upload_id}/{seq}")
async def upload_chunk(upload_id: str, seq: int, request: Request):
    s = get_settings()
    data = await request.body()
    if len(data) > s.chunk_size:
        raise HTTPException(413, "區塊過大")
    from starlette.concurrency import run_in_threadpool

    def put():
        with _sys() as c:
            return upload.put_chunk(c, s, upload_id, seq, data)
    return await run_in_threadpool(put)


@app.post("/api/upload/{upload_id}/complete")
def upload_complete(upload_id: str):
    with _sys() as c:
        return upload.complete(c, get_settings(), upload_id)


# ------------------------------------------------------------------ 匯入／增修／刪除
class NewLoad(BaseModel):
    dataset: str
    yyyymm: int
    mode: str = Field(pattern="^(replace|upsert|delete)$")
    upload_id: str
    operator_note: str = ""


@app.post("/api/loads")
def create_load(body: NewLoad, request: Request):
    with _sys() as c:
        lid = importer.create_load(c, adapter, dataset=body.dataset, yyyymm=body.yyyymm, mode=body.mode,
                                   upload_id=body.upload_id, operator_note=body.operator_note,
                                   client_ip=client_ip(request))
    jobs.submit(lid)
    return {"load_id": lid}


@app.get("/api/loads/{load_id}")
def get_load(load_id: str):
    with _sys() as c:
        return _ser(importer.get_load(c, load_id))


@app.get("/api/loads")
def list_loads(dataset: str | None = None, limit: int = 50):
    with _sys() as c:
        q = select(load_log).order_by(load_log.c.created_at.desc()).limit(min(limit, 500))
        if dataset:
            q = q.where(load_log.c.dataset_id == catalog_repo.load_dataset(c, dataset).dataset_id)
        return [_ser(dict(r._mapping)) for r in execute(c, q).fetchall()]


@app.get("/api/loads/{load_id}/errors")
def load_errors(load_id: str):
    uuid.UUID(load_id)
    p = get_settings().error_report_dir / f"{load_id}.csv"
    if not p.exists():
        raise HTTPException(404, "沒有錯誤報表")
    return FileResponse(p, media_type="text/csv", filename=f"errors_{load_id}.csv")


@app.post("/api/loads/{load_id}/retry")
def retry(load_id: str):
    with _sys() as c:
        importer.retry_swap(c, load_id)
    jobs.submit(load_id)
    return {"ok": True}


@app.post("/api/loads/{load_id}/cancel")
def cancel_load(load_id: str):
    with _sys() as c:
        importer.cancel(c, adapter, load_id)
    return {"ok": True}


class ConfirmDelete(BaseModel):
    operator: str
    confirm_large: bool = False


@app.post("/api/loads/{load_id}/confirm-delete")
def confirm_delete(load_id: str, body: ConfirmDelete):
    with get_pool("import").connection() as c:
        return _ser(importer.confirm_delete(c, adapter, load_id, operator=body.operator,
                                            confirm_large=body.confirm_large))


class DropPeriod(BaseModel):
    dataset: str
    yyyymm: int
    operator: str


@app.post("/api/delete-period")
def drop_period(body: DropPeriod, request: Request):
    with get_pool("import").connection() as c:
        return {"load_id": importer.drop_period(c, adapter, dataset=body.dataset, yyyymm=body.yyyymm,
                                                operator=body.operator, client_ip=client_ip(request))}


# ------------------------------------------------------------------ 查詢
class QueryReq(BaseModel):
    spec: dict[str, Any]
    offset: int = 0
    limit: int = 1000
    allow_many_to_many: bool = False
    job_id: str | None = None


def _catalog():
    with _sys() as c:
        return catalog_repo.load_all(c)


@app.post("/api/query/preview")
def query_preview(q: QueryReq, request: Request):
    if q.job_id:
        uuid.UUID(q.job_id)
    return runner.preview(q.spec, _catalog(), adapter, session_id=request.state.sid, client_ip=client_ip(request),
                          offset=q.offset, limit=min(max(q.limit, 1), 1000), allow_many=q.allow_many_to_many,
                          job_id=q.job_id)


@app.post("/api/query/count")
def query_count(q: QueryReq, request: Request):
    return {"count": runner.count(q.spec, _catalog(), adapter, session_id=request.state.sid,
                                  client_ip=client_ip(request), allow_many=q.allow_many_to_many)}


@app.post("/api/query/copy")
def query_copy(q: QueryReq, request: Request):
    tsv = runner.copy_tsv(q.spec, _catalog(), adapter, session_id=request.state.sid,
                          client_ip=client_ip(request), allow_many=q.allow_many_to_many)
    return PlainTextResponse(tsv, media_type="text/tab-separated-values; charset=utf-8")


class ExportReq(QueryReq):
    encoding: str = Field(default="utf-8-sig", pattern="^(utf-8-sig|big5)$")
    gzip: bool = False


@app.post("/api/query/export")
def query_export(q: ExportReq, request: Request):
    gen = runner.export(q.spec, _catalog(), adapter, session_id=request.state.sid, client_ip=client_ip(request),
                        encoding=q.encoding, gzip=q.gzip, allow_many=q.allow_many_to_many)
    name = "export.csv.gz" if q.gzip else "export.csv"
    return StreamingResponse(gen, media_type="application/gzip" if q.gzip else "text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/query/{job_id}/cancel")
def query_cancel(job_id: str):
    return {"cancelled": runner.cancel(adapter, job_id)}


# ------------------------------------------------------------------ 我的查詢
class SavedQuery(BaseModel):
    name: str = Field(max_length=200)
    spec: dict[str, Any]
    note: str = ""


@app.post("/api/saved-queries")
def save_query(q: SavedQuery):
    cat = _catalog()
    tables = [q.spec.get("from")] + [j.get("table") for j in q.spec.get("joins", [])]
    versions = {t: cat[t].schema_version for t in tables if t in cat}
    with _sys() as c:
        qid = execute(c, insert(saved_query).values(name=q.name, spec=json.dumps(q.spec, ensure_ascii=False),
                                                    schema_versions=json.dumps(versions), note=q.note[:200])
                      .returning(saved_query.c.query_id)).fetchall()[0][0]
    return {"query_id": qid}


@app.get("/api/saved-queries/{query_id}")
def load_query(query_id: int):
    with _sys() as c:
        rows = execute(c, select(saved_query).where(saved_query.c.query_id == query_id)).fetchall()
        if not rows:
            raise HTTPException(404)
        r = dict(rows[0]._mapping)
        cat = catalog_repo.load_all(c)
    spec = json.loads(r["spec"])
    stale = []
    for ref in spec.get("select", []):
        t, _, col = str(ref).partition(".")
        ds = cat.get(t)
        if ds is None or not any(c.code == col and not c.disabled for c in ds.columns):
            stale.append(ref)
    return {"query_id": query_id, "name": r["name"], "spec": spec, "stale_columns": stale,
            "schema_versions": json.loads(r["schema_versions"])}


@app.get("/")
def root():
    return Response("CSV 查詢平台 API（前端尚未實作，見 /docs）", media_type="text/plain; charset=utf-8")

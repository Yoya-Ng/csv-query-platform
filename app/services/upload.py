"""分塊上傳（SPEC 7.1）：upload_id + 區塊序號 + 每塊 SHA-256，可續傳；完整後組合、設唯讀、記錄 SHA-256。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import insert, select, update

from app.config import Settings
from app.db.meta import upload_session
from app.db.sqlexec import execute

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_UID_RE = re.compile(r"[0-9a-f-]{36}")
# 組檔時區塊與完整檔同時存在，峰值約 2 倍檔案大小
ASSEMBLE_PEAK_FACTOR = 2.1


class UploadError(ValueError):
    pass


def _dir(s: Settings, upload_id: str) -> Path:
    if not _UID_RE.fullmatch(upload_id):
        raise UploadError("upload_id 格式錯誤")
    return s.upload_dir / upload_id


def file_path(s: Settings, upload_id: str) -> Path:
    return _dir(s, upload_id) / "data"


def create(conn, s: Settings, *, filename: str, total_bytes: int, chunk_sha256: list[str]) -> dict:
    name = Path(filename).name
    if not (name.lower().endswith(".csv") or name.lower().endswith(".csv.gz")):
        raise UploadError("只接受 .csv 或 .csv.gz")
    if not 0 < total_bytes <= s.max_upload_bytes:
        raise UploadError(f"檔案大小需介於 1 byte 與 {s.max_upload_bytes} bytes")
    n = -(-total_bytes // s.chunk_size)
    if len(chunk_sha256) != n or not all(_SHA_RE.fullmatch(h) for h in chunk_sha256):
        raise UploadError(f"需提供 {n} 個區塊的 SHA-256（小寫 hex）")
    if shutil.disk_usage(s.upload_dir).free < total_bytes * ASSEMBLE_PEAK_FACTOR:
        raise UploadError("上傳暫存空間不足")
    uid = str(uuid.uuid4())
    (_dir(s, uid) / "chunks").mkdir(parents=True)
    with conn.transaction():
        execute(conn, insert(upload_session).values(
            upload_id=uid, filename=name, total_chunks=n, total_bytes=total_bytes,
            chunk_sha256=",".join(chunk_sha256)))
    return {"upload_id": uid, "total_chunks": n, "chunk_size": s.chunk_size}


def _get(conn, upload_id: str):
    rows = execute(conn, select(upload_session).where(upload_session.c.upload_id == upload_id)).fetchall()
    if not rows:
        raise UploadError("upload_id 不存在")
    return rows[0]._mapping


def put_chunk(conn, s: Settings, upload_id: str, seq: int, data: bytes) -> dict:
    sess = _get(conn, upload_id)
    if sess["status"] != "OPEN":
        raise UploadError(f"上傳狀態為 {sess['status']}，不可再傳")
    n = sess["total_chunks"]
    if not 0 <= seq < n:
        raise UploadError("區塊序號超出範圍")
    expected_len = s.chunk_size if seq < n - 1 else sess["total_bytes"] - s.chunk_size * (n - 1)
    if len(data) != expected_len:
        raise UploadError(f"區塊 {seq} 大小 {len(data)} ≠ 預期 {expected_len}")
    if hashlib.sha256(data).hexdigest() != sess["chunk_sha256"].split(",")[seq]:
        raise UploadError(f"區塊 {seq} SHA-256 不符，請重傳")
    p = _dir(s, upload_id) / "chunks" / f"{seq:06d}"
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")   # 同區塊並發重送時各自寫自己的暫存檔
    tmp.write_bytes(data)
    os.replace(tmp, p)       # 寫暫存檔 + rename
    return {"received": seq, "missing": missing(s, upload_id, n)}


def missing(s: Settings, upload_id: str, n: int) -> list[int]:
    d = _dir(s, upload_id) / "chunks"
    have = {int(p.name) for p in d.iterdir() if p.name.isdigit()}
    return [i for i in range(n) if i not in have]


def status(conn, s: Settings, upload_id: str) -> dict:
    """續傳用：回傳狀態與尚缺的區塊序號（只有 OPEN 才有意義）。"""
    _dir(s, upload_id)                      # 先驗證格式，再查資料庫
    sess = _get(conn, upload_id)
    miss = missing(s, upload_id, sess["total_chunks"]) if sess["status"] == "OPEN" else []
    return {"upload_id": upload_id, "status": sess["status"], "missing": miss,
            "total_chunks": sess["total_chunks"], "total_bytes": sess["total_bytes"],
            "chunk_sha256": [h for h in sess["chunk_sha256"].split(",") if h]}


def complete(conn, s: Settings, upload_id: str) -> dict:
    sess = _get(conn, upload_id)
    if sess["status"] == "COMPLETE":
        return {"upload_id": upload_id, "sha256": sess["file_sha256"]}
    n = sess["total_chunks"]
    if m := missing(s, upload_id, n):
        raise UploadError(f"尚缺區塊：{m[:20]}")
    d = _dir(s, upload_id)
    out, tmp = file_path(s, upload_id), d / "data.tmp"
    tmp.unlink(missing_ok=True)       # 上次在 chmod 後、rename 前失敗會留下唯讀暫存檔
    h = hashlib.sha256()
    with open(tmp, "wb") as f:
        for i in range(n):
            b = (d / "chunks" / f"{i:06d}").read_bytes()
            if hashlib.sha256(b).hexdigest() != sess["chunk_sha256"].split(",")[i]:
                raise UploadError(f"區塊 {i} 檔案已損毀，請重傳")
            h.update(b)
            f.write(b)
    os.chmod(tmp, 0o444)
    os.replace(tmp, out)
    shutil.rmtree(d / "chunks")
    digest = h.hexdigest()
    with conn.transaction():
        execute(conn, update(upload_session).where(upload_session.c.upload_id == upload_id)
                .values(status="COMPLETE", file_sha256=digest))
    return {"upload_id": upload_id, "sha256": digest, "filename": sess["filename"]}


def register_local_file(conn, s: Settings, src: Path) -> dict:
    """測試與批次用：把本機檔案登記成已完成的上傳（複製進上傳目錄，流程與網頁上傳相同）。"""
    uid = str(uuid.uuid4())
    d = _dir(s, uid)
    d.mkdir(parents=True)
    tmp = d / "data.tmp"
    shutil.copyfile(src, tmp)
    from app.services.csvfile import sha256_file
    digest = sha256_file(tmp)
    os.chmod(tmp, 0o444)
    os.replace(tmp, file_path(s, uid))
    size = file_path(s, uid).stat().st_size
    with conn.transaction():
        execute(conn, insert(upload_session).values(
            upload_id=uid, filename=src.name, total_chunks=1, total_bytes=size,
            file_sha256=digest, status="COMPLETE"))
    return {"upload_id": uid, "sha256": digest, "filename": src.name}


def expire_stale(conn, s: Settings, hours: int = 24) -> int:
    """未完成上傳 24 小時後清除。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = execute(conn, select(upload_session.c.upload_id).where(
        upload_session.c.status == "OPEN", upload_session.c.created_at < cutoff)).fetchall()
    for (uid,) in rows:
        shutil.rmtree(_dir(s, uid), ignore_errors=True)
        with conn.transaction():
            execute(conn, update(upload_session).where(upload_session.c.upload_id == uid)
                    .values(status="EXPIRED"))
    return len(rows)


def remove_file(s: Settings, upload_id: str) -> None:
    shutil.rmtree(_dir(s, upload_id), ignore_errors=True)

"""CSV 檔案讀取：表頭解析與對應、串流區塊（BOM、NUL、gzip 解壓比）、失敗時的錯誤定位（SPEC 7.2、7.3、7.6）。"""
from __future__ import annotations

import csv
import hashlib
import io
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.catalog import DatasetDef

BOM = b"\xef\xbb\xbf"
GZIP_MAGIC = b"\x1f\x8b"
GZIP_RATIO_GRACE = 16 << 20


class CsvRejected(ValueError):
    pass


def sha256_file(path: Path, block: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(block):
            h.update(b)
    return h.hexdigest()


def is_gzip(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == GZIP_MAGIC


def iter_raw(path: Path, *, block: int, max_ratio: int, max_bytes: int) -> Iterator[bytes]:
    """依副檔名無關地讀出解壓後位元組串流。gzip 串流解壓、不落地；超過解壓比或上限即中止。"""
    if not is_gzip(path):
        with open(path, "rb") as f:
            while b := f.read(block):
                yield b
        return
    # 支援多成員 gzip（cat a.gz b.gz、pigz、bgzip）：一個成員結束（eof）後，剩餘位元組從新的
    # decompressobj 繼續；最後一個成員沒有正常結束即視為截斷，整檔拒絕（不可默默少讀資料）
    d = zlib.decompressobj(wbits=47)   # 32+15：自動辨識 gzip 標頭
    comp = out = 0
    with open(path, "rb") as f:
        while b := f.read(block):
            comp += len(b)
            data = b
            while data:
                if d.eof:
                    d = zlib.decompressobj(wbits=47)
                try:
                    chunk = d.decompress(data, block)
                except zlib.error as e:
                    raise CsvRejected(f"gzip 格式錯誤：{e}") from None
                data = d.unused_data if d.eof else d.unconsumed_tail
                out += len(chunk)
                # 解壓後 16MB 以內不判斷比例（小檔表頭或重複值的壓縮比本來就高）；
                # 寬限必須依「解壓後」大小計算，依壓縮大小計算會讓小體積的壓縮炸彈繞過
                if out > max_bytes or (out > GZIP_RATIO_GRACE and out > comp * max_ratio):
                    raise CsvRejected(f"解壓後大小超過限制（比例 > {max_ratio} 或 > {max_bytes} bytes）")
                if chunk:
                    yield chunk
        if not d.eof:
            raise CsvRejected("gzip 檔案不完整（可能上傳或壓縮時被截斷），整檔拒絕")


def iter_copy_chunks(path: Path, *, block: int, max_ratio: int, max_bytes: int) -> Iterator[bytes]:
    """送進 COPY 的串流：去除開頭 BOM；遇到 NUL byte 整檔拒絕。"""
    first = True
    for b in iter_raw(path, block=block, max_ratio=max_ratio, max_bytes=max_bytes):
        if first:
            first = False
            if b.startswith(BOM):
                b = b[3:]
        if b"\x00" in b:
            raise CsvRejected("檔案含 NUL byte（0x00），PostgreSQL 文字欄位無法儲存，整檔拒絕")
        yield b


def read_header(path: Path, *, max_ratio: int, max_bytes: int, limit: int = 1 << 20) -> list[str]:
    buf = b""
    for b in iter_raw(path, block=1 << 16, max_ratio=max_ratio, max_bytes=max_bytes):
        buf += b
        if b"\n" in buf or len(buf) > limit:
            break
    if buf.startswith(BOM):
        buf = buf[3:]
    line = buf.split(b"\n", 1)[0]
    if len(line) > limit:
        raise CsvRejected("表頭過長")
    try:
        text = line.decode("utf-8").rstrip("\r")
    except UnicodeDecodeError as e:
        raise CsvRejected(f"表頭不是 UTF-8：{e}") from None
    if text.count('"') % 2:
        raise CsvRejected("表頭引號未閉合或含換行")
    return next(csv.reader([text]))


@dataclass
class HeaderMapping:
    columns: list[str]        # 依檔案表頭順序的實體欄名
    header_match: bool        # 表頭恰為實體名 → 用 COPY HEADER MATCH


def map_header(ds: DatasetDef, header: list[str], *, require_required: bool = True) -> HeaderMapping:
    """以名稱對應 meta_column 實體名或別名（不分大小寫、去前後空白）。拒絕重複、空白、未知、缺必填。"""
    lookup: dict[str, str] = {}
    for c in ds.active_columns:
        for name in (c.code, *c.aliases):
            k = name.strip().lower()
            if k in lookup and lookup[k] != c.code:
                raise CsvRejected(f"metadata 別名衝突：{name}")
            lookup[k] = c.code
    cols, problems, seen = [], [], set()
    for i, h in enumerate(header, 1):
        k = h.strip().lower()
        if not k:
            problems.append(f"第 {i} 欄表頭空白")
            continue
        code = lookup.get(k)
        if code is None:
            problems.append(f"未知欄位「{h}」（請先到欄位維護新增）")
            continue
        if code in seen:
            problems.append(f"欄位重複「{h}」→ {code}")
            continue
        seen.add(code)
        cols.append(code)
    for c in ds.active_columns:
        if require_required and c.required and c.code not in seen and c.code != ds.partition_key:
            problems.append(f"缺少必填欄位 {c.code}")
    if problems:
        raise CsvRejected("表頭驗證失敗：" + "；".join(problems))
    return HeaderMapping(columns=cols, header_match=(cols == header))


def locate_error(path: Path, *, max_ratio: int, max_bytes: int, max_scan: int | None = None) -> str:
    """COPY 失敗後的診斷掃描（只在失敗時執行）：回報第一個結構錯誤的 CSV 記錄序號與實體行號。"""
    raw = iter_raw(path, block=1 << 20, max_ratio=max_ratio, max_bytes=max_bytes)
    stream = io.TextIOWrapper(_IterIO(raw), encoding="utf-8-sig", newline="", errors="strict")
    reader = csv.reader(stream, strict=True)
    try:
        header = next(reader)
        n = len(header)
        for rec_no, row in enumerate(reader, 1):
            if len(row) != n:
                return f"CSV 記錄 {rec_no}（實體行號 {reader.line_num}）欄位數 {len(row)} ≠ 表頭 {n}"
            if max_scan and rec_no >= max_scan:
                break
    except csv.Error as e:
        return f"CSV 記錄 {reader.line_num}附近（實體行號 {reader.line_num}）格式錯誤：{e}"
    except UnicodeDecodeError as e:
        return f"非 UTF-8 編碼：{e}"
    return "診斷掃描未發現結構錯誤（請看資料庫錯誤訊息）"


class _IterIO(io.RawIOBase):
    def __init__(self, it):
        self._it, self._buf = it, b""

    def readable(self):
        return True

    def readinto(self, b):
        while not self._buf:
            try:
                self._buf = next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n], self._buf = self._buf[:n], self._buf[n:]
        return n

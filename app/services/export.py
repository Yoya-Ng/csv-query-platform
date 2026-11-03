"""匯出與複製（SPEC 10）。"""
from __future__ import annotations

import codecs
import zlib
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import Select
from sqlalchemy.dialects import postgresql


class ExportEncodingError(ValueError):
    """匯出編碼無法表示某字元（整筆失敗並回報）。"""


class CopyLimitError(ValueError):
    """複製貼上超過筆數或大小上限。"""


# 不用 postgresql.psycopg.dialect()（SQLAlchemy 2.1 的預設 postgresql.dialect() 也是它）：它會在綁定參數後面加 ::INTEGER 之類的轉型（SPEC 13.4 禁用 ::，
# 且 COPY 是用戶端合併參數，不需要）。psycopg2 dialect 只用來編譯（不需安裝 psycopg2），paramstyle 為 pyformat（%(name)s），psycopg 3 可直接合併。
_DIALECT = postgresql.psycopg2.dialect()


def to_copy_sql(select: Select) -> tuple[str, dict[str, Any]]:
    """把 Select 編譯成 `COPY (...) TO STDOUT (FORMAT csv, HEADER)` 的 SQL 與參數。

    參數機制（已讀 psycopg 3.3.6 原始碼 `_start_copy_gen` 確認）：COPY 不支援伺服器端
    綁定（extended protocol 的 $n 參數），所以 `cursor.copy(statement, params)` 會用
    `PostgresClientQuery` 在**用戶端**把 params 以 psycopg 的型別轉換（含跳脫）合併進
    SQL，再送出。因此：SQL 用 pyformat 佔位符 `%(name)s`、params 為 dict
    （SQLAlchemy PG dialect 預設 pyformat；SQL 內字面的 % 會被編成 %%）。
    expanding IN 用 `render_postcompile=True` 展開成個別佔位符（compile 後
    `compiled.params` 已含展開後的鍵）。不使用 literal_binds。
    """
    c = select.compile(dialect=_DIALECT, compile_kwargs={"render_postcompile": True})
    return f"COPY ({c}) TO STDOUT (FORMAT csv, HEADER)", dict(c.params)


def stream_export(conn, select: Select, *, encoding: str = "utf-8-sig", gzip: bool = False,
                  chunk: int = 1 << 20) -> Iterator[bytes]:
    """逐塊輸出 CSV bytes。conn 為 sync psycopg Connection（連線端 client_encoding 須為 UTF8）。

    - utf-8-sig：開頭輸出 BOM，其餘原樣轉送。
    - big5：增量解碼 UTF-8 後嚴格編碼 big5，遇到無法表示的字元丟 ExportEncodingError。
    - gzip：zlib 串流壓縮（gzip 容器）。
    - generator 提早關閉（客戶端斷線）時呼叫 conn.cancel_safe()（psycopg 3.3 有此 API）。
    """
    if encoding not in ("utf-8-sig", "big5"):
        raise ValueError(f"不支援的編碼：{encoding}")
    sql, params = to_copy_sql(select)
    dec = codecs.getincrementaldecoder("utf-8")() if encoding == "big5" else None
    comp = zlib.compressobj(wbits=31) if gzip else None

    def encode(data: bytes, final: bool = False) -> bytes:
        if dec is None:
            return data
        text = dec.decode(data, final)
        try:
            return text.encode("big5", errors="strict")
        except UnicodeEncodeError as e:
            raise ExportEncodingError(
                f"Big5 無法表示字元 {text[e.start]!r}（U+{ord(text[e.start]):04X}），匯出中止；請改用 UTF-8") from None

    def pack(data: bytes) -> bytes:
        return comp.compress(data) if comp else data

    cancelled = False
    buf = bytearray()
    try:
        with conn.cursor() as cur:
            with cur.copy(sql, params) as cp:
                try:
                    if encoding == "utf-8-sig":
                        out = pack(codecs.BOM_UTF8)
                        if out:
                            yield out
                    for data in cp:
                        buf += data
                        if len(buf) >= chunk:
                            out = pack(encode(bytes(buf)))
                            buf.clear()
                            if out:
                                yield out
                    out = pack(encode(bytes(buf), final=True))
                    if comp:
                        out += comp.flush()
                    if out:
                        yield out
                except GeneratorExit:
                    cancelled = True
                    conn.cancel_safe()   # 先取消，再讓 with 區塊結束時排空連線
                    raise
    except Exception as e:
        # 客戶端斷線後的 QueryCanceled 是預期結果，不應往外傳；其餘例外照常傳遞
        if cancelled and type(e).__name__ == "QueryCanceled":
            return
        raise


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        s = format(v, "f")   # 避免 1E+3 這類科學記號
    elif isinstance(v, bool):
        s = "true" if v else "false"
    elif hasattr(v, "isoformat"):
        s = v.isoformat()
    else:
        s = str(v)
    if any(ch in s for ch in ("\t", "\n", "\r", '"')):
        s = '"' + s.replace('"', '""') + '"'
    return s


def to_tsv(header: list[str], rows: Iterable[Sequence], *, max_rows: int = 10_000,
           max_bytes: int = 10 * 1024 * 1024) -> str:
    """產生 Excel 相容 TSV（含表頭，列尾 \\n）。超過資料列數或位元組（UTF-8）上限丟 CopyLimitError。"""
    lines = ["\t".join(_cell(h) for h in header)]
    size = len(lines[0].encode("utf-8")) + 1
    n = 0
    for row in rows:
        n += 1
        if n > max_rows:
            raise CopyLimitError(f"超過複製筆數上限 {max_rows:,} 筆，請改用匯出 CSV")
        line = "\t".join(_cell(v) for v in row)
        size += len(line.encode("utf-8")) + 1
        if size > max_bytes:
            raise CopyLimitError(f"超過複製大小上限 {max_bytes / 1024 / 1024:.0f}MB，請改用匯出 CSV")
        lines.append(line)
    return "\n".join(lines) + "\n"

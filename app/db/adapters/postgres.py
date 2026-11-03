"""PostgreSQL 16+ adapter（v1 唯一實作）。所有 PostgreSQL 專屬語法集中在此（SPEC 13.1）。

識別字一律經 catalog.check_ident 驗證後再用 psycopg.sql.Identifier 組合；值一律綁定參數，
期別等已驗證的整數才用 sql.Literal。
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

import psycopg
from psycopg import errors, sql

from app.catalog import ColumnDef, DatasetDef, check_ident, parse_type, partition_name
from app.db.adapters.base import Adapter, AdapterError, LockBusy, SchemaChanged, ValidationResult

I = sql.Identifier
L = sql.Literal

# bigint 單鍵 advisory lock 的命名空間：資料集層用 dataset_id 本身（小整數）；
# 工作擁有權用 2^40 以上的鍵，兩者不會重疊。期別層用 (int, int) 雙鍵，與單鍵空間不衝突。
_JOB_KEY_BASE = 1 << 40

_INDEXDEF_RE = re.compile(r"^CREATE (UNIQUE )?INDEX \S+ ON ONLY \S+ (USING .+)$")


def _job_key(load_id: str) -> int:
    h = int.from_bytes(hashlib.sha256(load_id.encode()).digest()[:4], "big")
    return _JOB_KEY_BASE + h


def _pg_type(data_type: str) -> str:
    base, args = parse_type(data_type)
    if base == "varchar":
        return f"varchar({args[0]})"
    if base == "numeric":
        return f"numeric({args[0]},{args[1]})"
    return base


class PostgresAdapter(Adapter):
    # ------------------------------------------------------------------ DDL
    def type_map(self, data_type: str) -> str:
        return _pg_type(data_type)

    def _col_ddl(self, c: ColumnDef, *, not_null: bool) -> sql.Composable:
        parts = [I(check_ident(c.code)), sql.SQL(_pg_type(c.data_type))]
        if not_null:
            parts.append(sql.SQL("NOT NULL"))
        return sql.SQL(" ").join(parts)

    def create_table(self, conn, ds: DatasetDef) -> None:
        ds.validate()
        cols = [self._col_ddl(c, not_null=c.required) for c in ds.columns]
        pk = ds.pk_columns
        if pk:
            cols.append(sql.SQL("PRIMARY KEY ({})").format(sql.SQL(", ").join(I(c.code) for c in pk)))
        conn.execute(sql.SQL("CREATE TABLE {} ({}) PARTITION BY LIST ({})").format(
            I(ds.table_name), sql.SQL(", ").join(cols), I(ds.partition_key)))
        dcols = [self._col_ddl(c, not_null=False) for c in ds.columns]
        dcols += [sql.SQL("_load_id varchar(36) NOT NULL"),
                  sql.SQL("_operator_note varchar(200)"),
                  sql.SQL("_deleted_at timestamptz NOT NULL")]
        conn.execute(sql.SQL("CREATE TABLE {} ({})").format(I(ds.deleted_table), sql.SQL(", ").join(dcols)))
        conn.execute(sql.SQL("CREATE INDEX {} ON {} (_load_id)").format(
            I(f"{ds.deleted_table}_load"), I(ds.deleted_table)))

    def add_column(self, conn, ds: DatasetDef, col: ColumnDef) -> None:
        """呼叫端需已在同一交易取得資料集獨佔鎖。新增欄位一律可為 NULL（PG11+ 不重寫整表）；
        必填只約束之後的匯入驗證，不對既有資料加 NOT NULL。"""
        check_ident(col.code)
        for t in (ds.table_name, ds.deleted_table):
            conn.execute(sql.SQL("ALTER TABLE {} ADD COLUMN {}").format(
                I(t), self._col_ddl(col, not_null=False)))

    def check_primary_key(self, conn, ds: DatasetDef) -> None:
        """啟動檢查：metadata 主鍵 = pg_constraint 主鍵（SPEC 5.2）。"""
        row = conn.execute(
            """SELECT array_agg(a.attname ORDER BY k.ord)
                 FROM pg_constraint c
                 CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                 JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE c.conrelid = to_regclass(%s) AND c.contype = 'p'""",
            (ds.table_name,)).fetchall()
        actual = list(row[0][0] or []) if row else []
        expected = [c.code for c in ds.pk_columns]
        if actual != expected:
            raise AdapterError(f"{ds.name} 主鍵不一致：metadata={expected} 資料庫={actual}")

    def table_columns(self, conn, name: str) -> list[tuple[str, str]]:
        rows = conn.execute(
            """SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute
                WHERE attrelid = to_regclass(%s) AND attnum > 0 AND NOT attisdropped
                ORDER BY attnum""", (name,)).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ------------------------------------------------------------------ 鎖
    def xact_lock_dataset(self, conn, dataset_id: int, *, exclusive: bool) -> None:
        fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        self._lock(conn, f"SELECT {fn}(%s::bigint)", (dataset_id,))

    def xact_lock_period(self, conn, dataset_id: int, yyyymm: int) -> None:
        self._lock(conn, "SELECT pg_advisory_xact_lock(%s::int, %s::int)", (dataset_id, yyyymm))

    def session_lock_dataset_exclusive(self, conn, dataset_id: int) -> None:
        self._lock(conn, "SELECT pg_advisory_lock(%s::bigint)", (dataset_id,))

    def session_unlock_dataset_exclusive(self, conn, dataset_id: int) -> None:
        conn.execute("SELECT pg_advisory_unlock(%s::bigint)", (dataset_id,))

    def try_claim_job(self, conn, load_id: str) -> bool:
        return bool(conn.execute("SELECT pg_try_advisory_lock(%s::bigint)",
                                 (_job_key(load_id),)).fetchall()[0][0])

    def release_job(self, conn, load_id: str) -> None:
        conn.execute("SELECT pg_advisory_unlock(%s::bigint)", (_job_key(load_id),))

    @staticmethod
    def _lock(conn, q, params):
        try:
            conn.execute(q, params)
        except errors.LockNotAvailable as e:
            raise LockBusy(str(e)) from e

    # ------------------------------------------------------------------ staging
    def create_staging(self, conn, name: str, columns: list[str]) -> None:
        cols = [sql.SQL("_rn bigint GENERATED ALWAYS AS IDENTITY")]
        cols += [sql.SQL("{} text").format(I(check_ident(c))) for c in columns]
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(I(name)))
        conn.execute(sql.SQL("CREATE UNLOGGED TABLE {} ({})").format(I(name), sql.SQL(", ").join(cols)))

    def bulk_load(self, conn, name: str, columns: list[str], chunks: Iterable[bytes],
                  *, header_match: bool) -> int:
        """COPY FROM STDIN 串流（SPEC 7.3）。一律帶明確欄位清單；未加引號空欄與 "" 都轉 NULL。"""
        collist = sql.SQL(", ").join(I(c) for c in columns)
        q = sql.SQL(
            "COPY {} ({}) FROM STDIN (FORMAT csv, HEADER {}, NULL '', FORCE_NULL ({}), ENCODING 'UTF8')"
        ).format(I(name), collist, sql.SQL("MATCH" if header_match else "true"), collist)
        with conn.cursor() as cur:
            with cur.copy(q) as cp:
                for chunk in chunks:
                    cp.write(chunk)
            return cur.rowcount

    # 每個欄位的「不合法」條件（staging 為 text）
    def _invalid_conds(self, ds: DatasetDef, columns: list[str], yyyymm: int, *,
                       require_required: bool = True) -> list[tuple[str, sql.Composable]]:
        out: list[tuple[str, sql.Composable]] = []
        present = set(columns)
        for code in columns:
            c = ds.col(code)
            x = I(code)
            base, args = parse_type(c.data_type)
            if c.required or c.is_pk:
                out.append((f"{code}:必填為空", sql.SQL("{} IS NULL").format(x)))
            if code == ds.partition_key:
                out.append((f"{code}:期別不符", sql.SQL("{} IS DISTINCT FROM {}").format(x, L(str(yyyymm)))))
                continue
            if base in ("integer", "bigint"):
                cond = sql.SQL("NOT pg_input_is_valid({}, {})").format(x, L(base))
            elif base == "numeric":
                p, s = args
                cond = sql.SQL(
                    "({x} !~ '^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$'"
                    " OR length(ltrim(split_part(ltrim({x}, '+-'), '.', 1), '0')) > {ip}"
                    " OR length(split_part({x}, '.', 2)) > {s})"
                ).format(x=x, ip=L(p - s), s=L(s))
            elif base == "date":
                cond = sql.SQL("({x} !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$' OR NOT pg_input_is_valid({x}, 'date'))").format(x=x)
            else:  # varchar
                cond = sql.SQL("char_length({}) > {}").format(x, L(args[0]))
            out.append((f"{code}:{c.data_type} 格式不符", sql.SQL("({} IS NOT NULL AND {})").format(x, cond)))
        for c in ds.active_columns:
            if require_required and c.code not in present and c.required and c.code != ds.partition_key:
                raise AdapterError(f"缺少必填欄位：{c.code}")
        return out

    def validate_staging(self, conn, ds, stg, columns, yyyymm, report=None, *,
                         require_required: bool = True) -> ValidationResult:
        rows = self.row_count(conn, stg)
        res = ValidationResult(rows=rows)
        if rows == 0:
            res.messages.append("檔案沒有資料列")
            return res
        conds = self._invalid_conds(ds, columns, yyyymm, require_required=require_required)
        if conds:
            any_bad = sql.SQL(" OR ").join(c for _, c in conds)
            res.error_rows = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE {}").format(
                I(stg), any_bad)).fetchall()[0][0]
        if res.error_rows == 0 and ds.pk_columns:
            # 檔案沒有的主鍵欄（分割鍵以載入期別補值）是常數，不參與分組；
            # 不可把常數放進 GROUP BY（整數常數會被當成欄位位置）
            key_cols = [c.code for c in ds.pk_columns if c.code in columns]
            if key_cols:
                keys = sql.SQL(", ").join(self._cast_expr(ds, c, columns, yyyymm) for c in key_cols)
                res.dup_pk_groups = conn.execute(sql.SQL(
                    "SELECT count(*) FROM (SELECT 1 FROM {} GROUP BY {} HAVING count(*) > 1) d").format(
                    I(stg), keys)).fetchall()[0][0]
            else:
                res.dup_pk_groups = 1 if rows > 1 else 0
            if res.dup_pk_groups:
                res.messages.append(f"主鍵重複 {res.dup_pk_groups} 組")
        return res

    def write_error_report(self, conn, ds, stg, columns, yyyymm, out, limit: int, *,
                           require_required: bool = True) -> None:
        """錯誤列 CSV：CSV 記錄序號（_rn，不含表頭）、錯誤原因、原始值（前 20 欄）。"""
        conds = self._invalid_conds(ds, columns, yyyymm, require_required=require_required)
        # 不用 concat_ws(…)：200 欄會超過 PostgreSQL 函式參數上限 100；ARRAY[] 沒有此限制，array_to_string 略過 NULL
        reasons = sql.SQL("array_to_string(ARRAY[{}]::text[], '; ')").format(sql.SQL(", ").join(
            sql.SQL("CASE WHEN {} THEN {} END").format(c, L(label)) for label, c in conds))
        shown = sql.SQL(", ").join(I(c) for c in columns[:20])
        q = sql.SQL(
            "COPY (SELECT _rn AS csv_record_no, reasons, {shown} FROM ("
            " SELECT _rn, {reasons} AS reasons, {shown} FROM {stg}) e"
            " WHERE reasons <> '' ORDER BY _rn LIMIT {lim}) TO STDOUT (FORMAT csv, HEADER)"
        ).format(shown=shown, reasons=reasons, stg=I(stg), lim=L(limit))
        with conn.cursor() as cur, cur.copy(q) as cp:
            for data in cp:
                out.write(data)

    def _cast_expr(self, ds: DatasetDef, code: str, columns: list[str], yyyymm: int) -> sql.Composable:
        if code == ds.partition_key and code not in columns:
            return L(yyyymm)
        c = ds.col(code)
        if code not in columns:
            return sql.SQL("CAST(NULL AS {})").format(sql.SQL(_pg_type(c.data_type)))
        return sql.SQL("CAST(NULLIF({}, '') AS {})").format(I(code), sql.SQL(_pg_type(c.data_type)))

    def build_partition(self, conn, ds, stg, columns, yyyymm, newp, check_name) -> int:
        """交易外建好新分割（SPEC 7.4 BUILT）：資料 → 索引 → CHECK → ANALYZE。呼叫端以 autocommit 執行。"""
        self.drop_table(conn, newp)
        conn.execute(sql.SQL("CREATE TABLE {} (LIKE {} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)").format(
            I(newp), I(ds.table_name)))
        targets = [c.code for c in ds.active_columns if c.code in columns or c.code == ds.partition_key]
        conn.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(
            I(newp), sql.SQL(", ").join(I(c) for c in targets),
            sql.SQL(", ").join(self._cast_expr(ds, c, columns, yyyymm) for c in targets), I(stg)))
        # 預建與父表每個分割索引同定義的索引，讓 SWAP 時 ATTACH 直接掛接、不在提交交易內建索引。
        # 父表主鍵／唯一限制的索引只能由「同樣是限制」的子表索引掛接，所以用 ADD CONSTRAINT 建立。
        for i, (idx, condef) in enumerate(self._parent_indexdefs(conn, ds)):
            if condef is not None:
                conn.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} {}").format(
                    I(newp), I(f"{newp}_c{i}"), sql.SQL(condef)))
                continue
            m = _INDEXDEF_RE.match(idx)
            if not m:
                raise AdapterError(f"無法解析父表索引定義：{idx}")
            conn.execute(sql.SQL("CREATE {}INDEX {} ON {} {}").format(
                sql.SQL(m.group(1) or ""), I(f"{newp}_i{i}"), I(newp), sql.SQL(m.group(2))))
        conn.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} CHECK ({} = {})").format(
            I(newp), I(check_name), I(ds.partition_key), L(yyyymm)))
        conn.execute(sql.SQL("ANALYZE {}").format(I(newp)))
        return self.row_count(conn, newp)

    def _parent_indexdefs(self, conn, ds: DatasetDef) -> list[tuple[str, str | None]]:
        """父表索引定義與（若有）其背後的主鍵／唯一限制定義。"""
        rows = conn.execute(
            """SELECT pg_get_indexdef(i.indexrelid), pg_get_constraintdef(c.oid)
                 FROM pg_index i
                 LEFT JOIN pg_constraint c ON c.conindid = i.indexrelid AND c.contype IN ('p', 'u')
                                          AND c.conrelid = i.indrelid
                WHERE i.indrelid = to_regclass(%s) ORDER BY i.indexrelid""",
            (ds.table_name,)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def partition_indexes(self, conn, name: str) -> list[str]:
        return [r[0] for r in conn.execute(
            "SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid = to_regclass(%s)",
            (name,)).fetchall()]

    def checksums(self, conn, ds, table, columns, *, from_text: bool) -> dict[str, str]:
        cks = [c.code for c in ds.active_columns if c.checksum and c.code in columns]
        if not cks:
            return {}
        if from_text:
            exprs = [sql.SQL("COALESCE(SUM(CAST(NULLIF({}, '') AS {})), 0)").format(
                I(c), sql.SQL(_pg_type(ds.col(c).data_type))) for c in cks]
        else:
            exprs = [sql.SQL("COALESCE(SUM({}), 0)").format(I(c)) for c in cks]
        row = conn.execute(sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(", ").join(exprs), I(table))).fetchall()[0]
        return {c: str(v) for c, v in zip(cks, row)}

    def row_count(self, conn, table: str) -> int:
        return conn.execute(sql.SQL("SELECT count(*) FROM {}").format(I(table))).fetchall()[0][0]

    def partition_exists(self, conn, ds: DatasetDef, yyyymm: int) -> bool:
        return conn.execute(
            """SELECT 1 FROM pg_inherits WHERE inhparent = to_regclass(%s)
                  AND inhrelid = to_regclass(%s)""",
            (ds.table_name, partition_name(ds, yyyymm))).fetchall() != []

    def swap_period(self, conn, ds, yyyymm, newp, bak, check_name) -> bool:
        """唯一提交點的 DDL 部分（SPEC 7.4 SWAPPED）。呼叫端負責交易、取鎖、更新 load_log。
        回傳是否有舊分割被換下。"""
        parent_cols = self.table_columns(conn, ds.table_name)
        if sorted(self.table_columns(conn, newp)) != sorted(parent_cols):
            raise SchemaChanged("新分割與父表欄位不一致（BUILT 之後欄位定義已變更）")
        cur = partition_name(ds, yyyymm)
        had_old = self.partition_exists(conn, ds, yyyymm)
        try:
            if had_old:
                conn.execute(sql.SQL("ALTER TABLE {} DETACH PARTITION {}").format(I(ds.table_name), I(cur)))
                conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(I(cur), I(bak)))
            elif conn.execute("SELECT to_regclass(%s)", (cur,)).fetchall()[0][0] is not None:
                raise AdapterError(f"{cur} 存在但不是 {ds.table_name} 的分割，需人工處理")
            conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(I(newp), I(cur)))
            conn.execute(sql.SQL("ALTER TABLE {} ATTACH PARTITION {} FOR VALUES IN ({})").format(
                I(ds.table_name), I(cur), L(yyyymm)))
            conn.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(I(cur), I(check_name)))
        except errors.LockNotAvailable as e:
            raise LockBusy(str(e)) from e
        return had_old

    def drop_period(self, conn, ds, yyyymm, bak) -> None:
        cur = partition_name(ds, yyyymm)
        if not self.partition_exists(conn, ds, yyyymm):
            raise AdapterError(f"期別 {yyyymm} 不存在")
        try:
            conn.execute(sql.SQL("ALTER TABLE {} DETACH PARTITION {}").format(I(ds.table_name), I(cur)))
            conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(I(cur), I(bak)))
        except errors.LockNotAvailable as e:
            raise LockBusy(str(e)) from e

    def drop_table(self, conn, name: str) -> None:
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(I(name)))

    # ------------------------------------------------------------------ 增修／刪除
    def _pk_join(self, ds: DatasetDef, a: str, b: str) -> sql.Composable:
        return sql.SQL(" AND ").join(
            sql.SQL("{}.{} = {}.{}").format(I(a), I(c.code), I(b), I(c.code)) for c in ds.pk_columns)

    def upsert(self, conn, ds, stg, columns, yyyymm: int) -> tuple[int, int]:
        """INSERT … ON CONFLICT (pk) DO UPDATE（SPEC 8.1，不用 MERGE）。
        只更新檔案中有的非主鍵欄位；檔案沒有的欄位保留原值。回傳 (新增, 更新)。"""
        pk = [c.code for c in ds.pk_columns]
        if not pk:
            raise AdapterError("沒有主鍵的資料集只能整期重傳")
        targets = [c.code for c in ds.active_columns if c.code in columns or c.code == ds.partition_key]
        upd = [c for c in targets if c not in pk]
        set_clause = (sql.SQL("DO UPDATE SET {}").format(sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(I(c), I(c)) for c in upd)) if upd else sql.SQL("DO NOTHING"))
        # 分割表不支援 RETURNING xmax，改為先算既有鍵數；呼叫端已持有期別獨佔鎖，期間不會有其他寫入
        typed = sql.SQL("(SELECT {} FROM {}) k").format(
            sql.SQL(", ").join(sql.SQL("{} AS {}").format(self._cast_expr(ds, c, columns, yyyymm), I(c))
                               for c in pk), I(stg))
        existing = conn.execute(sql.SQL(
            "SELECT count(*) FROM {} WHERE EXISTS (SELECT 1 FROM {} t WHERE {} AND {})").format(
            typed, I(ds.table_name), self._period(ds, "t", yyyymm), self._pk_join(ds, "t", "k"))).fetchall()[0][0]
        q = sql.SQL(
            "INSERT INTO {t} ({cols}) SELECT {exprs} FROM {stg} ON CONFLICT ({pk}) {set}"
        ).format(t=I(ds.table_name), cols=sql.SQL(", ").join(I(c) for c in targets),
                 exprs=sql.SQL(", ").join(self._cast_expr(ds, c, columns, yyyymm) for c in targets),
                 stg=I(stg), pk=sql.SQL(", ").join(I(c) for c in pk), set=set_clause)
        total = conn.execute(q).rowcount
        if upd:
            return int(total - existing), int(existing)
        return int(total), 0

    def _period(self, ds: DatasetDef, alias: str, yyyymm: int) -> sql.Composable:
        """目標表的期別常數條件：讓 planner 在計畫階段只取單一分割（驗證已保證 staging 期別相同）。"""
        return sql.SQL("{}.{} = {}").format(I(alias), I(ds.partition_key), L(yyyymm))

    def analyze(self, conn, name: str) -> None:
        conn.execute(sql.SQL("ANALYZE {}").format(I(name)))

    def _typed_keys(self, ds: DatasetDef, stg: str) -> sql.Composable:
        """刪除清單 staging（text）轉型後的子查詢，欄名同主鍵。"""
        cols = [c.code for c in ds.pk_columns]
        return sql.SQL("(SELECT {} FROM {}) k").format(
            sql.SQL(", ").join(sql.SQL("{} AS {}").format(self._cast_expr(ds, c, cols, 0), I(c)) for c in cols),
            I(stg))

    def delete_preview(self, conn, ds, stg, sample, yyyymm: int) -> tuple[int, int, list[tuple]]:
        pkc = sql.SQL(", ").join(sql.SQL("k.{}").format(I(c.code)) for c in ds.pk_columns)
        first = I(ds.pk_columns[0].code)
        # 單次 LEFT JOIN（主鍵唯一，不會放大列數）；t.<主鍵第一欄> NOT NULL，可用來判斷有無對應
        joined = sql.SQL("{k} LEFT JOIN {t} t ON {p} AND {on}").format(
            k=self._typed_keys(ds, stg), t=I(ds.table_name), p=self._period(ds, "t", yyyymm),
            on=self._pk_join(ds, "t", "k"))
        matched, unmatched = conn.execute(sql.SQL(
            "SELECT count(t.{f}), count(*) - count(t.{f}) FROM {j}").format(f=first, j=joined)).fetchall()[0]
        missing = conn.execute(sql.SQL("SELECT {} FROM {} WHERE t.{} IS NULL ORDER BY {} LIMIT {}").format(
            pkc, joined, first, pkc, L(sample))).fetchall()
        return int(matched), int(unmatched), missing

    def delete_by_keys(self, conn, ds, stg, load_id, operator_note, yyyymm: int) -> int:
        """單一陳述式刪除並寫入 _deleted（SPEC 8.2 步驟 4）。呼叫端負責交易與筆數比對。"""
        cols = sql.SQL(", ").join(I(c.code) for c in ds.columns)
        tcols = sql.SQL(", ").join(sql.SQL("t.{}").format(I(c.code)) for c in ds.columns)
        q = sql.SQL(
            "WITH d AS (DELETE FROM {t} t USING {k} WHERE {p} AND {on} RETURNING {tcols}),"
            " ins AS (INSERT INTO {dt} ({cols}, _load_id, _operator_note, _deleted_at)"
            " SELECT {cols}, %s, %s, now() FROM d RETURNING 1)"
            " SELECT count(*) FROM ins"
        ).format(t=I(ds.table_name), k=self._typed_keys(ds, stg), on=self._pk_join(ds, "t", "k"),
                 p=self._period(ds, "t", yyyymm), tcols=tcols, dt=I(ds.deleted_table), cols=cols)
        return int(conn.execute(q, (load_id, operator_note)).fetchall()[0][0])

    def restore_deleted(self, conn, ds, load_id) -> tuple[int, int]:
        """依 load_id 還原；主鍵已被重新寫入的列不覆蓋，回傳 (還原, 衝突略過)。"""
        cols = sql.SQL(", ").join(I(c.code) for c in ds.columns)
        pk = sql.SQL(", ").join(I(c.code) for c in ds.pk_columns)
        total = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE _load_id = %s").format(
            I(ds.deleted_table)), (load_id,)).fetchall()[0][0]
        # 只從 _deleted 移除「這次真的還原」的列（RETURNING 主鍵）；因衝突略過的列保留備份
        n = conn.execute(sql.SQL(
            "WITH r AS (INSERT INTO {t} ({cols}) SELECT {cols} FROM {dt} WHERE _load_id = %s"
            " ON CONFLICT ({pk}) DO NOTHING RETURNING {pk}),"
            " d AS (DELETE FROM {dt} d USING r WHERE d._load_id = %s AND {on} RETURNING 1)"
            " SELECT (SELECT count(*) FROM r), (SELECT count(*) FROM d)").format(
            t=I(ds.table_name), cols=cols, dt=I(ds.deleted_table), pk=pk,
            on=self._pk_join(ds, "d", "r")), (load_id, load_id)).fetchall()[0]
        if n[0] != n[1]:
            raise AdapterError(f"還原 {n[0]} 筆但 _deleted 移除 {n[1]} 筆，已回滾")
        return int(n[0]), int(total - n[0])

    def purge_deleted(self, conn, ds, days: int) -> int:
        return conn.execute(sql.SQL(
            "DELETE FROM {} WHERE _deleted_at < now() - make_interval(days => %s)").format(
            I(ds.deleted_table)), (days,)).rowcount

    # ------------------------------------------------------------------ 交易輔助
    def set_lock_timeout(self, conn, value: str) -> None:
        if not re.fullmatch(r"\d{1,5}(ms|s)", value):
            raise AdapterError("lock_timeout 格式錯誤")
        conn.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(L(value)))

    def set_read_only(self, conn) -> None:
        conn.execute("SET TRANSACTION READ ONLY")

    def table_exists(self, conn, name: str) -> bool:
        return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchall()[0][0]

    # ------------------------------------------------------------------ 查詢資源保護
    _SLOT_BASE = 1 << 42

    def try_slot(self, conn, namespace: str, ident: str, slots: int) -> int | None:
        """跨 worker 的名額：在 (namespace, ident) 的 slots 個 advisory key 中搶一個，持有到 release_slot。
        回傳取得的 key；全滿回 None。"""
        h = int.from_bytes(hashlib.sha256(f"{namespace}:{ident}".encode()).digest()[:4], "big") & 0x3FFFFFF
        for i in range(slots):
            key = self._SLOT_BASE + (h << 5) + i
            if conn.execute("SELECT pg_try_advisory_lock(%s::bigint)", (key,)).fetchall()[0][0]:
                return key
        return None

    def release_slot(self, conn, key: int) -> None:
        conn.execute("SELECT pg_advisory_unlock(%s::bigint)", (key,))

    def tag_connection(self, conn, tag: str) -> None:
        if not re.fullmatch(r"[a-z0-9-]{1,60}", tag):
            raise AdapterError("tag 格式錯誤")
        conn.execute(sql.SQL("SET application_name = {}").format(L(tag)))

    def cancel_tagged(self, conn, tag: str) -> bool:
        """取消同角色、application_name = tag 的執行中查詢（PID 由伺服器查出，不接受客戶端指定）。"""
        rows = conn.execute(
            """SELECT pg_cancel_backend(pid) FROM pg_stat_activity
                WHERE application_name = %s AND usename = current_user AND pid <> pg_backend_pid()""",
            (tag,)).fetchall()
        return any(r[0] for r in rows)

    def list_periods(self, conn, ds: DatasetDef) -> list[tuple[int, int]]:
        """已掛上父表的期別與估計筆數（reltuples，未 ANALYZE 為 -1 → 0）。期別取自分割界限，不依賴表名。"""
        rows = conn.execute(
            """SELECT pg_get_expr(c.relpartbound, c.oid), GREATEST(c.reltuples, 0)::bigint
                 FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
                WHERE i.inhparent = to_regclass(%s)""", (ds.table_name,)).fetchall()
        out = []
        for bound, est in rows:
            m = re.fullmatch(r"FOR VALUES IN \((\d{6})\)", bound or "")
            if m:
                out.append((int(m.group(1)), int(est)))
        return sorted(out, reverse=True)

    def estimate_rows(self, conn, ds: DatasetDef) -> int:
        """父表所有分割的 reltuples 合計（ANALYZE 後的估計值；-1 代表未分析，視為 0）。"""
        return int(conn.execute(
            """SELECT COALESCE(SUM(GREATEST(c.reltuples, 0)), 0) FROM pg_inherits i
                 JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = to_regclass(%s)""",
            (ds.table_name,)).fetchall()[0][0])

    # ------------------------------------------------------------------ 查詢
    def set_session_timeout(self, conn, ms: int | None) -> None:
        """連線層級 statement_timeout（匯出在交易外以 COPY 串流）；None 表示還原預設。"""
        if ms is None:
            conn.execute("RESET statement_timeout")
        else:
            conn.execute(sql.SQL("SET statement_timeout = {}").format(L(int(ms))))

    def set_timeout(self, conn, ms: int) -> None:
        conn.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(L(int(ms))))

    def explain_estimate(self, conn, sql_text: str, params) -> dict:
        plan = conn.execute("EXPLAIN (FORMAT JSON) " + sql_text, params).fetchall()[0][0][0]["Plan"]
        seq = []

        def walk(n):
            if n.get("Node Type") == "Seq Scan":
                seq.append(n.get("Relation Name"))
            for ch in n.get("Plans", []):
                walk(ch)
        walk(plan)
        return {"rows": plan.get("Plan Rows"), "cost": plan.get("Total Cost"), "seq_scans": seq}


def get_adapter() -> Adapter:
    return PostgresAdapter()


__all__ = ["PostgresAdapter", "get_adapter", "psycopg"]

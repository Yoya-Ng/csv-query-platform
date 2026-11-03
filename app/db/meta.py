"""系統中繼資料表（SPEC 5.1），由 Alembic 管理。資料集實體表不在這裡，由 adapter 依 metadata 產生。"""
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, MetaData, String, Table,
    Text, func, text,
)

metadata = MetaData()

meta_dataset = Table(
    "meta_dataset", metadata,
    Column("dataset_id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(30), nullable=False, unique=True),
    Column("partition_key", String(30), nullable=False, server_default="yyyymm"),
    Column("description", Text, nullable=False, server_default=""),
    Column("schema_version", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

meta_column = Table(
    "meta_column", metadata,
    Column("dataset_id", Integer, ForeignKey("meta_dataset.dataset_id"), primary_key=True),
    Column("code", String(30), primary_key=True),
    Column("display_name", String(200), nullable=False, server_default=""),
    Column("aliases", Text, nullable=False, server_default=""),   # 以 '|' 分隔
    Column("data_type", String(30), nullable=False),
    Column("required", Boolean, nullable=False, server_default=text("false")),
    Column("is_pk", Boolean, nullable=False, server_default=text("false")),
    Column("pk_order", Integer, nullable=False, server_default="0"),
    Column("checksum", Boolean, nullable=False, server_default=text("false")),
    Column("group_name", String(100), nullable=False, server_default=""),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    Column("description", Text, nullable=False, server_default=""),
    Column("disabled", Boolean, nullable=False, server_default=text("false")),
    Column("added_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

upload_session = Table(
    "upload_session", metadata,
    Column("upload_id", String(36), primary_key=True),
    Column("filename", String(255), nullable=False),
    Column("total_chunks", Integer, nullable=False),
    Column("total_bytes", BigInteger, nullable=False),
    Column("chunk_sha256", Text, nullable=False, server_default=""),  # 以 ',' 分隔，依序號
    Column("received", Text, nullable=False, server_default=""),      # 已收到的序號，','
    Column("file_sha256", String(64)),
    Column("status", String(20), nullable=False, server_default="OPEN"),  # OPEN/COMPLETE/EXPIRED
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

load_log = Table(
    "load_log", metadata,
    Column("load_id", String(36), primary_key=True),
    Column("dataset_id", Integer, ForeignKey("meta_dataset.dataset_id"), nullable=False),
    Column("yyyymm", Integer, nullable=False),
    Column("mode", String(10), nullable=False),          # replace / upsert / delete / drop
    Column("upload_id", String(36)),
    Column("filename", String(255)),
    Column("file_sha256", String(64)),
    Column("status", String(20), nullable=False),        # 見 app.services.importer.State
    Column("schema_version", Integer),
    Column("error", Text),
    Column("rows_csv", BigInteger),
    Column("rows_staged", BigInteger),
    Column("rows_rejected", BigInteger),
    Column("rows_inserted", BigInteger),
    Column("rows_updated", BigInteger),
    Column("rows_deleted", BigInteger),
    Column("rows_matched", BigInteger),                  # 刪除預覽：符合筆數
    Column("rows_unmatched", BigInteger),                # 刪除預覽：不存在筆數
    Column("checksums", Text),                           # JSON：{欄位: SUM 字串}
    Column("columns", Text),                             # JSON：依表頭順序的實體欄名
    Column("header_match", Boolean),
    Column("detail", Text),                              # JSON：預覽明細等
    Column("bak_table", String(63)),
    Column("operator_note", String(200)),
    Column("client_ip", String(45)),
    Column("cleanup_status", String(20), nullable=False, server_default="PENDING"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("staged_at", DateTime(timezone=True)),
    Column("validated_at", DateTime(timezone=True)),
    Column("built_at", DateTime(timezone=True)),
    Column("swapped_at", DateTime(timezone=True)),
    Column("cleaned_at", DateTime(timezone=True)),
)

# 進行中的狀態：同一資料集同一期別同時只允許一筆（避免兩次重傳或重傳與增修互相覆蓋）
ACTIVE_STATES = ("UPLOADED", "STAGED", "VALIDATED", "BUILT", "PREVIEWED")
Index(
    "uq_load_log_active", load_log.c.dataset_id, load_log.c.yyyymm, unique=True,
    postgresql_where=load_log.c.status.in_(ACTIVE_STATES),
)

saved_query = Table(
    "saved_query", metadata,
    Column("query_id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(200), nullable=False),
    Column("spec", Text, nullable=False),
    Column("schema_versions", Text, nullable=False),     # JSON：{dataset: version}
    Column("note", String(200), nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

audit_log = Table(
    "audit_log", metadata,
    Column("audit_id", BigInteger, primary_key=True, autoincrement=True),
    Column("action", String(50), nullable=False),
    Column("dataset_id", Integer),
    Column("detail", Text, nullable=False, server_default=""),
    Column("operator_note", String(200)),
    Column("client_ip", String(45)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

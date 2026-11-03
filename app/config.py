"""應用設定：一律由環境變數帶入（docker-compose env_file），不寫死密碼。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CQP_", env_file=None)

    # 連線字串（psycopg conninfo 格式）。app_rw：寫入；app_ro：唯讀查詢（SPEC 9.5）
    dsn_rw: str = "postgresql://app_rw@localhost:5432/cqp"
    dsn_ro: str = "postgresql://app_ro@localhost:5432/cqp"

    # 上傳暫存目錄，必須獨立於資料目錄（SPEC 7.1）
    upload_dir: Path = Path("/var/lib/cqp/uploads")
    error_report_dir: Path = Path("/var/lib/cqp/errors")

    chunk_size: int = 16 * 1024 * 1024          # 分塊上傳每塊大小
    max_upload_bytes: int = 50 * 1024**3        # 單檔上限
    max_gzip_ratio: int = 20                    # 解壓比上限
    copy_block: int = 8 * 1024 * 1024           # COPY 串流區塊

    swap_lock_timeout: str = "3s"
    swap_retry_total_seconds: float = 120.0
    bak_keep_days: int = 7
    bak_keep_per_period: int = 2
    deleted_keep_days: int = 7

    query_statement_timeout_ms: int = 300_000
    export_statement_timeout_ms: int = 1_800_000
    big_query_concurrency: int = 4
    export_concurrency: int = 2

    pool_query: int = 12
    pool_import: int = 2
    pool_export: int = 4
    pool_system: int = 2

    disk_safety_factor: float = 1.5


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""DB Adapter 介面（SPEC 13.2）。Service 層只透過這個介面操作資料集實體表。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from app.catalog import ColumnDef, DatasetDef


class AdapterError(RuntimeError):
    pass


class LockBusy(AdapterError):
    """取鎖逾時（lock_timeout），可重試。"""


class SchemaChanged(AdapterError):
    """BUILT 之後欄位定義改變，SWAP 前比對失敗（AC9）。"""


@dataclass
class ValidationResult:
    rows: int
    error_rows: int = 0
    dup_pk_groups: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error_rows == 0 and self.dup_pk_groups == 0 and not self.messages


class Adapter(ABC):
    # ---- DDL ----
    @abstractmethod
    def create_table(self, conn, ds: DatasetDef) -> None: ...
    @abstractmethod
    def add_column(self, conn, ds: DatasetDef, col: ColumnDef) -> None: ...
    @abstractmethod
    def check_primary_key(self, conn, ds: DatasetDef) -> None: ...
    @abstractmethod
    def type_map(self, data_type: str) -> str: ...

    # ---- 鎖 ----
    @abstractmethod
    def xact_lock_dataset(self, conn, dataset_id: int, *, exclusive: bool) -> None: ...
    @abstractmethod
    def xact_lock_period(self, conn, dataset_id: int, yyyymm: int) -> None: ...
    @abstractmethod
    def session_lock_dataset_exclusive(self, conn, dataset_id: int) -> None: ...
    @abstractmethod
    def session_unlock_dataset_exclusive(self, conn, dataset_id: int) -> None: ...
    @abstractmethod
    def try_claim_job(self, conn, load_id: str) -> bool: ...
    @abstractmethod
    def release_job(self, conn, load_id: str) -> None: ...

    # ---- 匯入 ----
    @abstractmethod
    def create_staging(self, conn, name: str, columns: list[str]) -> None: ...
    @abstractmethod
    def bulk_load(self, conn, name: str, columns: list[str], chunks: Iterable[bytes],
                  *, header_match: bool) -> int: ...
    @abstractmethod
    def validate_staging(self, conn, ds: DatasetDef, stg: str, columns: list[str],
                         yyyymm: int, report: Iterator | None = None) -> ValidationResult: ...
    @abstractmethod
    def write_error_report(self, conn, ds: DatasetDef, stg: str, columns: list[str],
                           yyyymm: int, out, limit: int) -> None: ...
    @abstractmethod
    def build_partition(self, conn, ds: DatasetDef, stg: str, columns: list[str],
                        yyyymm: int, newp: str, check_name: str) -> int: ...
    @abstractmethod
    def checksums(self, conn, ds: DatasetDef, table: str, columns: list[str],
                  *, from_text: bool) -> dict[str, str]: ...
    @abstractmethod
    def row_count(self, conn, table: str) -> int: ...
    @abstractmethod
    def swap_period(self, conn, ds: DatasetDef, yyyymm: int, newp: str, bak: str,
                    check_name: str) -> bool: ...
    @abstractmethod
    def drop_period(self, conn, ds: DatasetDef, yyyymm: int, bak: str) -> None: ...
    @abstractmethod
    def drop_table(self, conn, name: str) -> None: ...
    @abstractmethod
    def partition_exists(self, conn, ds: DatasetDef, yyyymm: int) -> bool: ...

    # ---- 增修／刪除 ----
    @abstractmethod
    def upsert(self, conn, ds: DatasetDef, stg: str, columns: list[str], yyyymm: int) -> tuple[int, int]: ...
    @abstractmethod
    def delete_preview(self, conn, ds: DatasetDef, stg: str, sample: int, yyyymm: int
                       ) -> tuple[int, int, list[tuple]]: ...
    @abstractmethod
    def delete_by_keys(self, conn, ds: DatasetDef, stg: str, load_id: str,
                       operator_note: str, yyyymm: int) -> int: ...
    @abstractmethod
    def restore_deleted(self, conn, ds: DatasetDef, load_id: str) -> tuple[int, int]: ...

    # ---- 查詢 ----
    @abstractmethod
    def set_timeout(self, conn, ms: int) -> None: ...
    @abstractmethod
    def explain_estimate(self, conn, sql: str, params) -> dict: ...

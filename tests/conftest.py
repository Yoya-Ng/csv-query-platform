"""資料庫整合測試共用設定。需要環境變數 CQP_TEST_DSN（app_rw 角色）；沒有就略過 db 測試。"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest

from app.catalog import ColumnDef, DatasetDef
from app.config import Settings
from app.db import catalog_repo
from app.db.adapters.postgres import PostgresAdapter

DSN = os.environ.get("CQP_TEST_DSN")


def pytest_collection_modifyitems(config, items):
    if DSN:
        return
    skip = pytest.mark.skip(reason="未設定 CQP_TEST_DSN")
    for it in items:
        if "db" in it.keywords:
            it.add_marker(skip)


@pytest.fixture(scope="session")
def migrated():
    """確保中繼資料表已依 Alembic 建立。"""
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", DSN.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    return True


@pytest.fixture
def conn(migrated):
    c = psycopg.connect(DSN, autocommit=True)
    yield c
    c.close()


@pytest.fixture
def conn2(migrated):
    c = psycopg.connect(DSN, autocommit=True)
    yield c
    c.close()


@pytest.fixture
def adapter():
    return PostgresAdapter()


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "up").mkdir()
    return Settings(dsn_rw=DSN or "", dsn_ro=DSN or "", upload_dir=tmp_path / "up",
                    error_report_dir=tmp_path / "err", swap_retry_total_seconds=2.0,
                    swap_lock_timeout="300ms")


def small_dataset(name: str) -> DatasetDef:
    return DatasetDef(dataset_id=0, name=name, columns=(
        ColumnDef("yyyymm", "integer", required=True, is_pk=True, pk_order=0, sort_order=0),
        ColumnDef("policy_no", "varchar(20)", required=True, is_pk=True, pk_order=1, sort_order=1,
                  aliases=("保單號碼",)),
        ColumnDef("amt", "numeric(18,2)", checksum=True, sort_order=2, aliases=("金額",)),
        ColumnDef("qty", "integer", sort_order=3),
        ColumnDef("eff_date", "date", sort_order=4),
        ColumnDef("note", "varchar(50)", sort_order=5),
    ))


@pytest.fixture
def dataset(conn, adapter):
    name = "t_" + uuid.uuid4().hex[:8]
    ds = catalog_repo.create_dataset(conn, adapter, small_dataset(name))
    yield ds
    # 清理：本測試建立的實體表與 metadata
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s OR tablename LIKE 'stg_%%'",
                        (name + "%",)).fetchall()
    for (t,) in rows:
        if t.startswith(name):
            conn.execute(psycopg.sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(t)))
    conn.execute("DELETE FROM load_log WHERE dataset_id = %s", (ds.dataset_id,))
    conn.execute("DELETE FROM audit_log WHERE dataset_id = %s", (ds.dataset_id,))
    conn.execute("DELETE FROM meta_column WHERE dataset_id = %s", (ds.dataset_id,))
    conn.execute("DELETE FROM meta_dataset WHERE dataset_id = %s", (ds.dataset_id,))

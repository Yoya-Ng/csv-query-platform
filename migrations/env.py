"""Alembic 只管理系統中繼資料表（SPEC 13.1）；資料集實體表由 adapter 產生，autogenerate 時排除。"""
import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.meta import metadata

config = context.config


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("CQP_DSN_RW", "")
    if not url:
        raise RuntimeError("請設定 CQP_DSN_RW")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def include_object(obj, name, type_, reflected, compare_to):
    # 資料庫中存在、但 metadata 沒有的表（資料集實體表、staging、_bak）一律忽略
    if type_ == "table" and reflected and compare_to is None:
        return False
    return True


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=metadata, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()

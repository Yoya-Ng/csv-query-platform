# csv-query-platform

大量 CSV 資料查詢平台（規格：SPEC.md；實作說明：docs/implementation.md）。

- 測試：`uv run python tools/withenv.py -- uv run pytest -q`（需先啟動 dev db，見 docs/implementation.md）
- 所有 PostgreSQL 專屬 SQL 只放在 `app/db/adapters/postgres.py`。

專案守則（改程式前先看）：
@.claude/lessons.md

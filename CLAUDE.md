# csv-query-platform

大量 CSV 資料查詢平台（規格：SPEC.md；實作說明：docs/implementation.md）。

- 測試：`.venv/bin/python tools/withenv.py -- .venv/bin/python -m pytest -q`（需 `.env` 的 `CQP_TEST_DSN`／`CQP_TEST_DSN_RO` 指向獨立測試庫 `cqp_test`，不可指向平台庫；見 docs/infra.md）
- 依賴：`requirements.txt`／`requirements-dev.txt`（`==` 鎖版本，不用 uv）；Docker image 以 `wheels/`（Python）與 `npm-cache/`（前端）離線安裝。
- 所有 PostgreSQL 專屬 SQL 只放在 `app/db/adapters/postgres.py`。

專案守則（改程式前先看）：
@.claude/lessons.md

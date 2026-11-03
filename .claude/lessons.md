# 專案守則（csv-query-platform）

從 Notion「🧯 問題修正紀錄」（主題：大量CSV查詢平台規格書／實作）提煉。改到相關檔案前先看。

## 分割與 SWAP（app/db/adapters/postgres.py）
- 預建新分割時，父表的主鍵／唯一「限制」索引必須用 `ALTER TABLE … ADD CONSTRAINT` 建立；一般 `CREATE UNIQUE INDEX` 不會被 ATTACH 掛接，會在提交交易內重建索引並持鎖。`LIKE … INCLUDING CONSTRAINTS` 不複製主鍵與索引。改完用 `tests/test_review_regressions.py::test_swap_keeps_single_pk_index` 驗證。（#247 復發）
- 分割表不支援 `RETURNING xmax`；`GROUP BY` 不可放整數常數（會被當成欄位位置）。
- 對分割父表的 join／EXISTS（upsert、刪除、預覽）一律在目標表加期別常數條件，並用 EXPLAIN 確認只出現單一分割。

## 輸入檔（app/services/csvfile.py）
- gzip 要處理多成員，結尾未正常結束即拒絕；解壓比寬限依「解壓後」大小計算。
- 新增驗證或報表 SQL 時，用 200 欄規模測（PostgreSQL 函式參數上限 100，改用 `ARRAY[...]`）。

## 查詢（app/services/query/）
- SQLAlchemy 2.1 只用 `postgresql.psycopg2` dialect 編譯（psycopg dialect 會加 `::TYPE`）。
- LEFT JOIN 右表的期別條件不可強制寫在 where（會變成 INNER JOIN）；經 on 的分割鍵相等傳遞。

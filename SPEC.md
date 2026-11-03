# 大量 CSV 資料查詢平台 — 規格書 v0.8.1

- 狀態：規格定稿待 PoC（尚未實作、尚未實測）
- 日期：2026-11-03
- 審查：Codex 第二意見 3 輪（Thread `01a19ec6-743c-76f1-9521-e212bbde581a`），最後一輪唯一高嚴重度問題已修正；紀錄見 Notion「🧯 問題修正紀錄」主題「大量CSV查詢平台規格書」
- 標示慣例：**[事實]** 已確認（官方文件或實測）；**[推論]** 合理推論；**[待查證]** 尚未驗證；**[待確認]** 需使用者決定

---

## 0. 結論

| 問題 | 結論 |
|---|---|
| 整體架構 | PostgreSQL 17 + FastAPI（Python）+ Vue 3 + AG Grid + Caddy（HTTPS）；app、Caddy 以 docker-compose 部署，PostgreSQL 自行安裝（v0.8.1 起不在 Docker 內）。資料一律在 DB 內運算，Python 不整批載入記憶體 |
| 取代 psql 的方法 | psycopg 3 `cursor.copy()`。**[事實]** 與 psql `\copy` 同為 `COPY ... FROM STDIN / TO STDOUT` 協定；**[推論]** 速度同級，PoC 實測。不用 pandas `to_sql`、逐筆 INSERT |
| JDBC 會不會慢 | JDBC 本身不慢，看用法。pgjdbc `CopyManager` 走 COPY 協定（**[推論]** 與 psql 同級）；batch INSERT（`reWriteBatchedInserts=true`）較慢；**[事實]** pgjdbc 預設整個結果集載入記憶體，大量讀取需 `autoCommit=false` + `setFetchSize`。Python 經 JDBC（JayDeBeApi）不建議（**[推論]** JVM↔Python 逐值轉換） |
| 欄位維護 | 中繼資料表 + 實體寬表；新增欄位走 `ALTER TABLE ADD COLUMN`；不用 EAV / JSONB 存主資料 |
| 查詢 | 前端組 JSON 查詢規格，後端白名單轉 SQL（SQLAlchemy Core）。不開放使用者輸入 SQL |
| 資料儲存 | 只存 PostgreSQL（不另存 Parquet / DuckDB） |
| 換資料庫 | 日後可能換其他產品；v1 只實作 PostgreSQL，資料庫專屬操作集中在 DB Adapter |

## 1. 需求

### 1.1 使用者原始需求
1. 上傳大量 CSV，筆數可能破千萬～上億
2. 資料欄位要好維護，可能新增欄位
3. 網頁查詢資料，能 join、自訂欄位加減乘除，匯出查詢結果 CSV 或複製貼上
4. 以前用 psql 上傳下載很快，要用什麼替代
5. 使用 JDBC 會不會很慢

### 1.2 已確認的條件
| 項目 | 內容 |
|---|---|
| 部署 | 本機 Docker。正式主機 Windows（RAM 32GB）+ Docker Desktop（WSL2）；目前在 Mac 測試（Apple M2、8 核、RAM 16GB、Docker 分配約 7.65GB、磁碟剩約 16GB） |
| 使用者 | 同時 ≤ 20 人，不做登入與權限 |
| 資料量 | 單一資料集約 200 欄；每期約 1,000 萬筆；保留 12 期 |
| 更新方式 | 整期重傳、零星增修、刪除都會有 |
| 查詢 | 會 join，但一定會下條件篩選 |
| 儲存 | 只存 PostgreSQL |
| 未來 | 可能換成其他資料庫產品（Greenplum、Oracle；SAP IQ **[待確認]**） |

## 2. 系統架構

```
瀏覽器 (Vue 3 + AG Grid)
   │ HTTPS
Caddy（tls internal / 公司憑證；IP allowlist；自行寫入 X-Forwarded-For）
   │
FastAPI (uvicorn, 多 worker；app port 不對外)
   ├─ upload service   分塊上傳 → 驗證表頭 → COPY staging → 驗證 → 建新分割 → 切換
   ├─ schema service   中繼資料 CRUD、ALTER TABLE
   ├─ query service    JSON 查詢規格 → AST 白名單 → SQLAlchemy Core
   ├─ export service   COPY (SELECT…) TO STDOUT → StreamingResponse
   ├─ job runner       背景工作（匯入、切換、清理、備份），前端輪詢狀態
   └─ db/adapters/     postgres.py（v1）；greenplum.py、oracle.py（日後）
   │
PostgreSQL 17（app_rw 寫入角色 / app_ro 唯讀查詢角色；自行安裝於主機，不在 Docker 內）
```

## 3. 部署與環境

### 3.1 docker-compose
| 服務 | 說明 |
|---|---|
| ~~`db`~~ | v0.8.1 移除：PostgreSQL 改為自行安裝（本機或其他主機），平台以 `CQP_DSN_RW`／`CQP_DSN_RO` 連線；參數改在 `postgresql.conf` 設定（值同 3.3），角色以 `sql/setup_roles.sql` 建立 |
| `app` | FastAPI + uvicorn，`psycopg_pool` |
| `proxy` | Caddy，對區網提供 HTTPS；`trusted_proxies` 為空 |

- 連線資訊放 repo 根目錄 `.env`（Compose 自動讀取），`.gitignore` 與 `.dockerignore` 排除；範本 `.env.example`。
- Python 依賴以 `requirements.txt`（`==` 鎖版本）管理，Docker image 以 `pip install --no-index --find-links` 從 `wheels/` 離線安裝。
- 最低 PostgreSQL 版本：15（`HEADER MATCH`、`MERGE` 等）；開發與正式皆用 17。

### 3.2 Windows 主機
- **[事實]** WSL2 預設只用主機一半 RAM。`%UserProfile%\.wslconfig`：
  ```ini
  [wsl2]
  memory=24GB
  processors=8
  swap=8GB
  ```
- （v0.8.1）PostgreSQL 改為直接安裝在 Windows 主機，資料目錄放主機本機 NVMe，不經 WSL2。上方 `.wslconfig` 的 24GB 是為 DB 容器配置；DB 移出後 WSL2 只跑 app／proxy，應調低（**[推論]** 約 4～6GB，依壓測調整），否則與主機上的 PostgreSQL 搶記憶體（3.3 的 20GB 改為留給主機上的 PostgreSQL）。
- Windows 防火牆開放 443；其他人用 `https://<主機名或 IP>` 連線，各電腦需匯入 Caddy 根憑證一次（或改用公司 CA）。

### 3.3 PostgreSQL 初始參數（DB 可用約 20GB；PoC 壓測後調整）
| 參數 | 值 | 說明 |
|---|---|---|
| `shared_buffers` | 6GB | |
| `effective_cache_size` | 16GB | |
| `work_mem` | 64MB | **[事實]** PG15+ `hash_mem_multiplier` 預設 2.0，hash 節點可用 2×work_mem；每個排序/雜湊節點、每個平行 worker 各自計算 |
| `maintenance_work_mem` | 2GB | |
| `max_parallel_workers_per_gather` | 2 | |
| `max_wal_size` | 16GB | |
| `max_connections` | 50 | |

記憶體檢查公式（只作檢查，不是上限保證）：`大查詢併發 × (1 + parallel workers) × 每查詢 hash/sort 節點數 × work_mem × hash_mem_multiplier + shared_buffers ≤ DB 可用記憶體`；以 PoC 20 人併發腳本觀察實際峰值。

### 3.4 Mac 測試環境
- 只驗證功能與相對速度；容量與查詢效能一律以 Windows 實測為準（**[推論]** 資料量超過記憶體後快取行為不同，無法線性外推）。
- PoC 預設 50 萬筆 × 200 欄；100 萬筆需先確認可用空間 ≥ 20GB（含 `docker system df` 與 Docker Desktop 磁碟映像上限），或使用外接 SSD。

## 4. 容量規劃

- **[推論]** 估算公式：每期筆數 × 200 欄 × 平均每欄 bytes，另加每列 23 bytes tuple header + 約 25 bytes NULL bitmap + 對齊；NUMERIC 為變長，可能大於 8 bytes。
- 估算區間（十進位 GB）：每期 16～30GB；12 期 190～360GB。
- 峰值 = 現有全部分割 + 原始上傳檔 + staging + new_p + `_bak`（每期最多 2 份）+ `_deleted` + WAL 峰值 + 索引建置暫存 + 錯誤報表。
- 建議正式主機 **1TB NVMe**；備份放另一顆磁碟。
- 保護：磁碟使用率 > 80% 告警；匯入前檢查可用空間 ≥ 預估峰值 × 安全係數（初值 1.5，PoC 以量測值校正），不足即拒絕。
- PoC 以 `pg_total_relation_size` 量測每列實際大小與重傳峰值，取代上述估算。

## 5. 資料模型

### 5.1 中繼資料表（由 Alembic 管理）
| 表 | 主要欄位 |
|---|---|
| `meta_dataset` | dataset_id、實體表名、分割鍵（`yyyymm`）、說明、`schema_version` |
| `meta_column` | dataset_id、欄位代碼（實體名）、顯示名稱、別名、型別、必填、主鍵、驗算欄位、群組、排序、說明、停用、新增日期 |
| `load_log` | load_id、dataset、期別、模式（重傳/upsert/刪除）、upload_id、檔名、SHA-256、狀態、各階段時間、列數、拒收數、新增/更新/刪除數、操作人註記、IP、cleanup_status |
| `upload_session` | upload_id、區塊數、每塊 SHA-256、狀態、建立時間 |
| `saved_query` | 查詢 JSON、schema_version、建立者註記 |
| `audit_log` | 寫入類操作紀錄 |

### 5.2 實體資料表（由 adapter 依 metadata 產生 DDL）
- 每資料集一張寬表，依 `yyyymm` LIST 分割，不建立 default partition。
- 父表建立 `PRIMARY KEY`（必須包含 `yyyymm`；**[事實]** 分割表唯一限制必須包含分割鍵）。沒有主鍵的資料集只能整期重傳。
- 啟動時檢查 metadata 主鍵與 `pg_constraint` 一致，不一致即啟動失敗。
- `<table>_deleted`：與主表相同欄位 + `load_id`、操作人註記、刪除時間。
- 命名：實體識別字小寫英數底線、長度 ≤ 30；備份分割 `_bak_<yyyymm>_<load_id 前 8 碼>`（≤ 63 bytes）。
- 金額與精算數值用 `NUMERIC(p,s)`；文字欄位指定長度 `VARCHAR(n)`。

### 5.3 欄位維護
- 新增欄位是獨立操作（填型別、必填、說明），不在匯入流程中自動新增。
- 執行 `ALTER TABLE ADD COLUMN`（可為 NULL 或非 volatile 預設值；**[事實]** PG11+ 不重寫整表，但仍需強鎖，設 `lock_timeout`）；同一交易同步 ALTER `<table>_deleted`；`schema_version + 1`。
- 刪除欄位只停用不 DROP；改型別走「新增欄位 → 搬資料 → 停用舊欄」。
- 欄位維護取資料集層獨佔鎖，與任何期別寫入、備份互斥。

## 6. 鎖與並行控制

| 層級 | 實作 | 誰取得 |
|---|---|---|
| 資料集層 | `pg_advisory_lock(dataset_id)` | 欄位維護：**獨佔**；備份：**獨佔**（只擋寫入不擋查詢）；期別寫入：共享 |
| 期別層 | `pg_advisory_lock(dataset_id, yyyymm)` | 整期重傳、upsert、刪除、整期刪除：獨佔 |

- 取鎖順序固定：資料集層 → 期別層，避免死結。
- **[事實]** advisory shared lock 彼此不衝突，所以需要互斥的操作一律取獨佔。

## 7. 匯入流程

### 7.1 分塊上傳
- 每塊 16MB，`upload_id` + 區塊序號 + 每塊 SHA-256，可續傳；伺服器確認完整後組合。
- 單檔上限 50GB；支援 `.csv`、`.csv.gz`（串流解壓、不落地；解壓比 > 20 或超過上限即中止）。
- 上傳完成後檔案設唯讀並記錄 SHA-256；之後每一步都用 `upload_id` 讀同一檔，COPY 前重驗雜湊。
- 暫存目錄獨立於資料目錄；未完成上傳 24 小時後清除。

### 7.2 CSV 格式規則
- 只接受 UTF-8（可帶 BOM）、逗號分隔；其他編碼由使用者先轉換（Big5 轉碼列為後續版本）。
- 缺失值（依使用者規則）：未加引號空欄與 `""` → NULL；`NA`、`N/A` 為一般字串原樣保留。
- 含 NUL byte（0x00）→ 整檔拒絕（**[事實]** PostgreSQL text 不能存 NUL）。
- 欄位數不一致、引號未閉合 → COPY 失敗，整檔拒絕。

### 7.3 表頭驗證
- App 讀第一行（處理 BOM），以名稱對應 `meta_column` 的實體名或別名。
- 重複、空白、未知欄位、缺必填欄位 → 拒絕（未知欄位提示先到欄位維護新增）。
- 依表頭順序產生明確欄位清單：
  - 表頭即實體名：`COPY staging (c1, c2, …) FROM STDIN (FORMAT csv, HEADER MATCH, NULL '', FORCE_NULL (…))`
  - 使用別名：`HEADER true` + 已驗證的欄位清單
- **[事實]** `HEADER true` 只跳過首列，不會依名稱對應；**不可省略欄位清單**。

### 7.4 狀態機（整期重傳）
| 狀態 | 動作 | 交易 |
|---|---|---|
| UPLOADED | 上傳完成、雜湊已記錄 | — |
| STAGED | 建立該次匯入專用 `UNLOGGED` staging（全 TEXT），COPY 串流寫入（8MB 區塊） | 獨立交易 |
| VALIDATED | SQL 檢查型別、必填、主鍵重複；產生錯誤列 CSV（保留 7 天）。**任何錯誤列即整檔拒絕**，不部分提交 | 唯讀 |
| BUILT | 交易外：`CREATE TABLE new_p (LIKE parent INCLUDING DEFAULTS INCLUDING CONSTRAINTS)` → `INSERT … SELECT`（`NULLIF`、型別轉換）→ 建立與父表每個分割索引同定義的索引（含主鍵唯一索引）→ 加上與分割界限相同的 `CHECK` → `ANALYZE`；比對 `pg_attribute` 欄位與 `schema_version` | 多個交易 |
| SWAPPED | **唯一提交點**：`SET LOCAL lock_timeout='3s'` → 再次比對 schema → DETACH 舊分割 → RENAME 為 `_bak_…` → ATTACH new_p（有 CHECK 不掃描、索引直接掛接）→ 刪除多餘 CHECK → `load_log` 狀態 = SWAPPED | 單一短交易 |
| CLEANED | DROP staging；刪除上傳檔；`_bak` 保留 7 天、同期最多 2 份，逾期由排程 DROP | 獨立 |

- SWAP 取鎖失敗：回滾、指數退避重試；**重試總時間上限 2 分鐘**；仍失敗則告警，畫面顯示「該資料集有長查詢佔用」，由操作人選擇稍後重試或取消該資料集的唯讀查詢。
- **[事實]** DETACH（非 CONCURRENTLY）需父表 ACCESS EXCLUSIVE 鎖，會等待執行中的查詢；`lock_timeout` 只限制等待時間，所以排在後面的新查詢每次最多被擋 3 秒。不用 `DETACH CONCURRENTLY`（**[事實]** 不能在交易區塊內，無法與 ATTACH 組成原子切換）。
- 切換由背景工作執行，不依附 HTTP 請求；前端輪詢 `load_log`。以 `load_id` 冪等：已是 SWAPPED 就不重做。
- 啟動 / crash 恢復：狀態 < SWAPPED 的工作清掉中間產物（staging、new_p），依 `upload_id` 與 SHA-256 從 STAGED 重跑（**[事實]** UNLOGGED 表在 crash 後會被清空）；CLEANED 失敗依 `cleanup_status` 重試並告警。
- 查詢可見性：查詢只會看到切換前或切換後的完整資料（交易快照）。

### 7.5 匯入驗算
- 列數：CSV 記錄數 = staging 列數 = new_p 列數（扣除拒收）
- 主鍵數 = 列數
- metadata 標記的驗算欄位：staging（轉型後）SUM = new_p SUM
- 原始檔 SHA-256（偵測重複上傳）

### 7.6 錯誤定位
- COPY 失敗時保存 PostgreSQL 錯誤訊息與 CONTEXT，另以 Python `csv` 模組診斷掃描同一檔，回報「CSV 記錄序號」與「實體行號」（引號內換行時兩者不同）；只在失敗時執行。

## 8. 增修與刪除

### 8.1 增修（upsert）
- staging 驗證後 `INSERT … ON CONFLICT (pk) DO UPDATE`；**不用 MERGE**（**[事實]** MERGE 在併發插入時可能直接出現唯一鍵錯誤，語義不同）。
- ON CONFLICT 欄位由 metadata 主鍵定義產生。
- staging 主鍵重複、主鍵 NULL、期別不存在 → 整檔拒絕。
- 主鍵含期別，期別不同視為不同列，不會跨分割搬移。
- 記錄新增 / 更新筆數。

### 8.2 依主鍵刪除
1. 上傳刪除清單 CSV（只含主鍵）→ staging；重複鍵、NULL 鍵、不存在期別 → 拒絕。
2. 預覽「符合 X 筆、不存在 Y 筆（列出主鍵）」。
3. 填操作人姓名 + 二次確認；超過該期筆數 10% 再確認一次，並建議改用整期重傳。
4. 單一陳述式執行（任一失敗整筆回滾）：
   ```sql
   WITH d AS (
     DELETE FROM t USING del_keys k
     WHERE <主鍵相等條件>
     RETURNING t.<明列欄位…>
   )
   INSERT INTO t_deleted (<明列欄位…>, load_id, operator_note, deleted_at)
   SELECT <明列欄位…>, :load_id, :operator, now() FROM d;
   ```
5. 實際刪除數 ≠ 預覽符合數 → ROLLBACK。
6. `_deleted` 保留 N 天（預設 7），可依 `load_id` 還原；排程清除也記錄。

### 8.3 整期刪除
- DETACH 並改名 `_bak_…`，保留 7 天內可重新 ATTACH。

### 8.4 維護
- 大量增修或刪除後，對受影響分割執行 `VACUUM (ANALYZE)`；autovacuum 調積極（200 欄寬列的更新膨脹較明顯）。

## 9. 查詢

### 9.1 查詢規格（JSON）
```json
{"from":"policy",
 "joins":[{"table":"claim","type":"left","on":[["policy_no","policy_no"]]}],
 "select":["policy.policy_no","claim.amt"],
 "calc":[{"name":"net","expr":"claim.amt - claim.deduct"}],
 "where":[["policy.yyyymm","=","202610"]],
 "order":[["net","desc","nulls_last"]]}
```
- 存成「我的查詢」時帶 `schema_version`；載入時若引用的欄位已停用，提示失效欄位。

### 9.2 篩選與 join
- 大表（> 1,000 萬筆）**必須**帶分割鍵條件（期別），確保分割裁剪。
- 篩選運算：`= / IN / BETWEEN / LIKE 'xx%' / IS NULL`，一律參數綁定；IN 可貼 Excel 整欄。
- join 前檢查 join 鍵唯一性，多對多需使用者確認。
- 執行前 `EXPLAIN`（不執行）估算，過大或全表掃描只作**提示**，不作硬性上限。

### 9.3 自訂欄位運算式
- Python `ast.parse(mode='eval')`，**只接受**：
  - `Expression`、`BinOp`（Add/Sub/Mult/Div）、`UnaryOp`（USub/UAdd）
  - `Constant`：數值（int / float / Decimal 字串），長度 ≤ 30
  - `Name`：必須解析成 `meta_column` 的欄位 ID
  - `Call`：只限 `ROUND(x[, n])`（n 為 0～10 的整數常數）、`ABS(x)`、`COALESCE(x, y[, …])`（≤ 5 個參數），不接受 keywords
- 其餘節點（Attribute、Subscript、Compare、BoolOp、Lambda、字串常數…）一律拒絕，錯誤訊息指出被拒的節點型別。
- 限制：節點數 ≤ 200、深度 ≤ 20、運算式長度 ≤ 1,000 字。
- 轉成 SQLAlchemy 表達式物件，不拼字串。除法一律 `CAST(a AS NUMERIC) / NULLIF(b, 0)`（**[事實]** PostgreSQL 整數除法 5/2=2，Oracle 為 2.5）。

### 9.4 預覽與顯示
- 預覽前 1,000 筆、伺服器端分頁；最多 50 欄；總筆數另按鈕查詢。
- 欄位挑選：可搜尋、依群組勾選、儲存常用欄位組合（AG Grid 欄位虛擬化）。

### 9.5 資源保護
| 項目 | 設定 |
|---|---|
| 連線池 | `psycopg_pool` max 20（查詢 12、匯入 2、匯出 4、系統 2） |
| 大查詢併發 | semaphore 4，其餘排隊並顯示序號 |
| 匯出併發 | semaphore 2 |
| 每使用者 | 每瀏覽器 session（cookie）同時 1 查詢 + 1 匯出；每 IP 上限 3（避免 NAT 下多人被合併） |
| 逾時 | 查詢 `statement_timeout` 300 秒；匯出 1,800 秒 |
| 取消 | 客戶端斷線即 `conn.cancel()`；取消 API 以伺服器端 job_id 對應 backend PID，不接受客戶端指定 PID |
| 角色 | 查詢一律用唯讀角色 `app_ro` |

### 9.6 查詢調校順序（依 PoC 結果逐步採用）
1. 強制期別條件（分割裁剪）+ 主鍵索引
2. 平行查詢與 `work_mem` 調整
3. 依 PoC 實際查詢與 `EXPLAIN ANALYZE` 增加索引（每資料集上限 5 個），必要時用 `INCLUDE` 覆蓋索引
4. 常用 join / 彙總做成實體表或 materialized view，匯入或增修後自動重算
5. 垂直拆表（常用欄 / 其餘欄，主鍵 1:1）— 維護成本高，最後手段
6. PostgreSQL 欄式擴充（Citus columnar、pg_duckdb）— **[待查證]** UPDATE/DELETE 支援與成熟度，目前不採用

## 10. 匯出與複製

| 方式 | 上限 | 做法 |
|---|---|---|
| 匯出 CSV | 不限 | `COPY (SELECT…) TO STDOUT (FORMAT csv, HEADER)` → `StreamingResponse`，可選 gzip；預設 UTF-8 BOM；可選 Big5（無法表示的字元整筆失敗並回報）；斷線即取消查詢；畫面先提示預估大小 |
| 複製貼上 | 先定 1 萬筆或 10MB，PoC 於 Chrome / Edge / Safari 實測後調整 | 後端回 TSV；欄值含 tab、換行或雙引號時以雙引號包住並將 `"` 轉為 `""`（Excel 相容）；`navigator.clipboard.writeText`（**[事實]** 需 HTTPS 或 localhost，瀏覽器可能要求使用者操作）；失敗時顯示可全選的 textarea 讓使用者 Ctrl+C |

- 提醒：**[事實]** Excel 單一工作表上限 1,048,576 列。

## 11. 無權限環境下的保護（風險接受）

使用者已決定不做登入與權限。風險接受的前提：
1. 系統只放在可信任內網，**不得對外**。
2. 寫入類 API（上傳、刪除、新增欄位、重傳、還原）套用 Caddy IP allowlist（預設全內網，可縮小）。
3. 姓名與 IP 只是「操作註記」，不是身分稽核；無法做個人責任歸屬。
4. 寫入操作需二次確認；所有寫入保留可回復的備份（`_bak`、`_deleted`）。
5. 查詢一律走唯讀角色。

**[待確認]** 是否加一組共用「管理密碼」保護寫入操作（可降低誤操作與非預期寫入，但不能辨識個人）。

## 12. 備份與維運

- 每週 `pg_basebackup` 到另一顆磁碟；每次成功匯入後 `pg_dump` 該分割（custom format）。
- 備份取資料集層獨佔鎖（與寫入互斥，不影響查詢），排在離峰。
- 每季還原演練。
- Docker / PostgreSQL 大版本升級前先完整備份並在 Mac 演練。

## 13. 跨資料庫設計（DB Adapter）

### 13.1 分層
- Service 層不得出現資料庫專屬語法；PostgreSQL 專屬語法集中在 `db/`。
- 查詢（SELECT / JOIN / 運算式 / 分頁）交給 SQLAlchemy Core 依 dialect 產生。
- Alembic 只管理系統中繼資料表；資料集實體表 DDL 由 adapter 依 metadata 產生。

### 13.2 Adapter 介面
`bulk_load`、`bulk_export`、`create_table`、`add_column`、`swap_period`、`backup_period`、`drop_period`、`upsert`、`delete_by_keys`、`set_timeout`、`explain_estimate`、`type_map`

### 13.3 相容性
- 一套與資料庫無關的相容性測試（匯入、增修、刪除、join、運算、匯出結果比對）；每個 adapter 都要通過。
- 相容性矩陣（指定目標產品與版本後逐項查證）：

| 功能 | PostgreSQL 17 | Greenplum | Oracle | SAP IQ |
|---|---|---|---|---|
| 批次匯入 | COPY | COPY / gpfdist **[待查證]** | SQL*Loader direct path / 外部表 / array binding **[待查證]** | LOAD TABLE **[待查證]** |
| 批次匯出 | COPY TO | **[待查證]** | 大 arraysize fetch 串流 | **[待查證]** |
| upsert | ON CONFLICT | 依版本 **[待查證]** | MERGE | **[待查證]** |
| 分割交換 | ATTACH / DETACH | 依版本 **[待查證]** | EXCHANGE PARTITION（**[事實]** Partitioning 為 EE 另計價選項） | **[待查證]** |
| 交易式 DDL | 有 | **[待查證]** | 無（DDL 隱含 commit） | **[待查證]** |
| 逾時 | statement_timeout | **[待查證]** | call_timeout / Resource Manager | **[待查證]** |

### 13.4 從 v1 就遵守的規則
| 規則 | 原因 |
|---|---|
| 除法先轉 NUMERIC 並 `NULLIF(b,0)` | 整數除法結果不同 |
| 空字串一律存 NULL | **[事實]** Oracle 視 `''` 為 NULL；與使用者缺失值規則一致 |
| 識別字小寫英數底線、≤ 30 字 | Oracle 不加引號轉大寫、舊版長度上限 30 |
| 文字欄位用 `VARCHAR(n)` | Oracle VARCHAR2 上限、CLOB 效能差 |
| 排序明寫 `NULLS FIRST/LAST` | 各家預設可能不同 |
| 不用 JSONB、陣列、`ILIKE`、`::` 轉型 | 無法移植 |

## 14. 驗收條件（每條斷言「通過的理由」，不只看結果狀態）

| # | 條件 | 驗證 |
|---|---|---|
| AC1 | 匯入速度：psycopg 3 COPY 與 psql `\copy` 差距 ≤ 10% | 同一檔各跑 3 次取中位數，以 MB/s 比較；另測 JDBC CopyManager、JDBC batch |
| AC2 | 匯出速度：同上 | `COPY TO` |
| AC3 | 記憶體：App RSS 不隨檔案大小成長 | 1GB 與 10GB 檔比較峰值 |
| AC4 | 欄位對應：CSV 欄序與資料表不同時，每個值落在正確欄位 | 斷言抽樣欄值；未知 / 重複表頭斷言拒絕訊息 |
| AC5 | 原子性：任一狀態中途 kill，正式資料不變 | 斷言 `load_log=FAILED`，且舊分割列數與驗算欄位 SUM 與 kill 前相同 |
| AC6 | 冪等：SWAP 後中斷連線再重試不重做 | 斷言只交換一次 |
| AC7 | 缺失值規則 | 未加引號空欄、`""`、NA、N/A、含 tab、引號內換行 / 逗號、BOM、NUL byte 各自斷言結果 |
| AC8 | 刪除一致性 | `_deleted` 列數 = DELETE 回傳數 = 預覽數 |
| AC9 | 互斥：備份中發起匯入、BUILT 後新增欄位 | 前者斷言等待鎖；後者斷言 SWAP 前比對失敗並標 FAILED |
| AC10 | 查詢效能：1 億 × 1,000 萬 left join 加條件，預覽 ≤ 30 秒（Windows 實測） | 同時斷言 `EXPLAIN` 只掃描指定分割 |
| AC11 | 注入防護 | `; DROP`、子查詢、Attribute、Subscript、字串常數、未知函式全部拒絕，斷言錯誤訊息為白名單拒絕（指出節點型別），而非資料庫錯誤 |
| AC12 | 切換鎖定影響 | 有長查詢時執行 SWAP，量測新查詢等待時間 ≤ 3 秒 / 次、持鎖時間 |
| AC13 | 資源保護 | 20 人併發腳本：排隊正常、無 OOM；傳入任意 PID 的取消請求被拒 |

## 15. PoC 計畫

**Mac（功能與相對速度）**
1. `docker-compose.yml`（PostgreSQL 17 + FastAPI 骨架）與合成資料產生器（200 欄，不用任何公司資料）。
2. AC1～AC9、AC11 在 50 萬筆規模驗證。
3. 量測每列實際大小，校正第 4 節容量公式。

**Windows（效能驗收）**
4. 每期 1,000 萬筆、2～3 期資料：AC10、AC12、AC13。
5. 依結果決定第 9.6 節調校要用到第幾層。
6. 產出 PoC 報告。

## 16. 待確認事項

1. **[待確認]** 是否加共用管理密碼保護寫入操作（第 11 節）。
2. **[待確認]** 日後可能更換的資料庫是否包含 SAP IQ（影響第 13.4 節規則嚴格度）。
3. **[待確認]** 開始 PoC（會新增 psycopg 3、FastAPI、SQLAlchemy、Alembic 等依賴）。

## 附錄 A：版本紀錄

| 版本 | 變更 |
|---|---|
| v0.1 | 初版：PostgreSQL + FastAPI + COPY；回答 psql 替代與 JDBC |
| v0.2 | 本機 Docker、20 人無權限、整期重傳 / 增修 / 刪除、join 強制篩選 |
| v0.3 | 32GB 主機設定、200 欄影響、可回復的刪除流程 |
| v0.4 | 確定只存 PostgreSQL，移除 DuckDB 方案，訂調校順序 |
| v0.5 | 每期 1,000 萬筆 × 12 期容量估算；Mac 測試 / Windows 驗收分工 |
| v0.6 | 可換資料庫產品：DB Adapter 與跨 DB 規則 |
| v0.7 | Codex 第 1 輪：表頭對應、狀態機、ON CONFLICT、AST 白名單、容量峰值、刪除 CTE、資源保護等 |
| v0.8 | Codex 第 2、3 輪：預建索引與 lock_timeout、兩層鎖、父表主鍵、取消 sentinel、crash 恢復、TOCTOU、備份獨佔鎖、驗收斷言理由 |
| v0.8.1 | 部署變更（使用者決定，2026-11-03）：PostgreSQL 不再放 Docker，改連自行安裝的 server；Compose 只含 app、proxy；依賴改 requirements.txt＋pip 離線安裝，移除 uv |

## 附錄 B：Codex 審查摘要

| 輪次 | 判定 | 高嚴重度 | 處理 |
|---|---|---|---|
| 1 | REVISE | 7 | 5 採納、2 部分採納（無登入：使用者決定、改為風險接受；Mac PoC 規模：改為 50 萬筆預設），Codex 第 2 輪接受 |
| 2 | REVISE | 3 | 全部採納 |
| 3 | REVISE | 1（備份與寫入共享鎖無法互斥） | 採納；Codex 表示修正後無其他高嚴重度問題 |

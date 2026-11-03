# PoC 後端實作說明（對應 SPEC v0.8）

- 範圍：SPEC 第 15 節「Mac（功能與相對速度）」階段的後端。Vue 3 + AG Grid 前端、Windows 效能驗收（AC10、AC12、AC13 壓測）不在本次範圍。
- 版本：PostgreSQL 17、Python 3.12+、psycopg 3.3.6、psycopg-pool 3.3.3、SQLAlchemy 2.1.6、FastAPI 0.162、Alembic 1.20。

## 1. 執行方式

PostgreSQL 自行安裝（不在 Docker 內），詳細步驟見 [infra.md](infra.md)。

```sh
psql -h 127.0.0.1 -U postgres -d postgres -f sql/setup_roles.sql   # 建 cqp 庫與 app_rw／app_ro（提示輸入密碼）
psql -h 127.0.0.1 -U postgres -d postgres -v dbname=cqp_test -f sql/setup_roles.sql   # pytest 專用測試庫
cp .env.example .env                                                 # 填入 CQP_DSN_RW／CQP_DSN_RO、CQP_TEST_DSN／CQP_TEST_DSN_RO
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python tools/withenv.py -- .venv/bin/python -m pytest -q   # 含資料庫整合測試（只連 cqp_test）
# 完整服務（Caddy HTTPS → app → 外部 PostgreSQL）；需先準備 wheels/ 與 npm-cache/（infra.md「離線安裝套件」）
docker compose up -d --build
```

app 容器啟動時先執行 `alembic upgrade head` 建立中繼資料表。

## 2. 模組

| 路徑 | 內容 |
|---|---|
| `app/catalog.py` | DatasetDef / ColumnDef、識別字與型別規則 |
| `app/db/meta.py`、`migrations/` | 中繼資料表（Alembic） |
| `app/db/adapters/postgres.py` | 所有 PostgreSQL 專屬 SQL（SPEC 13.1） |
| `app/services/importer.py` | 匯入狀態機、增修、依鍵刪除、整期刪除、crash 恢復、_bak 清理 |
| `app/services/csvfile.py`、`upload.py` | 表頭對應、BOM／NUL／gzip、分塊上傳 |
| `app/services/query/` | JSON 查詢規格、運算式 AST 白名單、執行與資源保護 |
| `app/services/export.py` | COPY TO STDOUT 串流、TSV 複製格式 |
| `app/main.py`、`app/services/jobs.py` | API、背景工作 |

## 3. 驗收條件對照（Mac、小量）

| AC | 狀態 | 測試／證據 |
|---|---|---|
| AC1／AC2 | 腳本完成，小量實測 | `bench/ac1_ac2.py`；2 萬筆 × 200 欄：psycopg COPY 100.4 MB/s、psql `\copy` 88.8 MB/s（路徑不同，見 docs/infra.md）。正式比較需 50 萬筆以上 |
| AC3 | 未量測 | 程式為串流設計（COPY 分塊、gzip 串流），未做 1GB／10GB RSS 實測 |
| AC4 | 通過 | `test_ac4_*`：表頭打亂＋別名、HEADER MATCH、未知／重複／空白／缺必填 |
| AC5 | 通過 | `test_ac5_*`：STAGED／VALIDATED／BUILT 注入失敗、SWAP 交易內 DETACH 後失敗 → 回滾；`test_crash_recovery_resumes` |
| AC6 | 通過 | `test_ac6_swap_not_repeated` |
| AC7 | 通過 | `test_ac7_*`：空欄、`""`、NA、N/A、tab、引號內換行／逗號、BOM、NUL、欄位數不符、引號未閉合 |
| AC8 | 通過 | `test_ac8_*`：預覽數 = 刪除數 = _deleted 列數；預覽後資料變動 → 回滾 |
| AC9 | 通過 | `test_ac9_*`：備份鎖下 SWAP 等鎖逾時並保留 BUILT；BUILT 後新增欄位 → SWAP 前比對失敗 |
| AC10 | 只驗證裁剪 | `test_left_join_prunes_right_partitions_and_keeps_rows`（EXPLAIN 只出現指定期別）；效能需 Windows |
| AC11 | 通過 | `tests/test_expr.py`、`test_query_spec.py` |
| AC12、AC13 | 未做 | 需 Windows 壓測；取消 API 只接受 job_id 已有測試 |

端對端（經 Caddy HTTPS）：5 萬筆 × 200 欄（119.8MB）分塊上傳＋匯入 8.4 秒、驗算 SUM 一致；join＋計算欄預覽 0.03 秒；匯出串流正常；注入被白名單拒絕。

## 4. 與規格不同或規格未寫明之處（需確認）

1. **最低 PostgreSQL 版本改為 16**（規格寫 15）：型別驗證用 `pg_input_is_valid()`（16+），逐列安全判斷能否轉型。
2. **numeric 小數位數超過定義即拒絕**（不四捨五入）；不接受科學記號；日期只接受 `YYYY-MM-DD`。
3. **匯入過程不全程持鎖**：同一期別同時只允許一筆進行中工作（`load_log` partial unique index）；兩層鎖只在 SWAP／增修／刪除的提交交易內取得。這是 AC9「BUILT 後新增欄位」能發生的前提。
4. **一次匯入只處理一個期別**：檔案內期別必須等於指定期別；檔案沒有期別欄時以指定期別補值。
5. **LEFT JOIN 與強制期別條件**：右表的期別條件若寫在 where，會把 LEFT JOIN 變成 INNER JOIN。實作允許經由 `on` 的「分割鍵相等」取得期別（inner 雙向、left 只由左到右），已用 EXPLAIN 確認右表分割裁剪仍成立。
6. **運算式 `claim.amt`**：規格白名單拒絕 Attribute，但範例用了 `表.欄`。實作只接受「Name.欄」這一層 Attribute 當欄位參照。
7. **join 唯一性檢查**：以 metadata 主鍵判斷（兩側 join 鍵都沒涵蓋各自主鍵 → 多對多需確認），不實際查資料。
8. **新增欄位一律可為 NULL**：必填只約束之後的匯入驗證。
9. **停用欄位也會讓 schema_version + 1**：進行中的匯入會在 SWAP 前失敗（保守）。
10. **AC5 解讀**：「中途 kill → FAILED」實作為：例外 → FAILED 並清掉中間產物；程序死亡 → recover 依狀態接手（BUILT 直接重試切換，其餘從頭重跑，最多 3 次）。
11. **Big5 匯出**中途遇到無法表示的字元時，已送出的部分無法撤回，客戶端會收到不完整檔案（HTTP 串流限制）。
12. **upsert 的新增／更新筆數**：分割表不支援 `RETURNING xmax`，改為提交交易內先計算既有鍵數（持有期別獨佔鎖，期間無其他寫入）。

## 5. 提交前審查（/code-review medium ＋ code-reviewer agent）

兩份審查共 22 項（去除重複），全部判斷成立並修正，每項都有回歸測試（`tests/test_review_regressions*.py`）：

| 嚴重度 | 問題 | 修正 |
|---|---|---|
| 高 | 多成員 gzip 只讀第一段、截斷 gzip 靜默接受 → 整期資料被不完整檔案取代且驗算擋不住 | 依序解每個成員；結尾未正常結束即拒絕 |
| 高 | 新分割主鍵用一般 UNIQUE INDEX，ATTACH 不認而在提交交易內重建主鍵索引 | 父表限制索引改用 `ADD CONSTRAINT` 預建；測試斷言分割只有 1 個索引且已掛為父表主鍵子索引 |
| 高 | 錯誤報表 `concat_ws` 200 欄超過函式 100 參數上限 | 改 `array_to_string(ARRAY[...])`；120 欄實測 |
| 高 | Caddy 寫入防護漏了 `POST /api/datasets` | 改為寫入路徑前綴 ＋ 非 GET/HEAD；curl 實測 13 條路徑 |
| 高 | 匯出 generator 未開始就被丟棄時，連線與名額永久遺留 | `ExportStream` 物件持有資源，close()／回收時釋放 |
| 中 | 還原時連衝突略過的 `_deleted` 備份一起刪除 | 只刪 `RETURNING` 實際還原的主鍵 |
| 中 | 刪除清單在有必填非主鍵欄位時一定被拒 | 刪除模式不檢查非主鍵必填 |
| 中 | staging 中途 crash 嘗試次數不增加 → 無限重試 | 開始 staging 前先累加 |
| 中 | 欄位維護取獨佔鎖前未設 lock_timeout | 先設 5s，逾時回 409 |
| 中 | upsert／刪除目標表沒有期別常數 → 可能掃 12 期 | 加期別常數條件、staging ANALYZE；EXPLAIN 實測只出現單一分割 |
| 中 | 計算欄位接受文字／日期欄 | 只接受數值欄 |
| 中 | 小表查詢也佔大查詢名額、每次重估所有表筆數 | 只有大表佔名額；估計值快取 60 秒 |
| 中 | 上傳磁碟檢查 1.5 倍低於組檔峰值 2 倍 | 改 2.1 倍 |
| 低 | 錯誤報表非暫存檔＋rename、join 右欄 `表.欄` 判斷錯、FOR UPDATE 鎖錯誤未轉換、刪除確認遇鎖即 FAILED、`$` 接受換行、唯讀暫存檔殘留、同一上傳重複使用、IN 參數總數、別名含 `|`、停用欄位取鎖後未重讀、資料集定義格式錯誤回 500 | 皆已修正 |

## 6. 尚待處理（未修，已知）

- `BUILT` ＋ 等鎖逾時的工作沒有到期機制，會一直佔著該期別與一張新分割表；需要操作人重試或取消。
- `check_joins` 只看左側為 from 表的鍵，三表以上鏈式 join 可能誤判多對多（偏保守，會要求確認）。
- Big5 匯出使用 Python `big5`；Windows Excel 常見的 `cp950` 字元較多，是否改用需確認。
- Docker Desktop 的 port 轉送可能讓 Caddy 看到的來源都是閘道 IP（落在 private_ranges）→ allowlist 形同全開、每 IP 名額變成全體共用。需在 Windows 正式主機實測 `load_log.client_ip`。

## 7. 未完成

- 前端（Vue 3 + AG Grid）。
- 備份（`pg_basebackup`、分割 `pg_dump`）只有鎖的實作（`session_lock_dataset_exclusive`），排程與指令未實作。
- 匯入前磁碟空間檢查只檢查上傳目錄，無法從 app 容器得知資料庫 volume 可用空間。
- 磁碟使用率 > 80% 告警、`_bak` 重新 ATTACH（整期刪除還原）API。
- 待確認事項（SPEC 16）：共用管理密碼、SAP IQ。

# 基礎設施使用說明（PoC）

## 檔案

| 路徑 | 用途 |
|---|---|
| `docker/docker-compose.yml` | `db`（postgres:17）、`app`、`proxy`（Caddy）；專案名 `cqp` |
| `docker/docker-compose.dev.yml` | 覆蓋檔：db 對應 `127.0.0.1:55432`（只綁本機），用於本機開發／測試 |
| `docker/Dockerfile` | python:3.13-slim + uv，`uv sync --frozen --no-dev`，非 root 執行 |
| `docker/Caddyfile` | `tls internal`、寫入類路徑 IP allowlist、`header_up X-Forwarded-For {remote_host}` |
| `docker/initdb/10-roles.sh` | 首次初始化建立 `cqp` 庫、`app_rw`、`app_ro` |
| `env/mac.env.example`、`env/win.env.example` | 參數範本（複製成 `env/mac.env`／`env/win.env` 並填密碼；已被 .gitignore） |
| `tools/make_local_env.py` | 由 mac 範本產生 `env/local.env`（隨機密碼，記憶體再調小） |
| `tools/verify_db.py` | 驗證角色權限與 PG 參數 |
| `tools/gen_data.py` | 合成資料產生器 |
| `tools/fixtures.py`、`tools/check_fixtures_copy.py` | AC7 邊界 CSV 與其 COPY 層核對 |
| `bench/ac1_ac2.py`、`bench/JdbcBench.java` | AC1／AC2 匯入匯出速度比較 |

## 啟動

```bash
python tools/make_local_env.py                       # 或自行複製 env/mac.env.example → env/mac.env
# 只啟動 db（本機開發／測試）
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml --env-file env/local.env up -d db
# 全部（Mac / Windows）
docker compose -f docker/docker-compose.yml --env-file env/mac.env up -d --build
```

- 連線（本機開發）：`postgresql://app_rw:<密碼>@127.0.0.1:55432/cqp`、`postgresql://app_ro:<密碼>@127.0.0.1:55432/cqp`，密碼見 `env/local.env`。
- 對外只有 proxy 的 443；db、app 不對外開 port。使用者連線網址必須與 `CQP_HOST` 一致（Caddy 以它簽發內部憑證）。
- initdb 只在 **資料目錄為空**（第一次建立 volume）時執行。改了 `initdb/` 或密碼，需 `docker compose ... down -v` 重建（會清掉資料）。
- `10-roles.sh` 必須有執行權限（`chmod +x`），且在 Windows 要用 LF 換行；否則容器內會報 `bad interpreter`（實測：缺執行權限時初始化失敗、角色未建立）。建議在 repo 根目錄加 `.gitattributes`：`*.sh text eol=lf`。

## 權限設計（initdb）

- `app_rw`：`cqp` 庫與 `public` schema 的 owner，可建表。`app_ro`：只有 `USAGE` on schema 與 `SELECT`。
- `ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public GRANT SELECT ON TABLES TO app_ro`：app_rw 日後建的表，app_ro 自動可讀（PG15+ public schema 預設不給 CREATE，這裡明確交給 app_rw）。
- `app_ro` 另設 `default_transaction_read_only = on`，但這只是預設值（連線後可 `SET` 關掉），真正的防線是權限；`tools/verify_db.py` 兩者都驗證。
- 注意：default privileges 只對「之後」建立的表生效；若 owner 不是 app_rw（例如以 postgres 建表），app_ro 讀不到。

## Caddy 寫入 allowlist

寫入類路徑（`/api/upload*`、`/api/datasets/*/columns*`、`/api/loads*`、`/api/delete*`）只允許 `CQP_WRITE_ALLOW`（預設 `private_ranges`，也可填空白分隔的 CIDR）來源；其餘來源回 403。不設 `trusted_proxies`，`X-Forwarded-For` 由 Caddy 以實際連線 IP 覆寫。
注意：Docker Desktop（Mac／Windows）的 port 轉送可能讓 Caddy 看到的來源 IP 是 Docker 閘道 IP（私有網段），而不是真實用戶端 IP，allowlist 在這種部署下實際上形同全放行；Windows 正式環境需實測 `remote_host` 看到的是什麼（建議 PoC 時用 `docker logs` 檢查）。

## 合成資料

```bash
.venv/bin/python tools/gen_data.py --rows 50000 --out data/policy_202610.csv --def-out data/policy.json
.venv/bin/python tools/gen_data.py --dataset claim --policy-rows 50000 --rows 20000 --out data/claim_202610.csv
.venv/bin/python tools/gen_data.py --rows 50000 --shuffle-columns --gzip --out data/policy_shuf.csv.gz   # AC4
```

- 參數：`--rows --period --seed --null-rate --shuffle-columns --gzip --out --def-out --cols --name`；同 seed 與參數會得到相同內容。
- 主鍵：policy 為 (`yyyymm`, `policy_no`)，`policy_no = P` + 10 位流水號，期別內唯一；claim 為 (`yyyymm`, `policy_no`, `claim_no`)，`policy_no` 取自 policy 的前 `--policy-rows` 筆。
- 數值含負數與 NULL（未加引號空欄，非必填欄依 `--null-rate`）。整個產生過程逐列串流寫出，並先寫 `.part` 再 rename。
- 定義 JSON 已用 `app.catalog.DatasetDef.validate()` 驗證通過（200 欄、3 個 checksum 欄）。

### 檔案大小

- **[實測]** 5 萬筆 × 200 欄 = 約 119.8 MB（每列約 2.4 KB）。
- **[推論]** 50 萬筆 × 200 欄 ≈ 1.2 GB（線性外推；未實際產生，因本機磁碟僅剩約 16GB）；gzip -6 實測 5 萬筆為 64.2 MB（約 54%，隨機資料壓縮率低），外推 50 萬筆約 0.64 GB（推論）。真實資料的壓縮率會不同。
- 資料庫內的實際大小需匯入後以 `pg_total_relation_size` 量測，用來校正 SPEC 第 4 節容量公式。

## Bench（AC1／AC2）

```bash
.venv/bin/python bench/ac1_ac2.py --rows 20000
```

偏差說明：psql 經 `docker exec` 在容器內執行（每次含固定啟動成本，腳本另量基線並輸出「扣基線」欄），且走容器內 loopback；psycopg／JDBC 在主機經 Docker Desktop 的 port 轉送。要驗收 AC1 的「≤10%」，需把比較雙方放在同一網路位置、使用 GB 級檔案。JDBC 使用 `~/.m2` 內既有的 pgjdbc jar 以 `java` 單檔原始碼啟動（本機沒有 mvn），找不到 jar 或 java 時自動跳過。

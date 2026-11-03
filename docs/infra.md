# 基礎設施使用說明（PoC）

## 檔案

| 路徑 | 用途 |
|---|---|
| `docker/docker-compose.yml` | `app`、`proxy`（Caddy）；專案自帶 `db`（postgres:17）需啟用 `bundled-db` profile |
| `docker/docker-compose.dev.yml` | 覆蓋檔：db 對應 `127.0.0.1:55432`（只綁本機），用於本機開發／測試 |
| `docker/Dockerfile` | python:3.13-slim + uv，`uv sync --frozen --no-dev`，非 root 執行 |
| `docker/Caddyfile` | `tls internal`、寫入類路徑 IP allowlist、`header_up X-Forwarded-For {remote_host}` |
| `docker/initdb/10-roles.sh` | 首次初始化建立 `cqp` 庫、`app_rw`、`app_ro` |
| `env/mac.env.example`、`env/win.env.example` | 專案自帶 DB 的參數範本 |
| `env/external.env.example` | 連接既有 PostgreSQL server 的範本（複製成 `env/external.env`；已被 .gitignore） |
| `tools/make_local_env.py` | 由 mac 範本產生 `env/local.env`（隨機密碼，記憶體再調小） |
| `tools/verify_db.py` | 驗證角色權限與 PG 參數 |
| `tools/gen_data.py` | 合成資料產生器 |
| `tools/fixtures.py`、`tools/check_fixtures_copy.py` | AC7 邊界 CSV 與其 COPY 層核對 |
| `bench/ac1_ac2.py`、`bench/JdbcBench.java` | AC1／AC2 匯入匯出速度比較 |

## 啟動

```bash
python tools/make_local_env.py                       # 或自行複製 env/mac.env.example → env/mac.env
# 只啟動 db（本機開發／測試）
docker compose --profile bundled-db -f docker/docker-compose.yml -f docker/docker-compose.dev.yml --env-file env/local.env up -d db
# 全部（使用專案自帶 DB，Mac / Windows）
docker compose --profile bundled-db -f docker/docker-compose.yml --env-file env/mac.env up -d --build
# 全部（使用既有 PostgreSQL server）
docker compose -f docker/docker-compose.yml --env-file env/external.env up -d --build
```

- 預設部署不啟動專案自帶 DB。`CQP_DSN_RW`、`CQP_DSN_RO` 必須指向同一個既有資料庫；查詢／匯出以唯讀帳號連線，匯入／系統操作以讀寫帳號連線。URI 密碼中的特殊字元須 percent-encode。若 DB 和 app 在同一台主機的 Docker Desktop，通常以 `host.docker.internal` 作為 DB 主機名稱。
- 連線池上限預設每個 app worker 合計 20 條（查詢 12、匯出 4、匯入 2、系統 2）；`CQP_WORKERS=2` 時，DB 最多可能看到 40 條 app 連線。實際連線採用時才建立，但設定既有 server 的連線上限時，請預留此容量，或調低 `CQP_POOL_QUERY`、`CQP_POOL_EXPORT`、`CQP_POOL_IMPORT`、`CQP_POOL_SYSTEM`。
- 專案自帶 DB 僅供本機／PoC；需明確啟用 `--profile bundled-db`。本機開發連線為 `postgresql://app_rw:<密碼>@127.0.0.1:55432/cqp`、`postgresql://app_ro:<密碼>@127.0.0.1:55432/cqp`，密碼見 `env/local.env`。
- 既有 PostgreSQL 必須為 **16 以上**。容器啟動命令會先以 `CQP_DSN_RW` 執行 `alembic upgrade head`，建立／更新平台中繼資料表；資料庫須預先建立，讀寫帳號需有 `public` schema 的建立物件權限。
- 建議使用專用資料庫 `cqp` 與專用角色，不要沿用超級使用者。以 DBA 身分先建立角色和資料庫（替換密碼；`CREATE DATABASE` 請單獨執行）：

  ```sql
  CREATE ROLE app_rw LOGIN PASSWORD '請換成強密碼';
  CREATE ROLE app_ro LOGIN PASSWORD '請換成強密碼';
  ALTER ROLE app_ro SET default_transaction_read_only = on;
  CREATE DATABASE cqp OWNER app_rw;
  ```

  連到 `cqp` 資料庫後再設定 schema 與未來資料表的讀取權限：

  ```sql
  ALTER SCHEMA public OWNER TO app_rw;
  REVOKE ALL ON SCHEMA public FROM PUBLIC;
  GRANT USAGE, CREATE ON SCHEMA public TO app_rw;
  GRANT USAGE ON SCHEMA public TO app_ro;
  ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public
    GRANT SELECT ON TABLES TO app_ro;
  ```

- 若要使用內建 DB，將 `env/mac.env.example` 複製為 `env/mac.env` 並填入密碼，再加上 `--profile bundled-db`；未啟用 profile 時，Compose 不啟動 DB 容器。
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

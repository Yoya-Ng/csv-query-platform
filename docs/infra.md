# 基礎設施使用說明（PoC）

PostgreSQL 不放在 Docker 內：自行安裝 PostgreSQL server（本機或其他主機），平台以 `.env` 中的連線字串連線。Docker 只跑 `app`（FastAPI）與 `proxy`（Caddy）。

## 檔案

| 路徑 | 用途 |
|---|---|
| `docker-compose.yml` | `app`、`proxy`（Caddy）；自動讀取同目錄的 `.env` |
| `.env.example` | 連線設定範本（複製為 `.env`；`.env` 已被 .gitignore 與 .dockerignore 排除） |
| `docker/Dockerfile` | 前端 `npm ci && npm run build`；後端 python:3.13-slim，`pip install --no-index --find-links` 離線安裝；非 root 執行 |
| `docker/Caddyfile` | `tls internal`、寫入類路徑 IP allowlist、`header_up X-Forwarded-For {remote_host}` |
| `sql/setup_roles.sql` | 在 PostgreSQL 建立 `cqp` 庫、`app_rw`、`app_ro` 與權限（可重複執行） |
| `requirements.txt`、`requirements-dev.txt` | Python 依賴（全部以 `==` 鎖定版本；dev 另含 pytest、httpx） |
| `pytest.ini` | pytest 設定 |
| `tools/withenv.py` | 讀 `.env` 設定 DSN 後執行指令（測試、工具用） |
| `tools/verify_db.py` | 驗證角色權限，列出 PG 參數 |
| `tools/gen_data.py` | 合成資料產生器 |
| `tools/fixtures.py`、`tools/check_fixtures_copy.py` | AC7 邊界 CSV 與其 COPY 層核對 |
| `bench/ac1_ac2.py`、`bench/JdbcBench.java` | AC1／AC2 匯入匯出速度比較 |

## 啟動步驟

1. 安裝 PostgreSQL **16 以上**（開發與正式建議 17），依下方「PostgreSQL 設定」調整參數與連線權限。
2. 以超級使用者建立資料庫與角色（過程中會提示輸入 `app_rw`、`app_ro` 的密碼）：
   ```sh
   psql -h 127.0.0.1 -U postgres -d postgres -f sql/setup_roles.sql
   ```
3. 複製 `.env.example` 為 `.env`，填入 `CQP_DSN_RW`、`CQP_DSN_RO`（密碼中的特殊字元須 percent-encode，例如 `@` → `%40`；`$` 也要編成 `%24`，否則 Compose 會把它當變數展開）。`.env` 的變數會整份傳進 app 容器。
4. 準備離線套件 `wheels/`（見下方「離線安裝套件」）。
5. 啟動（repo 根目錄）：
   ```sh
   docker compose up -d --build
   ```
6. 確認容器連得到 DB：
   ```sh
   docker compose exec app python -c "import os, psycopg; psycopg.connect(os.environ['CQP_DSN_RO']).close(); print('ok')"
   ```
   瀏覽 `https://<CQP_HOST>`。

- app 容器啟動時先以 `CQP_DSN_RW` 執行 `alembic upgrade head`，建立／更新平台中繼資料表。
- 連線池上限預設每個 app worker 合計 20 條（查詢 12、匯出 4、匯入 2、系統 2）；`CQP_WORKERS=2` 時，DB 最多可能看到 40 條 app 連線。`max_connections` 請預留此容量，或調低 `CQP_POOL_QUERY`、`CQP_POOL_EXPORT`、`CQP_POOL_IMPORT`、`CQP_POOL_SYSTEM`。
- 對外只有 proxy 的 443；app 不對外開 port。使用者連線網址必須與 `CQP_HOST` 一致（Caddy 以它簽發內部憑證）。

## PostgreSQL 設定

### 參數（postgresql.conf）

原本由 compose 帶入的參數，改為在 `postgresql.conf`（或 `ALTER SYSTEM SET ...`）設定，改完重啟服務。

| 參數 | Windows 正式主機（SPEC 3.3） | Mac 測試 | 說明 |
|---|---|---|---|
| `shared_buffers` | 6GB | 1GB | 需重啟 |
| `effective_cache_size` | 16GB | 3GB | |
| `work_mem` | 64MB | 32MB | PG15+ `hash_mem_multiplier` 預設 2.0，hash 節點可用 2×work_mem |
| `maintenance_work_mem` | 2GB | 512MB | |
| `max_parallel_workers_per_gather` | 2 | 2 | |
| `max_wal_size` | 16GB | 4GB | |
| `max_connections` | 50 | 50 | 需重啟；見上方連線池說明 |
| `hash_mem_multiplier` | 2.0 | 2.0 | 明寫，避免預設值變動 |

- Windows 正式值的前提是 DB 可用約 20GB 記憶體（SPEC 3.3）。DB 直接裝在 Windows 主機上時不再受 WSL2 記憶體上限影響，但要扣除 Docker（app、proxy）與其他程式的用量，依實際 RAM 調整。
- `tools/verify_db.py` 會列出這些參數的實際值。

### 讓 Docker 容器連得到 DB

容器內的 `localhost` 是容器自己；DB 在同一台主機時，`.env` 的主機名稱用 `host.docker.internal`。DB 端需要：

1. **`listen_addresses`**：預設 `localhost` 可能收不到容器的連線。**[待實測]** Docker Desktop（Mac／Windows）轉送到主機時，DB 看到的來源位址可能是 127.0.0.1，也可能是 Docker 的虛擬網段；先用預設值測試，連不上再改成主機的實際 IP 或 `'*'`。改成 `'*'` 時務必搭配下面的 `pg_hba.conf` 限制與作業系統防火牆，不要讓區網其他機器直接連 5432。
2. **`pg_hba.conf`**：只允許平台帳號、`cqp` 庫、`scram-sha-256` 驗證。先開 `log_connections = on`，從 PostgreSQL log 看實際來源位址，再把規則收斂到該位址：
   ```
   # TYPE  DATABASE  USER           ADDRESS         METHOD
   host    cqp       app_rw,app_ro  127.0.0.1/32    scram-sha-256
   # 主機上跑 pytest 時（測試庫，見「開發與測試」）
   host    cqp_test  app_rw,app_ro  127.0.0.1/32    scram-sha-256
   # 若 log 顯示來源為 Docker 網段（例如 172.17.0.0/16 或 192.168.65.0/24），把實際網段加在這裡
   ```
   修改後 `SELECT pg_reload_conf();` 即可生效（`listen_addresses` 需重啟）。
3. **Windows 防火牆**：`listen_addresses` 改成非 localhost 時，入站規則只開放給需要的來源。
4. 用「啟動步驟」第 6 步確認連線。

## 離線安裝套件

### Docker image 用的 wheels（Linux）

容器是 Linux，`wheels/` 必須放 Linux 版的 wheel，Python 版本要與 Dockerfile 一致（3.13）。

**請在與 image 相同的 Linux 環境裡下載**：`requirements.txt` 有平台條件（例如 `uvloop ... ; sys_platform != 'win32'`），`pip download` 依「執行 pip 的那台機器」判斷這些條件，即使加 `--platform` 也一樣。在 Windows 上直接下載會漏掉 `uvloop`，Docker build 就會因 `--no-index` 找不到而失敗。在有網路、裝了 Docker 的電腦，於 repo 根目錄執行：

```sh
# 與 image 同架構（Windows／Intel 主機為 amd64；Apple Silicon Mac 為 arm64，Docker 會自動用本機架構）
docker run --rm -v "${PWD}:/w" -w /w python:3.13-slim pip download -r requirements.txt -d wheels
# 要給「另一種架構」的主機用時，加上 --platform，例如在 Apple Silicon Mac 準備 Windows 主機用的 wheels：
docker run --rm --platform linux/amd64 -v "${PWD}:/w" -w /w python:3.13-slim pip download -r requirements.txt -d wheels
```

- Windows 的 cmd 請把 `${PWD}` 換成 `%cd%`；PowerShell 用 `${PWD}` 即可。
- 把整個 `wheels/` 資料夾放到 repo 根目錄（已被 .gitignore 排除，但會進 Docker build context）。
- Dockerfile 以 BuildKit bind mount 掛在 `/tmp/wheels` 執行 `pip install --no-cache-dir --no-index --find-links=/tmp/wheels -r requirements.txt`，wheel 不會留在 image 內。缺任何一個 wheel 會直接失敗，不會連網補抓。需要 BuildKit（Docker Desktop 與 Docker Engine 23 以上預設啟用）。
- 改了 `requirements.txt` 後，要重新下載 wheels。

### 其他離線前提

- **Docker base image**：`python:3.13-slim`、`node:22.14.0-slim`、`caddy:2` 需事先取得。離線主機可在有網路的電腦 `docker pull` 後 `docker save -o images.tar <image...>`，再到離線主機 `docker load -i images.tar`。
- **前端 build**：Dockerfile 的 `npm ci` 目前需要連網（下載 npm 套件）。完全離線時需另行處理（例如預先 build `web/dist`）。

## 開發與測試（主機上，不經 Docker）

**pytest 必須使用獨立的測試庫，不可指向平台庫 `cqp`**：測試會在該庫執行 `alembic upgrade head`、建立／刪除資料表、清空中繼資料表，並呼叫 `importer.recover()`（會把未完成的匯入工作重設或標為失敗）。

```sh
psql -h 127.0.0.1 -U postgres -d postgres -v dbname=cqp_test -f sql/setup_roles.sql   # 建測試庫（角色與平台共用）
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt          # Windows：.venv\Scripts\pip
.venv/bin/python tools/withenv.py -- .venv/bin/python -m pytest -q
.venv/bin/python tools/withenv.py -- .venv/bin/python tools/verify_db.py
```

- `.env` 填 `CQP_TEST_DSN`、`CQP_TEST_DSN_RO`（指向 `cqp_test`）。沒填時資料庫整合測試會被略過（不是失敗）；填成與平台 DSN 同一個庫時，`tools/withenv.py` 會拒絕執行。
- `tools/withenv.py` 以 python-dotenv 讀 `.env`（與 Compose 相同規則）。DSN 優先順序：shell 已設定的 `CQP_DSN_*` → `.env` 的 `CQP_HOST_DSN_*` → `.env` 的 `CQP_DSN_*`。主機上通常無法解析 `host.docker.internal`，請在 `.env` 另填 `CQP_HOST_DSN_RW`、`CQP_HOST_DSN_RO`（例如 `127.0.0.1`）。
- `tools/verify_db.py`、`tools/check_fixtures_copy.py`、`bench/ac1_ac2.py` 會在 `CQP_DSN_*` 指向的庫建立再刪除測試表（`verify_t`、`fx_t`、`bench_policy`）；建議同樣以 shell 指定測試庫執行，例如 `CQP_DSN_RW=<cqp_test 的 DSN> CQP_DSN_RO=<...> .venv/bin/python tools/withenv.py -- ...`。
- 主機離線安裝同樣可用 `pip install --no-index --find-links=<目錄> -r requirements-dev.txt`，但 wheel 要用 `pip download` 在**同作業系統、同 Python 版本**下載（不加 `--platform`）。

## 權限設計（sql/setup_roles.sql）

- `app_rw`：`cqp` 庫與 `public` schema 的 owner，可建表。用於匯入、刪除、欄位維護、alembic 與系統工作。
- `app_ro`：只有 `USAGE` on schema 與 `SELECT`。用於查詢與匯出（SPEC 9.5、11：查詢一律走唯讀角色）。查詢 SQL 由使用者在畫面上的條件、計算欄位、join 組出，平台又沒有登入；以唯讀角色執行，即使 SQL 組裝有 bug 或被注入，資料庫權限也會擋下任何修改。
- `ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public GRANT SELECT ON TABLES TO app_ro`：app_rw 日後建的表，app_ro 自動可讀（PG15+ public schema 預設不給 CREATE，這裡明確交給 app_rw）。
- `app_ro` 另設 `default_transaction_read_only = on`，但這只是預設值（連線後可 `SET` 關掉），真正的防線是權限；`tools/verify_db.py` 兩者都驗證。
- 注意：default privileges 只對「之後」建立的表生效；若 owner 不是 app_rw（例如以 postgres 建表），app_ro 讀不到。
- 兩個 DSN 技術上可以填同一個帳號，但會失去上述保護，不建議。

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
.venv/bin/python tools/withenv.py -- .venv/bin/python bench/ac1_ac2.py --rows 20000
```

需主機上有 `psql`。psql、psycopg、JDBC 都在主機上經同一條網路路徑連 `CQP_DSN_RW` 指向的 DB；psql 每次呼叫含行程啟動與連線成本，腳本另量基線並輸出「扣基線」欄。要驗收 AC1 的「≤10%」需使用 GB 級檔案。JDBC 使用 `~/.m2` 內既有的 pgjdbc jar 以 `java` 單檔原始碼啟動，找不到 jar 或 java 時自動跳過。

-- 在自行安裝的 PostgreSQL（16 以上）建立平台用的資料庫、角色與權限。
-- 以超級使用者（例如 postgres）執行；ALTER SCHEMA OWNER、ALTER DEFAULT PRIVILEGES FOR ROLE
-- 需要是 app_rw 的成員或超級使用者，只有 CREATEROLE／CREATEDB 的帳號不夠：
--   psql -h 127.0.0.1 -U postgres -d postgres -f sql/setup_roles.sql                      # 平台庫 cqp
--   psql -h 127.0.0.1 -U postgres -d postgres -v dbname=cqp_test -f sql/setup_roles.sql   # 測試庫（pytest 專用）
--
-- - 可重複執行：角色、資料庫不存在才建立；權限設定本身可重複套用。
-- - 角色 app_rw／app_ro 是整個 server 共用的；平台庫與測試庫用同一組角色。
-- - 密碼：執行到 \password 時由 psql 提示輸入（不顯示、不寫入檔案或命令列，送出前已在用戶端雜湊）。
--   重複執行時會再問一次，可用來更換密碼（會同時影響平台庫與測試庫的連線）。
-- - 資料庫一律 UTF8（平台的 CSV 一律 UTF-8）。TEMPLATE template0 才能指定與叢集預設不同的編碼；
--   Linux／Mac 叢集若使用非 UTF-8 的 locale 會建立失敗，此時請以 UTF-8 locale 重新 initdb。
-- - 之後由 app 啟動時以 app_rw 執行 alembic upgrade head 建立中繼資料表。
\set ON_ERROR_STOP on
\if :{?dbname}
\else
  \set dbname cqp
\endif

-- ---- 第一段：角色與資料庫（連在 postgres 庫）----
SELECT 'CREATE ROLE app_rw LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') \gexec
SELECT 'CREATE ROLE app_ro LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ro') \gexec

\echo '請設定 app_rw（讀寫角色）的密碼：'
\password app_rw
\echo '請設定 app_ro（唯讀角色）的密碼：'
\password app_ro

-- 唯讀角色預設 read-only transaction（只是預設值，連線後可 SET 關掉；真正的防線是下面的權限）
ALTER ROLE app_ro SET default_transaction_read_only = on;

-- CREATE DATABASE 不能放在交易或 DO 區塊內，用 \gexec 達成「不存在才建立」
SELECT format('CREATE DATABASE %I OWNER app_rw ENCODING %L TEMPLATE template0', :'dbname', 'UTF8')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec

REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"dbname" TO app_rw, app_ro;

-- ---- 第二段：資料庫內的 schema 權限 ----
\connect :"dbname"

-- PG15+ 的 public schema 預設只有 owner 有 CREATE；明確把 owner 交給 app_rw
ALTER SCHEMA public OWNER TO app_rw;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO app_rw;
GRANT USAGE ON SCHEMA public TO app_ro;
-- app_rw 日後建立的表，app_ro 自動可 SELECT（分割子表也由 app_rw 建立）
ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public GRANT SELECT ON TABLES TO app_ro;

\echo '完成：資料庫' :dbname '、角色 app_rw／app_ro 與權限已設定。'

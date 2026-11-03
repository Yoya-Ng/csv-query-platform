#!/bin/bash
# 由 postgres 官方映像在「資料目錄為空」的首次啟動時執行（/docker-entrypoint-initdb.d，依檔名排序）。
# 密碼來自容器環境變數，不寫死；用 psql 變數 + :'var' 做字面量引號，避免注入與跳脫問題。
set -euo pipefail

: "${POSTGRES_PASSWORD:?}" "${CQP_RW_PASSWORD:?}" "${CQP_RO_PASSWORD:?}"

# 第一段：角色與資料庫（連 postgres 庫）
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname postgres \
  -v rw_pw="${CQP_RW_PASSWORD}" -v ro_pw="${CQP_RO_PASSWORD}" <<'EOSQL'
CREATE ROLE app_rw LOGIN PASSWORD :'rw_pw';
CREATE ROLE app_ro LOGIN PASSWORD :'ro_pw';
-- 唯讀角色預設 read-only transaction（role 層級預設值；只是預設，不是安全邊界，權限才是）
ALTER ROLE app_ro SET default_transaction_read_only = on;
CREATE DATABASE cqp OWNER app_rw;
REVOKE ALL ON DATABASE cqp FROM PUBLIC;
GRANT CONNECT ON DATABASE cqp TO app_rw, app_ro;
EOSQL

# 第二段：cqp 內的 schema 權限
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname cqp <<'EOSQL'
-- PG15+ 的 public schema 預設只有 owner 有 CREATE；明確把 owner 交給 app_rw
ALTER SCHEMA public OWNER TO app_rw;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO app_rw;
GRANT USAGE ON SCHEMA public TO app_ro;
-- app_rw 日後建立的表，app_ro 自動可 SELECT（含序列／分割子表皆由 app_rw 建立）
ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA public GRANT SELECT ON TABLES TO app_ro;
EOSQL

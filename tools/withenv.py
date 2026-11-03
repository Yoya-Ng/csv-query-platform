"""本機開發用：讀 repo 根目錄的 .env，設定 DSN 環境變數後執行指令（不印出密碼）。

用法：python tools/withenv.py [--env 路徑] -- python -m pytest -q

DSN 取值順序（RW、RO 各自判斷）：
  1. shell 已設定的 CQP_DSN_*（明確指定，最優先）
  2. .env 的 CQP_HOST_DSN_*（主機上通常無法解析容器用的 host.docker.internal）
  3. .env 的 CQP_DSN_*
測試 DSN（CQP_TEST_DSN／CQP_TEST_DSN_RO）不會自動沿用平台 DSN：pytest 會在該庫建立／刪除資料表、
執行 alembic 與 importer.recover()（會改動未完成的匯入工作），只能指向獨立的測試庫。未設定時 db 測試略過；
若測試 DSN 與平台 DSN 指向同一個資料庫（主機、port、資料庫名稱相同），直接拒絕執行。
.env 以 python-dotenv 解析（與 Docker Compose 相同：支援行尾註解、引號、${VAR} 展開）。
"""
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

root = Path(__file__).resolve().parents[1]
args = sys.argv[1:]
env_path = root / ".env"
if args[:1] == ["--env"]:
    if len(args) < 2:
        sys.exit("--env 需要檔案路徑")
    env_path = Path(args[1])
    args = args[2:]
if args[:1] == ["--"]:
    args = args[1:]
if not args:
    sys.exit("用法：python tools/withenv.py [--env 路徑] -- <指令>")
if not env_path.is_file():
    sys.exit(f"找不到 {env_path}；請複製 .env.example 為 .env 並填入連線資訊")

shell = dict(os.environ)
file_vals = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
env = {**file_vals, **shell}                      # shell 優先

for kind in ("RW", "RO"):
    dsn = shell.get(f"CQP_DSN_{kind}") or file_vals.get(f"CQP_HOST_DSN_{kind}") or file_vals.get(f"CQP_DSN_{kind}")
    if not dsn:
        sys.exit(f"{env_path} 缺少 CQP_DSN_{kind}（或 CQP_HOST_DSN_{kind}）")
    env[f"CQP_DSN_{kind}"] = dsn


def db_identity(dsn: str) -> tuple[str, str, str]:
    d = conninfo_to_dict(dsn)
    return (str(d.get("host") or "localhost"), str(d.get("port") or "5432"), str(d.get("dbname") or ""))


platform_dbs = {db_identity(env["CQP_DSN_RW"]), db_identity(env["CQP_DSN_RO"])}
for key in ("CQP_TEST_DSN", "CQP_TEST_DSN_RO"):
    test_dsn = env.get(key)
    if test_dsn and db_identity(test_dsn) in platform_dbs:
        sys.exit(f"{key} 與平台 DSN 指向同一個資料庫；測試會改動資料，請改用獨立的測試庫（例如 cqp_test）")

sys.exit(subprocess.call(args, env=env, cwd=root))

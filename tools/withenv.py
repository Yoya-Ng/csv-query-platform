"""本機開發用：讀 env/local.env 組出 DSN 環境變數後執行指令（不印出密碼）。

用法：uv run python tools/withenv.py -- pytest -q
"""
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
env = dict(os.environ)
for line in (root / "env" / "local.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
host = env.get("CQP_TEST_HOST", "127.0.0.1:55432")
env["CQP_DSN_RW"] = f"postgresql://app_rw:{env['CQP_RW_PASSWORD']}@{host}/cqp"
env["CQP_DSN_RO"] = f"postgresql://app_ro:{env['CQP_RO_PASSWORD']}@{host}/cqp"
env["CQP_TEST_DSN"] = env["CQP_DSN_RW"]
env["CQP_TEST_DSN_RO"] = env["CQP_DSN_RO"]
args = sys.argv[1:]
if args and args[0] == "--":
    args = args[1:]
sys.exit(subprocess.call(args, env=env, cwd=root))

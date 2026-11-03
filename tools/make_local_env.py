"""由 env/mac.env.example 產生 env/local.env（隨機密碼；本機開發／測試用，已被 .gitignore 排除）。

用法：python tools/make_local_env.py [--force]
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = {"DB_MEM_LIMIT": "3g", "PG_SHARED_BUFFERS": "768MB", "PG_EFFECTIVE_CACHE_SIZE": "2GB"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆蓋既有 env/local.env")
    a = ap.parse_args()
    dst = ROOT / "env" / "local.env"
    if dst.exists() and not a.force:
        print(f"{dst} 已存在；加 --force 才會覆蓋（會換新密碼，既有 volume 內的密碼不會變）", file=sys.stderr)
        return 1
    out = []
    for line in (ROOT / "env" / "mac.env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m:
            k = m.group(1)
            if k.endswith("_PASSWORD"):
                line = f"{k}={secrets.token_hex(16)}"
            elif k in OVERRIDES:
                line = f"{k}={OVERRIDES[k]}"
        out.append(line)
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    dst.chmod(0o600)
    print(f"已建立 {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

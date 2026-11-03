# ---- 前端 build（Vue 3 + Vite）----
FROM node:22.14.0-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- 後端 ----
FROM python:3.13-slim

# uv 官方映像提供的靜態執行檔
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# 先裝依賴（利用層快取），再複製程式碼
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
# 前端靜態檔：FastAPI 在 /api 以外的路徑提供（app/main.py）
COPY --from=web /web/dist ./web/dist
RUN uv sync --frozen --no-dev

RUN useradd -r -u 10001 cqp && mkdir -p /var/lib/cqp/uploads /var/lib/cqp/errors \
    && chown -R cqp /var/lib/cqp
USER cqp

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${CQP_WORKERS:-2}"]

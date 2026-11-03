# ---- 前端 build（Vue 3 + Vite）----
FROM node:22.14.0-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- 後端 ----
FROM python:3.13-slim

# pip 安裝時預設會產生 .pyc（site-packages 之後是 root 擁有、執行身分無法寫入，所以要在建置時就編好）
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# 離線安裝：wheels/（repo 根目錄，需事先以 pip download 準備 Linux 版 wheel，見 docs/infra.md）
# 以 BuildKit bind mount 掛在 /tmp/wheels，只在這一步可見，不會留在 image 內。
# --no-index：不連 PyPI；缺任何一個 wheel 會直接失敗，不會偷偷上網補。
COPY requirements.txt ./
RUN --mount=type=bind,source=wheels,target=/tmp/wheels \
    pip install --no-cache-dir --no-index --find-links=/tmp/wheels -r requirements.txt

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
# 前端靜態檔：FastAPI 在 /api 以外的路徑提供（app/main.py）
COPY --from=web /web/dist ./web/dist
RUN python -m compileall -q app migrations

RUN useradd -r -u 10001 cqp && mkdir -p /var/lib/cqp/uploads /var/lib/cqp/errors \
    && chown -R cqp /var/lib/cqp
USER cqp

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${CQP_WORKERS:-2}"]

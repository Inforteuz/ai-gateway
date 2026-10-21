# API gateway + web console. Build context: repository root.

FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SF_STATIC_DIR=/app/web
WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/app ./app
COPY docker/api/bootstrap.py ./bootstrap.py
COPY --from=web /web/dist ./web

RUN useradd --system --uid 10001 --home /app gateway
USER gateway
EXPOSE 8310

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8310", \
     "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header", \
     "--timeout-keep-alive", "30", "--timeout-graceful-shutdown", "25", "--log-level", "info"]

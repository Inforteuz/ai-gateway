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
    SF_STATIC_DIR=/app/web \
    FORWARDED_ALLOW_IPS=127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16
WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/app ./app
COPY docker/api/bootstrap.py ./bootstrap.py
COPY --from=web /web/dist ./web

# X-Forwarded-* is trusted only from FORWARDED_ALLOW_IPS (uvicorn reads it from the environment):
# loopback and the private ranges Docker uses, where Caddy / the host's reverse proxy connect from.
# The API port is published on 127.0.0.1 by default, so outside clients cannot reach it directly.
RUN useradd --system --uid 10001 --home /app gateway
USER gateway
EXPOSE 8310

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8310", \
     "--proxy-headers", "--no-server-header", \
     "--timeout-keep-alive", "30", "--timeout-graceful-shutdown", "25", "--log-level", "info"]

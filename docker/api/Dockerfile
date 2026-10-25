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
    FORWARDED_ALLOW_IPS=127.0.0.1
WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/app ./app
# Adapter limits/capabilities and prompt composition are the routing source of
# truth. Importing these modules does not launch the cluster or any browsers.
COPY backend/cluster ./cluster
COPY docker/api/bootstrap.py ./bootstrap.py
COPY --from=web /web/dist ./web

# X-Forwarded-* is trusted only from FORWARDED_ALLOW_IPS (uvicorn reads it from the environment).
# docker-compose.yml sets it to the exact proxy hops (host gateway + Caddy); the image default trusts
# loopback only. The API port is published on 127.0.0.1 by default.
RUN useradd --system --uid 10001 --home /app gateway
USER gateway
EXPOSE 8310

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8310", \
     "--proxy-headers", "--no-server-header", \
     "--timeout-keep-alive", "30", "--timeout-graceful-shutdown", "25", "--log-level", "info"]

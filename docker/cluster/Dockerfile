# Web-chat cluster: Chrome instances on a virtual display + the cluster API.
# Build context: repository root.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg procps tini \
        xvfb x11vnc openbox tinyproxy supervisor \
        fonts-liberation fonts-noto-core fonts-noto-color-emoji fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome on amd64, Debian Chromium elsewhere (Chrome has no arm64 Linux build)
RUN set -eux; arch="$(dpkg --print-architecture)"; \
    if [ "$arch" = "amd64" ]; then \
        curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg; \
        echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
            > /etc/apt/sources.list.d/google-chrome.list; \
        apt-get update; apt-get install -y --no-install-recommends google-chrome-stable; \
    else \
        apt-get update; apt-get install -y --no-install-recommends chromium; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# Hide the "unsupported command-line flag" infobar (we run with --no-sandbox inside the container)
COPY docker/cluster/chrome-policy.json /etc/opt/chrome/policies/managed/gateway.json
COPY docker/cluster/chrome-policy.json /etc/chromium/policies/managed/gateway.json

WORKDIR /opt/gateway
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt websockets
COPY backend/cluster ./cluster
COPY docker/cluster/entrypoint.sh docker/cluster/launch-chrome.sh docker/cluster/heal-watch.sh \
     docker/cluster/import_cookies.py /opt/gateway/bin/
RUN chmod +x /opt/gateway/bin/*.sh \
    && useradd --system --uid 10002 --create-home --home-dir /home/gateway gateway

ENV DISPLAY=:99 \
    SF_CLUSTER_HOST=0.0.0.0 \
    SF_CLUSTER_PORT=8800 \
    SF_CLUSTER_STATE_DIR=/data/state \
    SF_CLUSTER_PROFILE_DIR=/data/profiles \
    SF_CLUSTER_BACKUP_DIR=/data/backups \
    SF_CLUSTER_PROXIES_FILE=/data/proxies.txt \
    SF_CLUSTER_MEM_GUARD_STATE=/data/state/memory_guard.json \
    SF_CLUSTER_HEAL_REQ_DIR=/run/gateway-heal \
    SF_CLUSTER_ATTACHMENT_DIR=/data/attachments

VOLUME ["/data"]
EXPOSE 8800 5900
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/gateway/bin/entrypoint.sh"]

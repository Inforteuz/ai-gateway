"""AG-07: X-Forwarded-For is honoured only from trusted proxies (the Docker image's FORWARDED_ALLOW_IPS)."""
import asyncio
import re
from pathlib import Path

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "api" / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"


def trusted_default() -> str:
    """The trusted proxy list a default Docker Compose install runs with."""
    m = re.search(r"FORWARDED_ALLOW_IPS: \$\{FORWARDED_ALLOW_IPS:-([^}]+)\}", COMPOSE.read_text())
    assert m, "docker-compose.yml must set FORWARDED_ALLOW_IPS for the api service"
    return m.group(1)


def client_seen(peer: str, xff: str | None) -> str:
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]

    mw = ProxyHeadersMiddleware(app, trusted_hosts=trusted_default())
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    asyncio.run(mw({"type": "http", "client": (peer, 5000), "headers": headers, "scheme": "http"}, None, None))
    return seen["client"]


def test_only_exact_proxy_hops_are_trusted():
    trusted = trusted_default().split(",")
    assert trusted == ["127.0.0.1", "172.30.240.1", "172.30.240.10"]
    assert not any("/" in t or t == "*" for t in trusted)            # no CIDR ranges, no wildcard
    assert "--forwarded-allow-ips=*" not in DOCKERFILE.read_text()
    assert "FORWARDED_ALLOW_IPS=127.0.0.1\n" in DOCKERFILE.read_text()   # image default: loopback only
    compose = COMPOSE.read_text()
    assert "ipv4_address: ${GATEWAY_PROXY_IP:-172.30.240.10}" in compose
    assert "subnet: ${GATEWAY_SUBNET:-172.30.240.0/24}" in compose


def test_spoofed_header_from_direct_client_is_ignored():
    assert client_seen("203.0.113.50", "1.2.3.4") == "203.0.113.50"


def test_spoofed_header_from_another_container_is_ignored():
    # e.g. a compromised cluster/db container on the same Docker network
    assert client_seen("172.30.240.5", "1.2.3.4") == "172.30.240.5"


def test_header_from_caddy_is_used():
    assert client_seen("172.30.240.10", "198.51.100.7") == "198.51.100.7"


def test_header_from_host_proxy_is_used():
    # nginx on the host -> 127.0.0.1:API_PORT -> container, seen from the network gateway
    assert client_seen("172.30.240.1", "198.51.100.7") == "198.51.100.7"


def test_client_supplied_prefix_cannot_override_real_address():
    # A proxy that appends: "<spoofed>, <real client>" -> the rightmost untrusted address wins
    assert client_seen("172.30.240.10", "9.9.9.9, 198.51.100.7") == "198.51.100.7"


def test_ipv6_client_spoofing_is_ignored():
    assert client_seen("2001:db8::5", "1.2.3.4") == "2001:db8::5"


# --- request body limit (chunked bodies have no Content-Length) -------------------------------------------------
def test_chunked_body_over_limit_is_refused(env):
    from .conftest import add_key, add_user, bearer
    uid = add_user(env, balance_som=100)
    key, _ = add_key(env, uid)
    limit = max(env.cfg.max_body_bytes, env.cfg.max_multimodal_bytes)

    def chunks():
        piece = b"x" * 1024 * 1024
        for _ in range(limit // len(piece) + 2):
            yield piece

    r = env.client.post("/v1/chat/completions", content=chunks(), headers={**bearer(key), "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"


def test_chunked_portal_body_over_default_limit_is_refused(env):
    def chunks():
        for _ in range(env.cfg.max_body_bytes // 65536 + 2):
            yield b"x" * 65536

    r = env.client.post("/portal/auth/login", content=chunks(), headers={"Content-Type": "application/json", "X-SF-CSRF": "1"})
    assert r.status_code == 413


def test_small_chunked_body_still_works(env):
    import json as _json
    from .conftest import CHAT, add_key, add_user, bearer
    uid = add_user(env, balance_som=1000)
    key, _ = add_key(env, uid)
    r = env.client.post("/v1/chat/completions", content=iter([_json.dumps(CHAT).encode()]),
                        headers={**bearer(key), "Content-Type": "application/json"})
    assert r.status_code == 200

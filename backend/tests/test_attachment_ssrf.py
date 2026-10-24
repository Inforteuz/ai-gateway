"""Attachment URLs from API clients must not reach internal addresses (SSRF)."""
import http.server
import ipaddress
import socket
import threading

import pytest

from cluster import attachments as A


def fake_dns(monkeypatch, *ips):
    calls = []

    def getaddrinfo(host, port, *a, **kw):
        calls.append(host)
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(A.socket, "getaddrinfo", getaddrinfo)
    return calls


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.18.0.4", "192.168.1.1", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "::1", "fd00::1", "::ffff:127.0.0.1", "fe80::1"])
def test_internal_addresses_are_refused(monkeypatch, ip):
    fake_dns(monkeypatch, ip)
    with pytest.raises(A.AttachmentError):
        A._resolve_public("http://files.example/x.png")


def test_one_internal_answer_among_public_ones_is_refused(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34", "127.0.0.1")
    with pytest.raises(A.AttachmentError):
        A._resolve_public("https://files.example/x.png")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://files.example/x", "gopher://x/", "http:///nohost"])
def test_only_http_urls(url):
    with pytest.raises(A.AttachmentError):
        A._resolve_public(url)


def names(calls):
    """Hostname lookups only (create_connection also 'resolves' the numeric IP it was given)."""
    def is_ip(c):
        try:
            ipaddress.ip_address(c)
            return True
        except ValueError:
            return False
    return [c for c in calls if not is_ip(c)]


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):
        _Handler.hits.append(self.path)
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server(monkeypatch):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Handler.hits = []
    # The test server lives on loopback: treat it as public, everything else stays checked
    monkeypatch.setattr(A, "_is_public_ip", lambda ip: True)
    yield srv.server_address[1]
    srv.shutdown()


def test_redirects_are_not_followed(local_server, monkeypatch):
    calls = fake_dns(monkeypatch, "127.0.0.1")
    with pytest.raises(A.AttachmentError, match="redirects elsewhere"):
        A._fetch_public(f"http://files.example:{local_server}/redirect", 1000, 5)
    assert _Handler.hits == ["/redirect"] and names(calls) == ["files.example"]


def test_connects_to_the_checked_address_only_once_resolved(local_server, monkeypatch):
    calls = fake_dns(monkeypatch, "127.0.0.1")
    data, mime = A._fetch_public(f"http://files.example:{local_server}/a.txt?x=1", 1000, 5)
    assert (data, mime) == (b"hello", "text/plain")
    assert names(calls) == ["files.example"]     # one DNS lookup; the socket then uses the checked IP literal
    assert _Handler.hits == ["/a.txt?x=1"]


def test_size_limit(local_server, monkeypatch):
    fake_dns(monkeypatch, "127.0.0.1")
    with pytest.raises(A.AttachmentError, match="too large"):
        A._fetch_public(f"http://files.example:{local_server}/a.txt", 3, 5)

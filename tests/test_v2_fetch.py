import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from harvest_rag.config import Settings
from harvest_rag.fetch import Fetcher, Blocked, ReviewRequired, retry_after
from harvest_rag.models import CrawlPlan


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://outside.invalid/secret")
                self.end_headers()
                return
            if self.path == "/blocked":
                self.send_response(403)
                self.end_headers()
                return
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("ETag", '"v1"')
            self.end_headers()
            self.wfile.write(b'<html><body><article>' + b'Useful content for testing. ' * 100 + b'</article><a href="/next#section">Next</a></body></html>')
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try: yield f"http://127.0.0.1:{instance.server_port}"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def fetcher(**kw):
    return Fetcher(Settings(allow_private_network=True, browser_enabled=False, **kw),
                   SimpleNamespace(reserve_domain=lambda *_: 0, cool_domain=lambda *_: None))


def test_conditional_http_and_link_canonicalization(server):
    plan = CrawlPlan(seed_urls=[server], max_depth=1, discovery_strategy="links").model_dump()
    f = fetcher()
    first = f.fetch(server, plan)
    assert first["status"] == 200 and first["etag"] == '"v1"'
    assert f.links(first, plan) == [server + "/next"]
    assert f.fetch(server, plan, first)["status"] == 304


def test_redirect_is_checked_before_following(server):
    with pytest.raises(Blocked, match="outside"):
        fetcher().http(server + "/redirect", ["127.0.0.1"])


def test_access_denied_and_body_limit(server):
    with pytest.raises(Blocked): fetcher().http(server + "/blocked", ["127.0.0.1"])
    with pytest.raises(ReviewRequired): fetcher(max_body_bytes=1024).http(server, ["127.0.0.1"])


def test_browser_versions_do_not_send_http_validators(server):
    result = fetcher().fetch(server, CrawlPlan(seed_urls=[server]).model_dump(), {"fetcher": "browser", "etag": '"v1"'})
    assert result["status"] == 200


def test_retry_after_is_bounded():
    assert retry_after("999999") == 3600
    assert retry_after("-5") == 0
    assert retry_after("invalid") == 30

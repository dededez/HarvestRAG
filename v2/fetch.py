from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx
from lxml import etree, html

from fetchers.config_loader import max_retries_for
from fetchers.content_heuristics import looks_empty
from v2 import obstacles
from v2.browser import BrowserSession
from v2.site_config import fetch_config, obstacle_config
from v2.urls import canonicalize, host_of, validate_target


class Blocked(RuntimeError):
    pass


class ReviewRequired(RuntimeError):
    pass


def retry_after(value):
    try:
        return min(3600, max(0, float(value)))
    except (TypeError, ValueError):
        try:
            return min(3600, max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            return 30


class Fetcher:
    """Static HTTP first, then Playwright with the V1 obstacle policy.

    ``browser`` may be a shared BrowserSession (worker) or None (one-off API
    call). ``logger`` is V2's EventLogger; fetch attempts and obstacle events
    are written to v2_events for auditability.
    """

    def __init__(self, settings, db, browser=None, logger=None, fetch_cfg=None, obstacle_cfg=None):
        self.cfg = settings
        self.db = db
        self._browser = browser
        self.logger = logger
        self.fetch_cfg = fetch_cfg or fetch_config()
        self.obstacle_cfg = obstacle_cfg or obstacle_config()
        self.user_agent = str(self.fetch_cfg.get("user_agent") or "WebScrapperV2/2.0")

    def set_logger(self, logger):
        self.logger = logger

    def emit(self, event, **kwargs):
        if self.logger is not None:
            self.logger.log_event(event, **kwargs)

    def _attempt(self, url, fetcher, outcome, *, status=None, content_type=None, reason=None):
        self.emit("fetch_attempt", url=url, fetcher=fetcher, outcome=outcome,
                  status=status, content_type=content_type, reason=reason)

    def wait_domain(self, url):
        while True:
            delay = self.db.reserve_domain(host_of(url), self.cfg.domain_interval)
            if delay <= 0:
                return
            time.sleep(min(delay, 1))

    @staticmethod
    def _decode(body: bytes, encoding: str | None):
        for candidate in (encoding, "utf-8"):
            if not candidate:
                continue
            try:
                return body.decode(candidate)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("latin-1", errors="replace")

    def http(self, url, domains, headers=None):
        original = url
        max_redirects = int(self.fetch_cfg.get("max_redirects", 10))
        max_429 = max_retries_for(self.obstacle_cfg, "rate_limit")
        max_5xx = max_retries_for(self.obstacle_cfg, "server_errors")
        base_backoff = float(self.fetch_cfg.get("backoff_base_seconds", 1.0))
        retries_429 = retries_5xx = 0
        redirects = 0

        with httpx.Client(timeout=self.cfg.request_timeout, follow_redirects=False,
                          trust_env=False) as client:
            while True:
                try:
                    url = validate_target(url, domains, self.cfg.allow_private_network)
                except ValueError as exc:
                    raise Blocked(str(exc)) from exc
                self.wait_domain(url)
                request_headers = {
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    **(headers or {}),
                }
                with client.stream("GET", url, headers=request_headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        target = urljoin(url, resp.headers.get("location", ""))
                        self.emit("obstacle_detected", url=original, outcome="follow_and_log",
                                  reason=f"redirect: {url} -> {target}",
                                  details={"obstacle": "unexpected_redirect",
                                           "policy": "follow_and_log",
                                           "detection_method": "redirect_chain_detected"})
                        redirects += 1
                        if redirects > max_redirects:
                            self._attempt(original, "http", "failed", status=resp.status_code,
                                          reason="redirect limit exceeded")
                            raise Blocked("Redirect limit exceeded")
                        url = target
                        headers = None  # validators belong to the original resource
                        continue
                    if resp.status_code in (401, 403):
                        self._attempt(original, "http", "blocked", status=resp.status_code,
                                      reason=f"HTTP {resp.status_code}")
                        raise Blocked(f"HTTP {resp.status_code}")
                    if resp.status_code == 429:
                        delay = retry_after(resp.headers.get("retry-after"))
                        self.db.cool_domain(host_of(url), delay)
                        self.emit("obstacle_detected", url=original, outcome="retry_with_backoff",
                                  reason=f"HTTP 429, backing off {delay:.1f}s",
                                  details={"obstacle": "rate_limit", "policy": "retry_with_backoff",
                                           "detection_method": "http_status_429",
                                           "attempt": retries_429 + 1})
                        if retries_429 < max_429:
                            retries_429 += 1
                            time.sleep(min(delay, 30))
                            continue
                        self._attempt(original, "http", "failed", status=429,
                                      reason="rate limited")
                        raise RuntimeError("HTTP 429 rate limited")
                    if 500 <= resp.status_code < 600:
                        self.emit("obstacle_detected", url=original, outcome="retry_with_backoff",
                                  reason=f"HTTP {resp.status_code}",
                                  details={"obstacle": "server_errors",
                                           "policy": "retry_with_backoff",
                                           "detection_method": "http_status_5xx",
                                           "status": resp.status_code,
                                           "attempt": retries_5xx + 1})
                        if retries_5xx < max_5xx:
                            retries_5xx += 1
                            time.sleep(min(30, base_backoff * 2 ** (retries_5xx - 1)))
                            continue
                        self._attempt(original, "http", "failed", status=resp.status_code,
                                      reason="server errors")
                        raise RuntimeError(f"HTTP {resp.status_code}")

                    if resp.status_code == 304:
                        self._attempt(original, "http", "not_modified", status=304,
                                      content_type=resp.headers.get("content-type"))
                        return {"url": original, "final_url": url, "status": 304,
                                "content_type": resp.headers.get("content-type", "").split(";")[0].lower(),
                                "text": "", "raw": "", "etag": resp.headers.get("etag"),
                                "last_modified": resp.headers.get("last-modified"),
                                "fetcher": "http", "scraped_at": datetime.now(timezone.utc).isoformat()}

                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError:
                        self._attempt(original, "http", "failed", status=resp.status_code,
                                      reason=f"HTTP {resp.status_code}")
                        raise
                    body = bytearray()
                    for part in resp.iter_bytes():
                        body.extend(part)
                        if len(body) > self.cfg.max_body_bytes:
                            self._attempt(original, "http", "review_required", status=resp.status_code,
                                          reason="response exceeds size limit")
                            raise ReviewRequired("Response exceeds configured size limit")
                    ctype = resp.headers.get("content-type", "").split(";")[0].lower()
                    text = "" if "pdf" in ctype else self._decode(bytes(body), resp.encoding)
                    self._attempt(original, "http", "success", status=resp.status_code,
                                  content_type=ctype)
                    return {"url": original, "final_url": url, "status": resp.status_code,
                            "content_type": ctype, "text": text,
                            "raw": base64.b64encode(body).decode(),
                            "etag": resp.headers.get("etag"), "last_modified": resp.headers.get("last-modified"),
                            "fetcher": "http", "scraped_at": datetime.now(timezone.utc).isoformat()}

    def _browser_handle(self):
        if self._browser is None:
            return None
        if hasattr(self._browser, "ensure"):
            return self._browser.ensure()
        return self._browser

    def fetch(self, url, plan, previous=None):
        headers = {}
        if previous and previous.get("fetcher") == "http":
            if previous.get("etag"):
                headers["If-None-Match"] = previous["etag"]
            if previous.get("last_modified"):
                headers["If-Modified-Since"] = previous["last_modified"]
        result = self.http(url, plan["allowed_domains"], headers)
        if result["status"] == 304:
            if not previous:
                raise RuntimeError("304 without a usable previous version")
            return result
        if "html" in result["content_type"]:
            tree = html.fromstring(result["text"] or "<html/>")
            visible = " ".join(tree.xpath("//body//text()[not(ancestor::script) and not(ancestor::style)]"))
            if any(x in visible.lower() for x in ("verify you are human", "complete the captcha", "checking your browser")):
                self._attempt(url, "http", "blocked", reason="access-control challenge detected")
                raise Blocked("Access-control challenge detected")
            threshold = float(self.fetch_cfg.get("empty_content_threshold_chars", 100))
            if self.cfg.browser_enabled and looks_empty(result["text"], threshold):
                self.emit("fetch_decision", url=url, outcome="fallback",
                          reason=f"static visible text below {threshold} chars; browser fallback",
                          details={"fetcher": "http"})
                result = self.browser(url, plan["allowed_domains"])
        return result

    def browser(self, url, domains):
        shared = self._browser_handle()
        owns = shared is None
        session = None
        if owns:
            session = BrowserSession()
            shared = session.ensure()
        context = None
        try:
            context = shared.new_context(
                user_agent=str(self.fetch_cfg.get("user_agent") or "WebScrapperV2/2.0"),
                service_workers="block",
            )

            def route_request(route):
                try:
                    validate_target(route.request.url, domains, self.cfg.allow_private_network)
                    self.wait_domain(route.request.url)
                    route.continue_()
                except (ValueError, OSError):
                    route.abort()

            context.route("**/*", route_request)
            page = context.new_page()
            page.on("request", lambda request: obstacles.log_redirect(request, self.emit, url))
            timeout_ms = int(float(self.fetch_cfg.get("timeout_seconds", 30)) * 1000)
            try:
                response = obstacles.goto_with_retries(
                    page, url, self.obstacle_cfg, self.fetch_cfg, self.emit, timeout_ms)
            except obstacles.FetchFailed as exc:
                self._attempt(url, "browser", "failed", reason=exc.reason)
                raise RuntimeError(exc.reason) from exc
            if response and response.status in (401, 403):
                self._attempt(url, "browser", "blocked", status=response.status,
                              reason=f"browser HTTP {response.status}")
                raise Blocked(f"Browser HTTP {response.status}")
            if response and response.status >= 400:
                self._attempt(url, "browser", "failed", status=response.status,
                              reason=f"browser HTTP {response.status}")
                raise RuntimeError(f"Browser HTTP {response.status}")

            obstacles.settle(page, self.fetch_cfg, self.emit, url)
            try:
                terminal = obstacles.terminal_block(page, self.obstacle_cfg, self.emit, url)
                if terminal:
                    self._attempt(url, "browser", "blocked", reason=terminal[1])
                    raise Blocked(terminal[1])
                obstacles.dismiss_cookie_banner(page, self.obstacle_cfg, self.fetch_cfg, self.emit, url)
                obstacles.dismiss_popup(page, self.obstacle_cfg, self.fetch_cfg, self.emit, url)
                terminal = obstacles.terminal_block(page, self.obstacle_cfg, self.emit, url)
                if terminal:
                    self._attempt(url, "browser", "blocked", reason=terminal[1])
                    raise Blocked(terminal[1])
                obstacles.paginate(page, self.obstacle_cfg, self.fetch_cfg, self.emit, url)
            except obstacles.TerminalBlock as exc:
                self._attempt(url, "browser", "blocked", reason=exc.reason)
                raise Blocked(exc.reason) from exc
            except obstacles.ClickNoOp as exc:
                self._attempt(url, "browser", "failed", reason=exc.reason)
                raise RuntimeError(exc.reason) from exc

            text = page.content()
            visible = page.inner_text("body")
            if any(x in visible.lower() for x in ("verify you are human", "complete the captcha",
                                                  "checking your browser")):
                self._attempt(url, "browser", "blocked", reason="browser access-control challenge")
                raise Blocked("Browser access-control challenge")
            if len(text.encode()) > self.cfg.max_body_bytes:
                self._attempt(url, "browser", "review_required", reason="rendered page exceeds size limit")
                raise ReviewRequired("Rendered page exceeds size limit")
            self._attempt(url, "browser", "success", status=200, content_type="text/html")
            return {"url": url, "final_url": page.url, "status": 200, "content_type": "text/html",
                    "text": text, "raw": "", "etag": None, "last_modified": None,
                    "fetcher": "browser", "visible_text": visible,
                    "scraped_at": datetime.now(timezone.utc).isoformat()}
        finally:
            if context is not None:
                context.close()
            if owns and session is not None:
                session.close()

    def links(self, fetched, plan):
        if "html" not in fetched.get("content_type", ""):
            return []
        tree = html.fromstring(fetched.get("text") or "<html/>")
        urls = []
        for href in tree.xpath("//a/@href")[:5000]:
            try:
                candidate = canonicalize(urljoin(fetched["final_url"], href))
                if host_of(candidate) in plan["allowed_domains"]:
                    urls.append(candidate)
            except ValueError:
                continue
        return list(dict.fromkeys(urls))

    def sitemap(self, url, plan):
        p = urlsplit(url)
        pending = [f"{p.scheme}://{p.netloc}/sitemap.xml"]
        seen, found = set(), []
        for _ in range(5):
            if not pending or len(found) >= plan["max_pages"]:
                break
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            result = self.http(current, plan["allowed_domains"])
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            tree = etree.fromstring(base64.b64decode(result["raw"]), parser)
            is_index = etree.QName(tree).localname == "sitemapindex"
            for loc in tree.xpath("//*[local-name()='loc']/text()")[:plan["max_pages"]]:
                try:
                    candidate = canonicalize(loc)
                    if host_of(candidate) in plan["allowed_domains"]:
                        (pending if is_index else found).append(candidate)
                except ValueError:
                    continue
        return list(dict.fromkeys(found))[:plan["max_pages"]]

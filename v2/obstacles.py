"""V1 obstacle handling, ported to V2's Playwright fetch path.

This is the V1 ``fetchers/browser_fetch.py`` policy layer, extracted so V2 can
reuse it without V1's FetchResult/FetchLogger types. Every function takes an
``emit`` callback in the shape of V2's EventLogger:

    emit("obstacle_detected", url=..., outcome=..., reason=..., details={...})

Obstacle configuration is still the versioned ``config/obstacles.json`` (with
``config/obstacles.schema.json`` validation done by the loader).
"""

from __future__ import annotations

import random
import re
import time
from typing import Callable, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from fetchers.config_loader import is_enabled, max_retries_for

Emit = Callable[..., None]

_CHALLENGE_FRAME_KEYWORDS = ("recaptcha", "hcaptcha", "captcha", "turnstile", "challenge")
_CHALLENGE_TEXT_KEYWORDS = (
    "verify you are human",
    "verify you're human",
    "unusual traffic",
    "just a moment",
    "checking your browser",
    "enter the captcha",
    "i'm not a robot",
    "im not a robot",
)
_AUTH_URL_RE = re.compile(r"/(login|signin|sign-in|logon)([/?#]|$)", re.IGNORECASE)
_AUTH_TEXT_KEYWORDS = ("sign in", "log in", "enter your password")

_CONSENT_KEYWORDS = ("cookie", "consent", "gdpr", "privacy policy", "we use cookies")
_CONSENT_REJECT = ("reject", "decline", "deny", "necessary only")
_CONSENT_ACCEPT = ("accept all", "accept", "agree", "allow all", "allow")
_CLOSE_TEXT = ("close", "dismiss", "ok", "×", "x", "✕", "✖")
_LOAD_MORE_RE = re.compile(r"load more|show more|view more|see more|more results", re.IGNORECASE)


class FetchFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TerminalBlock(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ClickNoOp(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _log(emit: Optional[Emit], event: str, **kwargs) -> None:
    if emit is not None:
        emit(event, **kwargs)


def _obstacle(emit: Optional[Emit], cfg: dict, url: str, obstacle: str, reason: str,
              details: Optional[dict] = None) -> None:
    entry = cfg.get(obstacle, {})
    payload = dict(details or {})
    payload.update({
        "obstacle": obstacle,
        "policy": entry.get("policy", ""),
        "detection_method": entry.get("detection_method", ""),
    })
    _log(emit, "obstacle_detected", url=url, outcome=payload["policy"] or "handled",
         reason=reason, details=payload)


def backoff_seconds(attempt: int, base: float, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            pass
    return min(30.0, base * (2 ** attempt)) + random.uniform(0, 0.5)


def visible_len(page) -> int:
    try:
        return len(re.sub(r"\s", "", page.locator("body").inner_text(timeout=3000)))
    except PlaywrightError:
        return 0


def terminal_block(page, cfg: dict, emit: Optional[Emit], url: str) -> Optional[tuple[str, str]]:
    if is_enabled(cfg, "captcha_gate"):
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            frame_url = (frame.url or "").lower()
            if any(key in frame_url for key in _CHALLENGE_FRAME_KEYWORDS):
                return ("captcha_gate", f"challenge widget frame detected: {frame.url}")
        try:
            text = page.locator("body").inner_text(timeout=4000).lower()
        except PlaywrightError:
            text = ""
        if any(key in text for key in _CHALLENGE_TEXT_KEYWORDS):
            return ("captcha_gate", "challenge text detected on page")
    if is_enabled(cfg, "session_expiry") or is_enabled(cfg, "random_logout"):
        auth = detect_auth_block(page, cfg, emit, url)
        if auth:
            return auth
    return None


def detect_auth_block(page, cfg: dict, emit: Optional[Emit], url: str) -> Optional[tuple[str, str]]:
    obstacle = "random_logout" if is_enabled(cfg, "random_logout") else "session_expiry"
    current = page.url or ""
    if _AUTH_URL_RE.search(current):
        return (obstacle, f"redirected to auth page: {current}")
    try:
        if page.locator("input[type=password]").count() > 0:
            return (obstacle, "password field present (auth-gated content)")
    except PlaywrightError:
        pass
    try:
        text = page.locator("body").inner_text(timeout=3000).lower()
    except PlaywrightError:
        text = ""
    if any(key in text for key in _AUTH_TEXT_KEYWORDS):
        return (obstacle, "auth challenge text detected")
    return None


def find_consent_banner(page, emit: Optional[Emit] = None, url: str = ""):
    for selector in ("[role=dialog]", "dialog", "[aria-modal=true]"):
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 5)
        except PlaywrightError:
            continue
        for index in range(count):
            element = locator.nth(index)
            try:
                if not element.is_visible():
                    continue
                text = element.inner_text(timeout=2000).lower()
            except PlaywrightError:
                continue
            if any(key in text for key in _CONSENT_KEYWORDS):
                return element
    return None


def find_popup(page):
    for selector in ("[aria-modal=true]", "[role=dialog]", "dialog"):
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 5)
        except PlaywrightError:
            continue
        for index in range(count):
            element = locator.nth(index)
            try:
                if element.is_visible():
                    return element
            except PlaywrightError:
                continue
    return None


def choose_dismiss_button(banner, kind: str):
    try:
        buttons = banner.locator("button")
        count = min(buttons.count(), 12)
    except PlaywrightError:
        return None
    for index in range(count):
        element = buttons.nth(index)
        try:
            text = (element.inner_text(timeout=1500) or "").strip().lower()
            aria = (element.get_attribute("aria-label") or "").lower()
        except PlaywrightError:
            continue
        label = f"{text} {aria}".strip()
        if not label:
            continue
        if kind == "cookie_banner":
            if any(piece in label for piece in _CONSENT_REJECT):
                return element
            if any(piece in label for piece in _CONSENT_ACCEPT):
                return element
        if any(piece in label for piece in _CLOSE_TEXT):
            return element
    return None


def safe_click(page, locator, cfg: dict, fetch_cfg: dict, emit: Optional[Emit], url: str,
               kind: str, change_threshold: int = 10, gone_locator=None) -> None:
    max_retries = max_retries_for(cfg, "blocked_clicks")
    base_backoff = float(fetch_cfg["backoff_base_seconds"])
    base_url = page.url
    base_len = visible_len(page)

    for attempt in range(max_retries + 1):
        try:
            locator.click(timeout=5000)
        except PlaywrightTimeoutError:
            _obstacle(emit, cfg, url, "dom_drift", "element not actionable, re-querying")
            if attempt >= max_retries:
                raise ClickNoOp(f"{kind}: element not actionable after {max_retries + 1} attempts")
            time.sleep(backoff_seconds(attempt, base_backoff, None))
            continue
        except PlaywrightError as exc:
            _obstacle(emit, cfg, url, "stale_element", f"stale element on {kind}: {exc.message}",
                      {"error": exc.message, "attempt": attempt + 1})
            if attempt >= max_retries:
                raise ClickNoOp(f"{kind}: stale element after {max_retries + 1} attempts")
            time.sleep(backoff_seconds(attempt, base_backoff, None))
            continue

        page.wait_for_timeout(700)
        terminal = terminal_block(page, cfg, emit, url)
        if terminal:
            raise TerminalBlock(terminal[1])

        changed = page.url != base_url or (visible_len(page) - base_len) > change_threshold
        if gone_locator is not None:
            try:
                gone = gone_locator.count() == 0 or not gone_locator.is_visible()
            except PlaywrightError:
                gone = True
            changed = changed or gone
        if changed:
            if attempt > 0:
                _obstacle(emit, cfg, url, "blocked_clicks", f"recovered on attempt {attempt + 1}",
                          {"attempt": attempt + 1, "outcome": "recovered"})
            return
        _obstacle(emit, cfg, url, "blocked_clicks",
                  f"click no-op (attempt {attempt + 1}/{max_retries + 1})",
                  {"attempt": attempt + 1, "max_retries": max_retries})
        if attempt >= max_retries:
            raise ClickNoOp(f"{kind}: click no-op after {max_retries + 1} attempts")
        time.sleep(backoff_seconds(attempt, base_backoff, None))


def dismiss_banner(page, banner, cfg: dict, fetch_cfg: dict, emit: Optional[Emit],
                   url: str, kind: str) -> None:
    target = choose_dismiss_button(banner, kind)
    if target is None:
        raise ClickNoOp(f"{kind}: no dismiss/accept button found on banner")
    safe_click(page, target, cfg, fetch_cfg, emit, url, kind,
               change_threshold=50, gone_locator=banner)


def dismiss_cookie_banner(page, cfg: dict, fetch_cfg: dict, emit: Optional[Emit], url: str) -> None:
    if not is_enabled(cfg, "cookie_banner"):
        return
    banner = find_consent_banner(page, emit, url)
    if banner is None:
        return
    _obstacle(emit, cfg, url, "cookie_banner", "cookie banner detected, dismissing")
    try:
        dismiss_banner(page, banner, cfg, fetch_cfg, emit, url, "cookie_banner")
    except ClickNoOp as exc:
        _obstacle(emit, cfg, url, "cookie_banner", str(exc), {"outcome": "dismiss_failed"})


def dismiss_popup(page, cfg: dict, fetch_cfg: dict, emit: Optional[Emit], url: str) -> None:
    if not is_enabled(cfg, "popup_modal"):
        return
    popup = find_popup(page)
    if popup is None:
        return
    _obstacle(emit, cfg, url, "popup_modal", "popup/modal overlay detected, dismissing")
    try:
        dismiss_banner(page, popup, cfg, fetch_cfg, emit, url, "popup_modal")
    except ClickNoOp as exc:
        _obstacle(emit, cfg, url, "popup_modal", str(exc), {"outcome": "dismiss_failed"})


def find_load_more(page):
    if not page:
        return None
    try:
        buttons = page.locator("button")
        count = min(buttons.count(), 20)
    except PlaywrightError:
        return None
    for index in range(count):
        element = buttons.nth(index)
        try:
            label = element.inner_text(timeout=1500).strip()
            if label and _LOAD_MORE_RE.search(label) and element.is_visible():
                return element
        except PlaywrightError:
            continue
    return None


def paginate(page, cfg: dict, fetch_cfg: dict, emit: Optional[Emit], url: str) -> None:
    if not is_enabled(cfg, "load_more"):
        return
    max_clicks = int(fetch_cfg.get("pagination_max_clicks", 5))
    for index in range(max_clicks):
        button = find_load_more(page)
        if button is None:
            return
        before = visible_len(page)
        try:
            safe_click(page, button, cfg, fetch_cfg, emit, url, "load_more", change_threshold=10)
        except ClickNoOp as exc:
            _obstacle(emit, cfg, url, "load_more", str(exc), {"click": index + 1})
            return
        after = visible_len(page)
        if after <= before + 10:
            _obstacle(emit, cfg, url, "load_more",
                      f"no new content after click {index + 1} (before={before}, after={after})",
                      {"click": index + 1})
            return
        _obstacle(emit, cfg, url, "load_more", f"loaded more content (+{after - before} chars)",
                  {"click": index + 1})


def settle(page, fetch_cfg: dict, emit: Optional[Emit], url: str) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightError:
        _obstacle(emit, {**fetch_cfg, "slow_responses": {"policy": "wait"}}, url,
                  "slow_responses", "networkidle timeout")
    page.wait_for_timeout(int(float(fetch_cfg["browser_settle_seconds"]) * 1000))
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
    except PlaywrightError:
        pass


def goto_with_retries(page, url: str, cfg: dict, fetch_cfg: dict,
                      emit: Optional[Emit], timeout_ms: int):
    max_429 = max_retries_for(cfg, "rate_limit")
    max_5xx = max_retries_for(cfg, "server_errors")
    base_backoff = float(fetch_cfg["backoff_base_seconds"])
    attempt = 0
    extended = False
    while True:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            if not extended:
                _obstacle(emit, cfg, url, "slow_responses", "navigation timed out, extending timeout")
                timeout_ms = int(float(fetch_cfg["extended_timeout_seconds"]) * 1000)
                extended = True
                continue
            raise FetchFailed("slow_responses: navigation timed out after extended timeout")
        except PlaywrightError as exc:
            raise FetchFailed(f"navigation error: {exc}")
        if response is None:
            raise FetchFailed("navigation returned no response")
        status = response.status
        if status == 429:
            retry_after = (response.headers or {}).get("retry-after")
            if attempt < max_429:
                delay = backoff_seconds(attempt, base_backoff, retry_after)
                _obstacle(emit, cfg, url, "rate_limit",
                          f"429, backing off {delay:.1f}s (attempt {attempt + 1})",
                          {"attempt": attempt + 1})
                time.sleep(delay)
                attempt += 1
                continue
            raise FetchFailed(f"rate_limit: exhausted {max_429 + 1} retries")
        if status and 500 <= status < 600:
            if attempt < max_5xx:
                delay = backoff_seconds(attempt, base_backoff, None)
                _obstacle(emit, cfg, url, "server_errors",
                          f"{status}, backing off {delay:.1f}s (attempt {attempt + 1})",
                          {"attempt": attempt + 1, "status": status})
                time.sleep(delay)
                attempt += 1
                continue
            raise FetchFailed(f"server_errors: exhausted {max_5xx + 1} retries (last {status})")
        return response


def log_redirect(request, emit: Optional[Emit], url: str) -> None:
    if request.redirected_from is not None:
        _log(emit, "obstacle_detected", url=url, outcome="follow_and_log",
             reason=f"redirect: {request.redirected_from.url} -> {request.url}",
             details={"obstacle": "unexpected_redirect", "policy": "follow_and_log",
                      "detection_method": "redirect_chain_detected"})

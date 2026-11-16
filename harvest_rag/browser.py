"""Playwright browser session shared by V2 workers (restores V1's shared browser)."""

from __future__ import annotations


class BrowserSession:
    """Lazily start one Chromium per worker process and reuse it across tasks."""

    def __init__(self):
        self._playwright = None
        self.browser = None

    def ensure(self):
        if self.browser is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self.browser = self._playwright.chromium.launch(headless=True)
        return self.browser

    def close(self) -> None:
        if self.browser is not None:
            try:
                self.browser.close()
            finally:
                self.browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            finally:
                self._playwright = None

    def __enter__(self):
        self.ensure()
        return self

    def __exit__(self, *args):
        self.close()

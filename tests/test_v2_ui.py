"""Browser wiring smoke test with intercepted service responses, no deployed services."""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def test_console_submit_plan_and_query(tmp_path):
    submitted = []
    errors = []
    html = (Path(__file__).resolve().parents[1] / "harvest_rag/index.html").read_text(encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            def respond(route):
                path = route.request.url.split("localhost:8123", 1)[-1]
                clean = path.split("?", 1)[0]
                if clean == "/":
                    route.fulfill(status=200, content_type="text/html", body=html)
                elif clean == "/health":
                    route.fulfill(json={"service": "harvest-rag", "status": "alive"})
                elif clean == "/metrics":
                    route.fulfill(status=200, content_type="text/plain",
                                  body='scraper_published_documents 1\nscraper_tasks{status="COMPLETED"} 1\n')
                elif clean == "/events/types":
                    route.fulfill(json=["fetch_attempt"])
                elif clean == "/jobs":
                    submitted.append(route.request.post_data_json)
                    route.fulfill(json={"job_id": "test-job-123"})
                elif clean == "/query":
                    route.fulfill(json={"evidence": [{"text": "Grounded result", "metadata": {"source_url": "https://example.com/"}}], "answer": None})
                elif clean == "/documents":
                    route.fulfill(json={"total": 1, "items": [{
                        "id": "d1", "url": "https://example.com/", "page_title": "Example",
                        "chunk_count": 1, "published_at": "2026-11-13T10:00:00+00:00"}]})
                elif clean.startswith("/documents/"):
                    route.fulfill(json={"document": {
                        "id": "d1", "url": "https://example.com/", "page_title": "Example",
                        "current_version": "v1", "collection_name": "test", "chunk_count": 1,
                        "published_at": "2026-11-13T10:00:00+00:00"},
                        "chunks": [{"text": "Grounded chunk", "version": "v1",
                                    "metadata": {"chunk_index": 1, "chunk_total": 1}}],
                        "total": 1})
                else:
                    route.fulfill(json=[])
            page.route("http://localhost:8123/**", respond)
            page.goto("http://localhost:8123/")
            page.locator("#urls").fill("https://example.com/")
            page.locator("#submit").click()
            page.wait_for_function("document.querySelector('#submitResult').textContent.includes('test-job-123')")
            assert submitted[0]["seed_urls"] == ["https://example.com/"]
            page.locator(".nav[data-tab='ask']").click()
            page.locator("#question").fill("What does the source say?")
            page.locator("#query").click()
            page.wait_for_function("document.querySelector('#queryResult').textContent.includes('Grounded result')")
            page.locator(".nav[data-tab='kb']").click()
            page.wait_for_selector(".kb-row")
            page.locator(".kb-row").first.click()
            page.wait_for_function("document.querySelectorAll('.chunk').length > 0")
            assert "Grounded chunk" in page.locator("#kbDetail").inner_text()
            assert not errors
            path = Path(os.environ.get("V2_UI_SCREENSHOT", str(tmp_path / "v2-console.png")))
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True)
        finally:
            browser.close()

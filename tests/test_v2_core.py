import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from harvest_rag.artifacts import ArtifactStore
from harvest_rag.config import Settings
from harvest_rag.db import LeaseLost
from harvest_rag.models import CrawlPlan, QueryRequest
from harvest_rag.urls import canonicalize, fingerprint, validate_target
from harvest_rag.worker import Worker


def test_url_identity_preserves_meaning_and_drops_tracking():
    assert canonicalize("HTTPS://Example.COM:443/a?lang=zh&utm_source=x&page=2#part") == "https://example.com/a?lang=zh&page=2"
    assert fingerprint("https://example.com/a#one") == fingerprint("https://example.com/a#two")
    assert fingerprint("https://example.com/a?page=1") != fingerprint("https://example.com/a?page=2")
    assert canonicalize("https://example.com/?a=2&a=1") == "https://example.com/?a=2&a=1"


@pytest.mark.parametrize("url", ["file:///tmp/file", "http://name:password@example.com/", "https:///bad"])
def test_invalid_urls_rejected(url):
    with pytest.raises(ValueError):
        canonicalize(url)


def test_plan_scope_budget_and_private_network():
    plan = CrawlPlan(seed_urls=["https://example.com/a", "https://example.com/a#x"], max_pages=1)
    assert len(plan.seed_urls) == 1
    with pytest.raises(ValueError):
        CrawlPlan(seed_urls=["https://a.example/", "https://b.example/"], max_pages=1)
    with pytest.raises(ValueError):
        validate_target("http://127.0.0.1/", ["127.0.0.1"])
    with pytest.raises(ValueError):
        validate_target("https://outside.example/", ["example.com"], True)


def test_artifacts_detect_corruption_and_traversal(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put({"text": "测试"})
    assert store.get(ref) == {"text": "测试"}
    assert ref == store.put({"text": "测试"})
    (tmp_path / ref["uri"]).write_text("corrupted")
    with pytest.raises(ValueError, match="checksum"):
        store.get(ref)
    with pytest.raises(ValueError, match="escapes"):
        store.get({"uri": "../outside.json", "hash": "x"})


class MemoryDB:
    def __init__(self):
        self.refs = {}
        self.document = None
        self.versions = {}
        self.state = None
        self.valid = True
        self.events = []
        self.published = 0

    def guard(self):
        if not self.valid:
            raise LeaseLost()

    def checkpoints(self, task): return self.refs.copy()
    def stage(self, task, stage): self.guard()
    def checkpoint(self, task, stage, ref):
        self.guard()
        self.refs[stage] = ref
    def invalidate_checkpoints(self, task, stages):
        self.guard()
        for name in stages: self.refs.pop(name, None)
    def current(self, doc): return self.document
    def finish(self, task, status, reason=None):
        self.guard()
        self.state = status
    def checked(self, task, fetch): self.guard()
    def event(self, task_id, event, details): self.events.append((event, details))
    def stage_version(self, task, version, manifest_ref, count, collection, content_hash, page_title=None):
        self.guard()
        self.versions[version] = {"manifest": manifest_ref, "content_hash": content_hash,
                                  "processing_key": task["processing_key"], "collection_name": collection}
    def publish(self, task, version, fetch):
        self.guard()
        self.document = {**self.versions[version], "current_version": version, "fetcher": fetch["fetcher"]}
        self.published += 1
        return True


class FakeFetcher:
    def __init__(self):
        self.calls = 0
        self.content = "Reliable ingestion preserves source evidence and resumes after an interrupted worker. " * 5
        self.not_modified = False

    def fetch(self, url, plan, previous=None):
        self.calls += 1
        return {"url": url, "final_url": url, "status": 304 if previous and self.not_modified else 200,
                "text": self.content, "content_type": "text/plain", "raw": "", "fetcher": "http",
                "scraped_at": "2026-11-12T10:00:00+00:00", "etag": '"v1"'}


class FakeEmbedder:
    model_name = "test-embedder"
    dimension = 3
    def __init__(self): self.calls = 0
    def embed(self, texts):
        self.calls += 1
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeStore:
    collection = "test_collection"
    def __init__(self):
        self.rows = {}
        self.fail = False
        self.on_write = lambda: None
    def write(self, version, rows, vectors):
        self.rows[version] = copy.deepcopy(rows)
        self.on_write()
        if self.fail:
            self.fail = False
            raise RuntimeError("crash after index write")
    def contains(self, version, expected): return len(self.rows.get(version, [])) == expected


def make_worker(tmp_path):
    cfg = Settings(artifact_dir=tmp_path, embedding_model="test-embedder", max_chunk_chars=200)
    plan = CrawlPlan(seed_urls=["https://example.com/article"])
    task = {"id": "task-1", "url": plan.seed_urls[0], "url_hash": fingerprint(plan.seed_urls[0]),
            "plan": plan.model_dump(), "processing_key": cfg.processing_key(plan), "depth": 0,
            "created_at": datetime.now(timezone.utc), "lease_token": "token", "attempts": 1}
    db, store, embedder, fetcher = MemoryDB(), FakeStore(), FakeEmbedder(), FakeFetcher()
    worker = Worker(cfg, db, store, embedder, fetcher=fetcher)
    return worker, task, db, store, embedder, fetcher


def test_resume_after_index_crash_reuses_fetch_and_embeddings(tmp_path):
    worker, task, db, store, embedder, fetcher = make_worker(tmp_path)
    store.fail = True
    with pytest.raises(RuntimeError, match="crash"):
        worker.process(task)
    assert db.document is None
    assert "EMBED" in db.refs
    worker.process(task)
    assert db.state == "COMPLETED"
    assert fetcher.calls == 1 and embedder.calls == 1
    assert len(store.rows) == 1


def test_fewer_chunks_get_new_version_and_old_version_is_not_current(tmp_path):
    worker, task, db, store, embedder, fetcher = make_worker(tmp_path)
    worker.process(task)
    old_version = db.document["current_version"]
    old_count = len(store.rows[old_version])
    task["id"] = "task-2"
    db.refs.clear()
    fetcher.content = "Changed source content that is long enough for ingestion but fits inside one chunk."
    worker.process(task)
    current = db.document["current_version"]
    assert current != old_version
    assert len(store.rows[current]) == 1 < old_count


def test_expired_worker_cannot_publish_after_external_write(tmp_path):
    worker, task, db, store, embedder, fetcher = make_worker(tmp_path)
    store.on_write = lambda: setattr(db, "valid", False)
    with pytest.raises(LeaseLost):
        worker.process(task)
    assert store.rows and db.document is None


def test_corrupted_checkpoint_recomputes_downstream(tmp_path):
    worker, task, db, store, embedder, fetcher = make_worker(tmp_path)
    store.fail = True
    with pytest.raises(RuntimeError): worker.process(task)
    (tmp_path / db.refs["EMBED"]["uri"]).write_text("corrupt")
    worker.process(task)
    assert embedder.calls == 2 and fetcher.calls == 1
    assert db.state == "COMPLETED"


def test_304_skips_only_when_published_index_is_available(tmp_path):
    worker, task, db, store, embedder, fetcher = make_worker(tmp_path)
    worker.process(task)
    db.refs.clear()
    task["id"] = "task-2"
    fetcher.not_modified = True
    worker.process(task)
    assert db.state == "UNCHANGED" and embedder.calls == 1
    store.rows.clear()
    worker.process(task)
    assert db.state == "COMPLETED" and embedder.calls == 2


def test_config_change_requires_new_job(tmp_path):
    worker, task, db, store, embedder, fetcher = make_worker(tmp_path)
    worker.cfg.max_chunk_chars = 250
    worker.process(task)
    assert db.state == "FAILED" and fetcher.calls == 0


def test_bad_content_is_retained_for_review(tmp_path):
    from harvest_rag.fetch import ReviewRequired
    worker, task, db, store, embedder, fetcher = make_worker(tmp_path)
    fetcher.content = "empty"
    with pytest.raises(ReviewRequired): worker.process(task)
    assert "PARSE" in db.refs and not store.rows


def test_planner_rejects_hallucinated_source():
    from harvest_rag.planner import discover
    client = SimpleNamespace(complete_structured=lambda **_: {"urls": ["https://invented.example/"]})
    with pytest.raises(ValueError, match="not present"):
        discover("Python docs", search=lambda _: [{"url": "https://docs.python.org/", "title": "Python"}], client=client)


def test_retrieval_filters_retired_versions_before_rerank():
    from harvest_rag.retrieval import Retriever
    responses = iter([["old", "new"], ["new"]])
    db = SimpleNamespace(active_versions=lambda *args: next(responses))
    store = SimpleNamespace(collection="test", search=lambda *args: [
        {"version": "old", "text": "stale"}, {"version": "new", "text": "current"}])
    r = Retriever(Settings(reranker_enabled=False), db, store, FakeEmbedder())
    assert r.query(QueryRequest(question="test"))["evidence"] == [{"version": "new", "text": "current"}]


def test_api_auth_and_plan_validation(monkeypatch):
    from fastapi.testclient import TestClient
    from harvest_rag import api
    monkeypatch.setattr(api, "settings", lambda: Settings(api_key="test-secret"))
    client = TestClient(api.app)  # no lifespan: no external services
    assert client.get("/health").status_code == 200
    assert client.get("/jobs").status_code == 401
    assert client.post("/jobs", headers={"X-API-Key": "test-secret"}, json={"seed_urls": ["file:///bad"]}).status_code == 422


def test_chroma_version_adapter_preserves_old_until_publication(tmp_path):
    from harvest_rag.store import ChromaVersionStore
    store = ChromaVersionStore(tmp_path / "chroma", "test", 3)
    store.write("old", [{"content": "old source"}], [[1., 0., 0.]])
    store.write("new", [{"content": "new source"}], [[1., 0., 0.]])
    assert store.contains("old", 1) and store.contains("new", 1)
    assert [r["text"] for r in store.search("source", [1., 0., 0.], ["new"], 5)] == ["new source"]


def test_parse_passes_browser_visible_text_like_v1(monkeypatch):
    """V2 matches V1's call site: parse() forwards the browser's visible text."""
    from types import SimpleNamespace
    import harvest_rag.content as content

    seen = {}

    def fake_strip(html, *, url=None, browser_visible_text=None, **kwargs):
        seen.update(html=html, url=url, visible=browser_visible_text)
        return SimpleNamespace(text="正文", title="标题", blocks=[])

    monkeypatch.setattr(content, "strip_html", fake_strip)
    fetched = {"url": "https://example.com/app", "content_type": "text/html",
               "text": "<html><body><div id='root'></div></body></html>",
               "visible_text": "浏览器渲染后的可见正文",
               "scraped_at": "2026-11-13T10:00:00+00:00"}
    parsed = content.parse(fetched)
    assert seen["visible"] == "浏览器渲染后的可见正文"
    assert parsed["text"] == "正文"


def test_quality_rejects_css_js_text():
    from harvest_rag.content import quality
    from harvest_rag.fetch import ReviewRequired
    with pytest.raises(ReviewRequired, match="CSS/JS"):
        quality({"text": '@charset "UTF-8";' + ".a{color:red}" * 100}, 10)


def test_retrieval_drops_low_rerank_scores():
    from harvest_rag.retrieval import Retriever

    db = SimpleNamespace(active_versions=lambda *args: ["v"])
    store = SimpleNamespace(collection="test", search=lambda *args: [
        {"version": "v", "text": "relevant", "metadata": {"source_url": "https://x/"}},
        {"version": "v", "text": "noise", "metadata": {"source_url": "https://x/"}},
    ])

    class Reranker:
        def predict(self, pairs):
            return [0.9, 0.01]

    keep = Retriever(Settings(reranker_enabled=True, rerank_min_score=0.2),
                     db, store, FakeEmbedder(), reranker=Reranker())
    assert [row["text"] for row in keep.query(QueryRequest(question="q"))["evidence"]] == ["relevant"]

    drop = Retriever(Settings(reranker_enabled=True, rerank_min_score=0.95),
                     db, store, FakeEmbedder(), reranker=Reranker())
    result = drop.query(QueryRequest(question="q"))
    assert result["evidence"] == [] and "relevant" in result["message"]


def test_event_logger_never_fails_the_task():
    from harvest_rag.worker import EventLogger

    class BrokenDB:
        def event(self, *args, **kwargs):
            raise RuntimeError("database down")

    EventLogger(BrokenDB(), "task-1").log_event("fetch_attempt", url="https://example.com/")


def test_site_config_uses_v1_files():
    from harvest_rag.site_config import fetch_config, obstacle_config
    assert fetch_config()["user_agent"]
    assert obstacle_config()["cookie_banner"]["enabled"] is True
    assert obstacle_config()["load_more"]["policy"] == "paginate"


def test_api_cancel_delete_clear_endpoints(monkeypatch):
    from fastapi.testclient import TestClient
    from harvest_rag import api

    class FakeDB:
        def initialize(self): pass
        def cancel_job(self, job_id): return 3
        def clear_events(self): return 4
        def delete_url(self, url, delete_version): return {"deleted": True, "versions": 1, "tasks": 2, "events": 5}
        def recent_events(self, limit=200, events=None, url=None, task_id=None):
            return [{"event": "fetch_attempt", "details": {"url": url}, "limit": limit,
                     "events": events}]
        def event_types(self): return ["fetch_attempt", "published"]

    class FakeStore:
        def delete_version(self, version, collection=None): pass

    monkeypatch.setattr(api, "database", lambda: FakeDB())
    monkeypatch.setattr(api, "components", lambda: (FakeStore(), None))
    monkeypatch.setattr(api, "settings", lambda: Settings(api_key="test-secret"))
    client = TestClient(api.app)
    headers = {"X-API-Key": "test-secret"}

    assert client.post("/jobs/abc/cancel", headers=headers).json() == {"cancelled": 3}
    assert client.delete("/events", headers=headers).json() == {"deleted": 4}
    deleted = client.delete("/documents?url=https://example.com/", headers=headers).json()
    assert deleted["deleted"] is True and deleted["versions"] == 1
    filtered = client.get("/events?url=example.com&event_type=fetch_attempt&limit=10",
                          headers=headers).json()
    assert filtered[0]["limit"] == 10 and filtered[0]["events"] == ["fetch_attempt"]
    assert client.get("/events/types", headers=headers).json() == ["fetch_attempt", "published"]


def test_api_documents_and_chunks(monkeypatch):
    from fastapi.testclient import TestClient
    from harvest_rag import api

    class FakeDB:
        def initialize(self): pass
        def document_count(self, q=None): return 1
        def documents(self, limit=50, offset=0, q=None):
            return [{"id": "d1", "url": "https://example.com/", "page_title": "Example",
                     "chunk_count": 1, "published_at": "2026-11-13T10:00:00+00:00"}]
        def document(self, document_id):
            if document_id != "d1":
                return None
            return {"id": "d1", "url": "https://example.com/", "page_title": "Example",
                    "current_version": "v1", "collection_name": "test",
                    "chunk_count": 1, "published_at": "2026-11-13T10:00:00+00:00"}

    monkeypatch.setattr(api, "database", lambda: FakeDB())
    monkeypatch.setattr(api, "settings", lambda: Settings(api_key="test-secret"))
    monkeypatch.setattr(api, "_read_chunks",
                        lambda collection, version, limit, offset: [
                            {"text": "Grounded chunk", "version": version,
                             "metadata": {"chunk_index": 1, "chunk_total": 1}}])
    client = TestClient(api.app)
    headers = {"X-API-Key": "test-secret"}

    listing = client.get("/documents?q=Example", headers=headers).json()
    assert listing["total"] == 1 and listing["items"][0]["page_title"] == "Example"
    detail = client.get("/documents/d1/chunks", headers=headers).json()
    assert detail["chunks"][0]["text"] == "Grounded chunk"
    assert client.get("/documents/missing/chunks", headers=headers).status_code == 404


def test_query_defaults_and_retriever_warmup():
    from harvest_rag.retrieval import Retriever

    req = QueryRequest(question="test")
    assert req.candidates == 15

    calls = []

    class FakeReranker:
        def predict(self, pairs):
            calls.append(("rerank", pairs))
            return [0.9 for _ in pairs]

    class SpyEmbedder(FakeEmbedder):
        def embed(self, texts):
            calls.append(("embed", list(texts)))
            return super().embed(texts)

    r = Retriever(Settings(reranker_enabled=True), SimpleNamespace(), SimpleNamespace(collection="t"),
                  SpyEmbedder(), reranker=FakeReranker())
    r.warmup()
    assert calls == [("embed", ["warmup"])]
    r.reranker = None
    # Verify warmup is safe even when reranker_enabled=False
    r2 = Retriever(Settings(reranker_enabled=False), SimpleNamespace(), SimpleNamespace(collection="t"),
                   SpyEmbedder())
    r2.warmup()
    assert calls[-1] == ("embed", ["warmup"])

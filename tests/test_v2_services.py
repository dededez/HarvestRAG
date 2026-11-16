"""Opt-in integration checks. Use disposable services; no Benchmark workload."""
import os
from uuid import uuid4

import pytest
from v2.config import Settings
from v2.db import Database, LeaseLost
from v2.models import CrawlPlan


@pytest.fixture
def pg():
    dsn = os.getenv("V2_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set V2_TEST_DATABASE_URL for PostgreSQL integration checks")
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg import sql
    schema = "test_v2_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db = Database(make_conninfo(dsn, options=f"-csearch_path={schema}"))
    db.initialize()
    try:
        yield db
    finally:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_claim_recovery_and_fencing(pg):
    plan = CrawlPlan(seed_urls=["https://example.com/"])
    pg.create_job(plan, "key")
    first = pg.claim("one", 60, 3)
    assert pg.claim("two", 60, 3) is None
    with pg.connect() as c:
        c.execute("UPDATE v2_tasks SET lease_until=now()-interval '1 second'")
    second = pg.claim("two", 60, 3)
    assert second["id"] == first["id"] and second["lease_token"] != first["lease_token"]
    with pytest.raises(LeaseLost): pg.checkpoint(first, "FETCH", {"hash": "x", "uri": "x"})
    pg.checkpoint(second, "FETCH", {"hash": "x", "uri": "x"})
    pg.finish(second, "COMPLETED")
    assert pg.tasks()[0]["status"] == "COMPLETED"


def test_postgres_budget_is_atomic_across_workers(pg):
    from concurrent.futures import ThreadPoolExecutor
    plan = CrawlPlan(seed_urls=["https://example.com/a", "https://example.com/b"], max_pages=5, max_depth=1)
    pg.create_job(plan, "key")
    tasks = [pg.claim("one", 60, 3), pg.claim("two", 60, 3)]
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda t: pg.enqueue_links(t, [f"https://example.com/{i}" for i in range(20)]), tasks))
    assert len(pg.tasks()) == 5


def test_postgres_publish_and_older_job_cannot_replace_newer(pg):
    plans = [CrawlPlan(seed_urls=["https://example.com/a"]) for _ in range(2)]
    for plan in plans: pg.create_job(plan, "key")
    old, new = pg.claim("one", 60, 3), pg.claim("two", 60, 3)
    pg.stage_version(new, "new", {"hash": "n", "uri": "n"}, 1, "test", "hash")
    assert pg.publish(new, "new", {"fetcher": "http"})
    pg.stage_version(old, "old", {"hash": "o", "uri": "o"}, 1, "test", "hash")
    assert not pg.publish(old, "old", {"fetcher": "http"})
    assert pg.active_versions("test") == ["new"]
    assert pg.retired()[0]["id"] == "old"


def test_postgres_cancel_and_delete_url(pg):
    plan = CrawlPlan(seed_urls=["https://example.com/cancel"])
    job = pg.create_job(plan, "key")
    task = pg.claim("worker", 60, 3)
    pg.stage_version(task, "v1", {"hash": "h", "uri": "u"}, 1, "test", "hash")
    assert pg.publish(task, "v1", {"fetcher": "http"})

    assert pg.cancel_job(job) == 1
    assert pg.tasks(job)[0]["status"] == "BLOCKED"

    deleted = []
    outcome = pg.delete_url(plan.seed_urls[0], lambda v, c: deleted.append((v, c)))
    assert outcome == {"deleted": True, "versions": 1, "tasks": 1, "events": 0}
    assert deleted == [("v1", "test")]
    assert pg.tasks(job) == []


def test_milvus_dense_bm25_roundtrip():
    uri = os.getenv("V2_TEST_MILVUS_URI")
    if not uri:
        pytest.skip("Set V2_TEST_MILVUS_URI for Milvus integration checks")
    from types import SimpleNamespace
    from v2.store import MilvusStore
    cfg = Settings(milvus_uri=uri, collection_prefix="test_" + uuid4().hex[:12])
    store = MilvusStore(cfg, SimpleNamespace(model_name="test", dimension=4))
    try:
        store.ensure()
        store.write("version1", [{"content": "PostgreSQL durable task queue", "source_url": "https://example.com/"}], [[1., 0., 0., 0.]])
        store.write("version2", [{"content": "Milvus hybrid search", "source_url": "https://example.com/"}], [[0., 1., 0., 0.]])
        assert store.contains("version1", 1)
        rows = store.search("hybrid", [0., 1., 0., 0.], ["version2"], 5)
        assert rows and all(r["version"] == "version2" for r in rows)
        store.delete_version("version1")
        assert not store.contains("version1", 1)
    finally:
        store.client.drop_collection(store.collection)

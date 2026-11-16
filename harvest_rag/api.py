from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, Response
from harvest_rag.models import CrawlPlan, QueryRequest, DiscoverRequest
from harvest_rag.runtime import settings, database, retriever, components
from harvest_rag import maintenance


def _warmup_background():
    try:
        retriever().warmup()
        logging.getLogger(__name__).info("API retrieval models warmed up")
    except Exception as exc:
        logging.getLogger(__name__).warning("API model warmup skipped: %s", exc)


@asynccontextmanager
async def lifespan(app):
    database().initialize()
    if os.environ.get("V2_DISABLE_WARMUP", "").lower() not in {"1", "true", "yes"}:
        threading.Thread(target=_warmup_background, name="v2-model-warmup", daemon=True).start()
    yield


app = FastAPI(title="HarvestRAG", version="2.0.0", lifespan=lifespan)


def authorize(x_api_key: str = Header(default=""), authorization: str = Header(default="")):
    key = settings().api_key
    supplied = x_api_key or (authorization[7:] if authorization.startswith("Bearer ") else "")
    if key and not hmac.compare_digest(key, supplied):
        raise HTTPException(401, "Invalid API key")


def service_call(operation):
    try:
        return operation()
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logging.exception("V2 service operation failed")
        # Connection strings and provider errors must not leak into API responses.
        raise HTTPException(503, "Service unavailable; run /ready to check PostgreSQL/Milvus") from exc


@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"service": "harvest-rag", "status": "alive"}


@app.get("/ready", dependencies=[Depends(authorize)])
def ready():
    def check():
        with database().connect() as c:
            c.execute("SELECT 1")
        from pymilvus import MilvusClient
        client = MilvusClient(uri=settings().milvus_uri, token=settings().milvus_token, timeout=5)
        try:
            client.list_collections()
        finally:
            client.close()
        return {"postgresql": "ready", "milvus": "ready", "models": "loaded on first use"}
    return service_call(check)


@app.post("/plans/discover", dependencies=[Depends(authorize)])
def discover_plan(request: DiscoverRequest):
    from harvest_rag.planner import discover
    return service_call(lambda: discover(
        request.goal, request.max_pages, request.max_depth,
        multi_query=request.multi_query,
    ).model_dump())


@app.post("/jobs", dependencies=[Depends(authorize)])
def submit(plan: CrawlPlan):
    return service_call(lambda: {"job_id": database().create_job(plan, settings().processing_key(plan))})


@app.get("/jobs", dependencies=[Depends(authorize)])
def jobs():
    return service_call(database().jobs)


def _read_chunks(collection: str, version: str, limit: int, offset: int):
    """Read one version's chunks directly from Milvus (no embedder needed)."""
    from pymilvus import MilvusClient

    cfg = settings()
    client = MilvusClient(uri=cfg.milvus_uri, token=cfg.milvus_token, timeout=10)
    try:
        rows = client.query(
            collection,
            filter="version == " + json.dumps(version),
            output_fields=["text", "metadata", "version"],
            limit=max(1, min(int(limit), 500)),
            offset=max(0, int(offset)),
            consistency_level="Strong",
        )
    finally:
        client.close()
    for row in rows:
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            try:
                row["metadata"] = json.loads(metadata)
            except (TypeError, ValueError):
                row["metadata"] = {}
    rows.sort(key=lambda row: (row.get("metadata") or {}).get("chunk_index", 0))
    return rows


@app.get("/documents", dependencies=[Depends(authorize)])
def documents(limit: int = 50, offset: int = 0, q: str | None = None):
    return service_call(lambda: {"total": database().document_count(q),
                                 "items": database().documents(limit, offset, q)})


@app.get("/documents/{document_id}/chunks", dependencies=[Depends(authorize)])
def document_chunks(document_id: str, limit: int = 200, offset: int = 0):
    def run():
        doc = database().document(document_id)
        if not doc:
            raise HTTPException(404, "Document not found")
        chunks = _read_chunks(doc["collection_name"], doc["current_version"], limit, offset)
        return {"document": doc, "chunks": chunks, "total": doc["chunk_count"]}
    return service_call(run)


@app.get("/tasks", dependencies=[Depends(authorize)])
def tasks(job_id: str | None = None, review_only: bool = False):
    return service_call(lambda: database().tasks(job_id, review_only))


@app.post("/tasks/{task_id}/retry", dependencies=[Depends(authorize)])
def retry(task_id: str):
    return service_call(lambda: {"requeued": database().requeue(task_id)})


@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(authorize)])
def cancel(job_id: str):
    return service_call(lambda: {"cancelled": database().cancel_job(job_id)})


@app.delete("/documents", dependencies=[Depends(authorize)])
def delete_document(url: str):
    """Delete one URL's tasks, versions, vectors and events (V1 Delete URL)."""
    return service_call(lambda: maintenance.delete_url(database(), components()[0], url))


@app.delete("/events", dependencies=[Depends(authorize)])
def clear_events():
    return service_call(lambda: {"deleted": database().clear_events()})


@app.get("/events", dependencies=[Depends(authorize)])
def events(limit: int = 200, event_type: str | None = None,
           url: str | None = None, task_id: str | None = None):
    types = [part.strip() for part in event_type.split(",") if part.strip()] if event_type else None
    return service_call(lambda: database().recent_events(limit=limit, events=types, url=url, task_id=task_id))


@app.get("/events/types", dependencies=[Depends(authorize)])
def event_types():
    return service_call(database().event_types)


@app.post("/query", dependencies=[Depends(authorize)])
def query(request: QueryRequest):
    def run():
        result = retriever().query(request)
        try:
            database().event(None, "answer", {"question": request.question[:200],
                                              "evidence": len(result.get("evidence", [])),
                                              "answered": bool(result.get("answer"))})
        except Exception:
            pass
        return result
    return service_call(run)


@app.post("/live", dependencies=[Depends(authorize)])
def live(request: QueryRequest):
    if not request.source_url:
        raise HTTPException(400, "source_url is required for live queries")
    def run():
        found = retriever().query(request)
        if found["evidence"]:
            return {**found, "source": "corpus"}
        from harvest_rag.fetch import Fetcher
        from harvest_rag.content import parse, quality, structured_result
        plan = CrawlPlan(seed_urls=[request.source_url], max_pages=1)
        fetched = Fetcher(settings(), database()).fetch(plan.seed_urls[0], plan.model_dump())
        parsed = parse(fetched)
        quality(parsed, settings().min_text_chars)
        result = structured_result(parsed, fetched)
        evidence = [{"text": s.content, "metadata": {"source_url": result.source_url,
                    "scrape_timestamp": result.scrape_timestamp.isoformat(), "page_title": result.page_title,
                    "section_heading": s.heading}} for s in result.sections]
        answer = None
        message = "Fresh evidence only; submit an ingestion Job to add it to the corpus"
        if request.answer and evidence:
            from harvest_rag.retrieval import answer_evidence
            answer, answer_message = answer_evidence(request.question, evidence)
            message = answer_message or message
        try:
            database().event(None, "answer", {"question": request.question[:200],
                                              "evidence": len(evidence),
                                              "answered": bool(answer),
                                              "source": "live",
                                              "url": result.source_url})
        except Exception:
            pass
        return {"source": "live", "persisted": False, "evidence": evidence, "answer": answer,
                "message": message}
    return service_call(run)


@app.get("/metrics", dependencies=[Depends(authorize)])
def metrics():
    from prometheus_client import CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST
    registry = CollectorRegistry()
    tasks = Gauge("scraper_tasks", "Durable tasks by status", ["status"], registry=registry)
    documents = Gauge("scraper_published_documents", "Documents with published versions", registry=registry)
    with database().connect() as c:
        for row in c.execute("SELECT status,count(*) n FROM v2_tasks GROUP BY status").fetchall():
            tasks.labels(row["status"]).set(row["n"])
        documents.set(c.execute("SELECT count(*) n FROM v2_documents WHERE current_version IS NOT NULL").fetchone()["n"])
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

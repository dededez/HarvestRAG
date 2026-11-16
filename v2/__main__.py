from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from v2.models import CrawlPlan, QueryRequest
from v2.runtime import settings, database, components, retriever

TERMINAL = {"COMPLETED", "UNCHANGED", "BLOCKED", "REVIEW_REQUIRED", "FAILED", "DEAD_LETTER", "CANCELLED"}


def _read_urls(path: Path) -> list[str]:
    urls = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def _run_batch(args) -> None:
    urls = _read_urls(args.urls_file)
    if not urls:
        print("no URLs found in file")
        return
    cfg, db = settings(), database()
    db.initialize()
    if args.reset:
        print(f"reset_tasks={db.delete_tasks_for_urls(urls)}")
    plan = CrawlPlan(seed_urls=urls, max_pages=len(urls), max_depth=0,
                     discovery_strategy="seeds", extraction_strategy=args.extract)
    job_id = db.create_job(plan, cfg.processing_key(plan))
    print(job_id)
    if args.workers <= 0:
        return
    procs = [subprocess.Popen([sys.executable, "-m", "v2", "worker"]) for _ in range(args.workers)]
    try:
        while True:
            states = {row["status"] for row in db.tasks(job_id)}
            if states and states <= TERMINAL:
                break
            time.sleep(2)
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _fetch(args) -> None:
    from v2.fetch import Fetcher
    cfg, db = settings(), database()
    plan = CrawlPlan(seed_urls=[args.url], max_pages=1)
    result = Fetcher(cfg, db).fetch(plan.seed_urls[0], plan.model_dump())
    summary = {key: result.get(key) for key in ("url", "final_url", "status", "content_type",
                                                "fetcher", "etag")}
    summary["text_chars"] = len(result.get("text") or "")
    summary["visible_text_chars"] = len(result.get("visible_text") or "")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _strip(args) -> None:
    from pipeline.strip import strip_html
    html = Path(args.path).read_text(encoding="utf-8-sig", errors="replace")
    result = strip_html(html, url=args.url)
    print(f"outcome={result.outcome.value} method={result.method} "
          f"chars={result.chars_before}->{result.chars_after} blocks={result.num_blocks}")
    print(result.text[: args.chars])


def _extract(args) -> None:
    from agents.config_loader import load_agent_config
    from agents.extractor import extract_content, mime_to_content_type
    from agents.llm.client import LiteLLMClient
    from schemas.extraction import ContentType

    client = LiteLLMClient(load_agent_config())
    if args.file:
        data = Path(args.file).read_bytes()
        ctype = {"html": ContentType.HTML, "text": ContentType.TEXT,
                 "pdf": ContentType.PDF}.get(args.content_type, ContentType.TEXT)
        content = data.decode("utf-8", errors="replace") if ctype != ContentType.PDF else data
        result = extract_content(content, content_type=ctype, source_url=args.url,
                                 page_title=None, mode=args.mode, client=client,
                                 agent_cfg=load_agent_config())
    else:
        from v2.fetch import Fetcher
        cfg, db = settings(), database()
        plan = CrawlPlan(seed_urls=[args.url], max_pages=1)
        fetched = Fetcher(cfg, db).fetch(plan.seed_urls[0], plan.model_dump())
        result = extract_content(fetched["text"], content_type=mime_to_content_type(fetched["content_type"]),
                                 source_url=fetched["url"], page_title=None, mode=args.mode,
                                 client=client, agent_cfg=load_agent_config())
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


def _live(args) -> None:
    from v2.content import parse, quality, structured_result
    from v2.fetch import Fetcher
    from v2.retrieval import answer_evidence
    from v2.urls import canonicalize

    cfg, db = settings(), database()
    url = canonicalize(args.url)
    request = QueryRequest(question=args.question, k=args.k, answer=args.answer, source_url=url)
    found = retriever().query(request)
    if found["evidence"]:
        found["source"] = "corpus"
        print(json.dumps(found, ensure_ascii=False, indent=2, default=str))
        return
    plan = CrawlPlan(seed_urls=[url], max_pages=1)
    fetched = Fetcher(cfg, db).fetch(plan.seed_urls[0], plan.model_dump())
    parsed = parse(fetched)
    quality(parsed, cfg.min_text_chars)
    result = structured_result(parsed, fetched)
    evidence = [{"text": section.content,
                 "metadata": {"source_url": result.source_url,
                              "scrape_timestamp": result.scrape_timestamp.isoformat(),
                              "page_title": result.page_title,
                              "section_heading": section.heading}}
                for section in result.sections]
    answer = None
    message = "Fresh evidence only; submit an ingestion Job to add it to the corpus"
    if args.answer and evidence:
        answer, message = answer_evidence(args.question, evidence)
    print(json.dumps({"source": "live", "persisted": False, "evidence": evidence,
                      "answer": answer, "message": message}, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="HarvestRAG")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Apply idempotent PostgreSQL schema")
    submit = sub.add_parser("submit", help="Submit manual URLs or a saved JSON plan")
    submit.add_argument("urls", nargs="*")
    submit.add_argument("--plan", type=Path)
    submit.add_argument("--max-pages", type=int, default=50)
    submit.add_argument("--depth", type=int, default=0)
    submit.add_argument("--discover", choices=["seeds", "links", "sitemap", "links+sitemap"], default="seeds")
    submit.add_argument("--extract", choices=["structured", "llm"], default="structured")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("status")
    sub.add_parser("cleanup", help="Delete retired vectors only; retain source artifacts and version history")
    retry = sub.add_parser("retry")
    retry.add_argument("task_id")
    search = sub.add_parser("query")
    search.add_argument("question")
    search.add_argument("--answer", action="store_true")
    plan = sub.add_parser("plan")
    plan.add_argument("goal")
    plan.add_argument("--output", type=Path, required=True)
    migrate = sub.add_parser("import-v1", help="Queue source URLs from a read-only V1 SQLite queue for fresh V2 ingestion")
    migrate.add_argument("queue_db", type=Path)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    fetch = sub.add_parser("fetch", help="Fetch one URL (static first, then Playwright with V1 obstacles)")
    fetch.add_argument("url")
    strip = sub.add_parser("strip", help="Run the HTML stripper on a local file")
    strip.add_argument("path")
    strip.add_argument("--url", default=None)
    strip.add_argument("--chars", type=int, default=1000)
    extract = sub.add_parser("extract", help="LLM extraction from a URL or local file")
    extract.add_argument("--url", required=True)
    extract.add_argument("--file")
    extract.add_argument("--content-type", choices=["html", "text", "pdf"], default="html")
    extract.add_argument("--mode", choices=["batch", "single"], default="batch")
    chunk = sub.add_parser("chunk", help="Chunk a saved ExtractionResult JSON (V1 pipeline chunk)")
    chunk.add_argument("file")
    chunk.add_argument("--max-chars", type=int, default=2000)
    live = sub.add_parser("live", help="Per-URL live query: corpus first, then fresh scrape")
    live.add_argument("url")
    live.add_argument("--question", default="这个页面讲了什么？")
    live.add_argument("--answer", action="store_true")
    live.add_argument("--k", type=int, default=5)
    delete = sub.add_parser("delete", help="Delete one URL from tasks, versions, vectors and events")
    delete.add_argument("url")
    events = sub.add_parser("events", help="Show or clear pipeline events")
    events.add_argument("--clear", action="store_true")
    events.add_argument("--event", help="Event type filter (comma-separated)")
    events.add_argument("--url", help="URL fragment filter")
    events.add_argument("--limit", type=int, default=200)
    cancel = sub.add_parser("cancel", help="Cancel a Job and stop its non-terminal tasks")
    cancel.add_argument("job_id")
    run = sub.add_parser("run", help="V1-style batch run over a URL file")
    run.add_argument("urls_file", type=Path)
    run.add_argument("--reset", action="store_true")
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--extract", choices=["structured", "llm"], default="structured")
    run.add_argument("--mode", choices=["batch", "single"], default="batch",
                     help="V1 compatibility flag; V2 uses --extract for extraction strategy")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = settings()

    if args.command == "serve":
        import uvicorn
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not cfg.api_key:
            parser.error("Set V2_API_KEY before binding the application to a non-loopback interface")
        uvicorn.run("v2.api:app", host=args.host, port=args.port)
        return
    if args.command == "plan":
        from v2.planner import discover
        value = discover(args.goal)
        args.output.write_text(value.model_dump_json(indent=2), encoding="utf-8")
        print(f"Plan written: {args.output}")
        return
    if args.command == "run":
        _run_batch(args)
        return
    if args.command == "fetch":
        _fetch(args)
        return
    if args.command == "strip":
        _strip(args)
        return
    if args.command == "extract":
        _extract(args)
        return
    if args.command == "chunk":
        from pipeline.chunk import chunk_result
        from schemas.extraction import ExtractionResult
        result = ExtractionResult.model_validate_json(
            Path(args.file).read_text(encoding="utf-8-sig"))
        chunks = chunk_result(result, max_chunk_chars=args.max_chars)
        print(json.dumps([c.model_dump(mode="json") for c in chunks], ensure_ascii=False, indent=2))
        return
    if args.command == "live":
        _live(args)
        return

    db = database()
    db.initialize()
    if args.command == "init":
        print("PostgreSQL schema ready")
    elif args.command == "submit":
        plan = CrawlPlan.model_validate_json(args.plan.read_text(encoding="utf-8-sig")) if args.plan else CrawlPlan(
            seed_urls=args.urls, max_pages=args.max_pages, max_depth=args.depth,
            discovery_strategy=args.discover, extraction_strategy=args.extract)
        print(db.create_job(plan, cfg.processing_key(plan)))
    elif args.command == "import-v1":
        import sqlite3
        with sqlite3.connect(args.queue_db.resolve().as_uri() + "?mode=ro", uri=True) as c:
            urls = [r[0] for r in c.execute("SELECT url FROM urls WHERE state='done'")]
        if not urls:
            print("No completed V1 URLs to import")
            return
        for start in range(0, len(urls), 1000):
            batch = urls[start:start+1000]
            plan = CrawlPlan(seed_urls=batch, max_pages=len(batch), goal="V1 migration: fresh ingestion")
            print(db.create_job(plan, cfg.processing_key(plan)))
    elif args.command == "status":
        print(json.dumps(db.jobs(), ensure_ascii=False, indent=2, default=str))
    elif args.command == "retry":
        print(json.dumps({"requeued": db.requeue(args.task_id)}))
    elif args.command == "query":
        print(json.dumps(retriever().query(QueryRequest(question=args.question, answer=args.answer)),
                         ensure_ascii=False, indent=2))
    elif args.command == "delete":
        from v2 import maintenance
        print(json.dumps(maintenance.delete_url(db, components()[0], args.url), ensure_ascii=False))
    elif args.command == "events":
        if args.clear:
            print(json.dumps({"deleted": db.clear_events()}))
        else:
            types = [part.strip() for part in args.event.split(",")] if args.event else None
            print(json.dumps(db.recent_events(limit=args.limit, events=types, url=args.url),
                             ensure_ascii=False, indent=2, default=str))
    elif args.command == "cancel":
        print(json.dumps({"cancelled": db.cancel_job(args.job_id)}))
    else:
        from v2.worker import Worker
        store, embedder = components()
        worker = Worker(cfg, db, store, embedder)
        if args.command == "cleanup":
            worker.cleanup()
        else:
            stop = threading.Event()
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            worker.run(once=args.once, stop=stop)


if __name__ == "__main__":
    main()

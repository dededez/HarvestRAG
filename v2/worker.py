from __future__ import annotations

import hashlib
import logging
import random
import socket
import threading
import time
from uuid import uuid4

from agents.config_loader import load_agent_config
from agents.extractor import extract_content
from agents.llm.client import LiteLLMClient
from agents.validator import validate_result
from pipeline.chunk import chunk_result
from schemas.extraction import ExtractionResult, ContentType
from v2.artifacts import ArtifactStore
from v2.browser import BrowserSession
from v2.content import parse, quality, structured_result, near_similarity
from v2.db import LeaseLost
from v2.fetch import Fetcher, Blocked, ReviewRequired
from v2.models import CrawlPlan

STAGES = ["FETCH", "PARSE", "EXTRACT", "VALIDATE", "CHUNK", "EMBED", "INDEX"]
log = logging.getLogger(__name__)


class Cancelled(RuntimeError):
    pass


class EventLogger:
    def __init__(self, db, task_id):
        self.db, self.task_id = db, task_id

    def log_event(self, event_type, **kwargs):
        # Audit logging is best-effort: a database hiccup must never fail a
        # task that already fetched/parsed/embedded successfully.
        try:
            self.db.event(self.task_id, event_type, kwargs)
        except Exception:
            log.warning("Event logging failed for %s", event_type, exc_info=True)


class Heartbeat:
    def __init__(self, db, task, cfg):
        self.db, self.task, self.cfg = db, task, cfg
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.wait(min(self.cfg.heartbeat_seconds, self.cfg.lease_seconds / 3)):
            try:
                self.db.heartbeat(self.task, self.cfg.lease_seconds)
            except Exception:
                self.lost.set()
                return

    def check(self):
        if self.lost.is_set():
            raise LeaseLost("Heartbeat failed; result publication is disabled")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=12)


class Worker:
    def __init__(self, cfg, db, store, embedder, fetcher=None, artifacts=None):
        self.cfg, self.db, self.store, self.embedder = cfg, db, store, embedder
        self.browser = BrowserSession()
        self.fetcher = fetcher or Fetcher(cfg, db, browser=self.browser)
        self.artifacts = artifacts or ArtifactStore(cfg.artifact_dir)
        self.worker_id = f"{socket.gethostname()}-{uuid4().hex[:12]}"

    def run_once(self):
        task = self.db.claim(self.worker_id, self.cfg.lease_seconds, self.cfg.max_attempts)
        if not task:
            return False
        with Heartbeat(self.db, task, self.cfg) as heartbeat:
            def check():
                heartbeat.check()
                if self.db.job_cancelled(task["job_id"]):
                    raise Cancelled("Job cancelled by user")
            try:
                self.process(task, check)
            except Cancelled:
                try:
                    self.db.finish(task, "BLOCKED", "Cancelled by user")
                except Exception:
                    pass
            except LeaseLost:
                log.warning("Ownership lost for task %s; no state committed", task["id"])
            except (Blocked, ReviewRequired) as exc:
                try:
                    self.db.finish(task, "BLOCKED" if isinstance(exc, Blocked) else "REVIEW_REQUIRED", str(exc))
                except Exception:
                    pass
            except Exception as exc:
                log.exception("Task %s failed", task["id"])
                delay = min(600, self.cfg.retry_base_seconds * 2 ** (task["attempts"]-1)) + random.random()
                try:
                    self.db.retry(task, f"{type(exc).__name__}: {exc}", self.cfg.max_attempts, delay)
                except Exception:
                    log.warning("Could not schedule retry for task %s", task["id"], exc_info=True)
        return True

    def process(self, task, check=lambda: None):
        plan = CrawlPlan.model_validate(task["plan"])
        if task["processing_key"] != self.cfg.processing_key(plan):
            self.db.finish(task, "FAILED", "Processing configuration changed; submit a new Job with this configuration")
            return
        logger = EventLogger(self.db, task["id"])
        if hasattr(self.fetcher, "set_logger"):
            self.fetcher.set_logger(logger)
        refs = self.db.checkpoints(task)

        def stage(name, operation):
            check()
            self.db.stage(task, name)
            if name in refs:
                try:
                    return self.artifacts.get(refs[name])
                except (OSError, ValueError):
                    invalid = STAGES[STAGES.index(name):]
                    self.db.invalidate_checkpoints(task, invalid)
                    for item in invalid:
                        refs.pop(item, None)
                    logger.log_event("checkpoint_invalid", stage=name, url=task["url"])
            value = operation()
            check()
            ref = self.artifacts.put(value)
            self.db.checkpoint(task, name, ref)
            refs[name] = ref
            return value

        previous = self.db.current(task["url_hash"])
        usable_previous = False
        if previous and previous.get("manifest") and previous["processing_key"] == task["processing_key"]:
            try:
                old_rows = self.artifacts.get(previous["manifest"])
                usable_previous = (previous["collection_name"] == self.store.collection and
                                   self.store.contains(previous["current_version"], len(old_rows)))
            except (OSError, ValueError):
                usable_previous = False
        # Discovery needs the actual page links, so do not use HTTP 304 on discovery jobs.
        validators = previous if usable_previous and plan.discovery_strategy == "seeds" else None
        fetched = stage("FETCH", lambda: self.fetcher.fetch(task["url"], task["plan"], validators))
        if fetched["status"] == 304:
            if usable_previous:
                self.db.checked(task, fetched)
                self.db.finish(task, "UNCHANGED", "HTTP 304; published index verified")
                return
            # Index/artifact disappeared since this checkpoint was recorded.
            self.db.invalidate_checkpoints(task, STAGES)
            refs.clear()
            fetched = stage("FETCH", lambda: self.fetcher.fetch(task["url"], task["plan"], None))

        if task["depth"] < plan.max_depth and plan.discovery_strategy != "seeds":
            links = self.fetcher.links(fetched, task["plan"]) if "links" in plan.discovery_strategy else []
            if task["depth"] == 0 and "sitemap" in plan.discovery_strategy:
                try:
                    links += self.fetcher.sitemap(task["url"], task["plan"])
                except Exception as exc:
                    logger.log_event("sitemap_unavailable", url=task["url"], reason=str(exc)[:500])
            self.db.enqueue_links(task, list(dict.fromkeys(links)))

        parsed = stage("PARSE", lambda: parse(fetched, logger))
        if usable_previous and parsed["content_hash"] == previous["content_hash"]:
            self.db.checked(task, fetched)
            self.db.finish(task, "UNCHANGED", "Content and processing versions unchanged; index verified")
            return

        def extract():
            quality(parsed, self.cfg.min_text_chars)
            if plan.extraction_strategy == "structured":
                return structured_result(parsed, fetched).model_dump(mode="json")
            agent_cfg = load_agent_config()
            result = extract_content(parsed["text"], content_type=ContentType.TEXT,
                                     source_url=task["url"], page_title=parsed["title"], mode="batch",
                                     client=LiteLLMClient(agent_cfg, logger), agent_cfg=agent_cfg, logger=logger)
            # PDF text has already been decoded by parse(); pass TEXT rather than reparsing bytes.
            return result.model_dump(mode="json")

        extracted = stage("EXTRACT", extract)

        def validate():
            result = ExtractionResult.model_validate(extracted)
            if plan.extraction_strategy == "llm":
                cfg = load_agent_config()
                validation, result = validate_result(result, content=parsed["text"], mode="batch",
                                                     client=LiteLLMClient(cfg, logger), agent_cfg=cfg, logger=logger)
                if not validation.is_valid:
                    raise ReviewRequired("; ".join(validation.errors))
            else:
                # V1's deterministic validator checks apply to structured results too.
                from agents.validator import deterministic_errors
                errors = deterministic_errors(result)
                if errors:
                    logger.log_event("validation", url=task["url"], outcome="failed",
                                     details={"errors": errors})
                    raise ReviewRequired("; ".join(errors))
            score = quality(parsed, self.cfg.min_text_chars)
            if self.cfg.near_duplicate_enabled:
                for candidate in self.db.duplicate_candidates(task["url_hash"]):
                    other = self.artifacts.get(candidate["manifest"])
                    similarity = near_similarity(parsed["text"], "\n".join(r["content"] for r in other))
                    if similarity >= self.cfg.near_duplicate_threshold:
                        logger.log_event("near_duplicate", url=task["url"],
                                         source_url=candidate["url"], similarity=similarity)
                        raise ReviewRequired(f"Near duplicate of {candidate['url']} ({similarity:.3f}); source relation retained")
            result.content_type = structured_result(parsed, fetched).content_type
            result.source_url = task["url"]
            result.scrape_timestamp = structured_result(parsed, fetched).scrape_timestamp
            return {"result": result.model_dump(mode="json"), "quality": score}

        valid = stage("VALIDATE", validate)
        logger.log_event("validation", url=task["url"], outcome="valid",
                         details={"quality": valid.get("quality")})
        rows = stage("CHUNK", lambda: [c.model_dump(mode="json") for c in
                        chunk_result(ExtractionResult.model_validate(valid["result"]), max_chunk_chars=self.cfg.max_chunk_chars)])
        logger.log_event("chunk_complete", url=task["url"], outcome="ok",
                         details={"num_chunks": len(rows)})
        vectors = stage("EMBED", lambda: self.embedder.embed([row["content"] for row in rows]))
        # Include the immutable manifest in version identity so abandoned writes cannot alter a successor's data.
        version = hashlib.sha256(f"{task['id']}:{task['processing_key']}:{refs['CHUNK']['hash']}".encode()).hexdigest()
        check()
        self.db.stage_version(task, version, refs["CHUNK"], len(rows), self.store.collection,
                              parsed["content_hash"], parsed.get("title"))
        def index():
            self.store.write(version, rows, vectors)
            return {"version": version, "count": len(rows), "collection": self.store.collection}
        stage("INDEX", index)
        if not self.store.contains(version, len(rows)):
            index()  # replay if the vector store was restored/lost after the checkpoint
        check()
        published = self.db.publish(task, version, fetched)
        logger.log_event("published", url=task["url"],
                         outcome="published" if published else "superseded",
                         details={"version": version})
        self.db.finish(task, "COMPLETED", "Published" if published else "Superseded by a newer Job")

    def cleanup(self):
        for row in self.db.retired():
            self.store.delete_version(row["id"], collection=row["collection_name"])
            self.db.mark_cleaned(row["id"])

    def run(self, once=False, stop=None):
        stop = stop or threading.Event()
        try:
            while not stop.is_set():
                try:
                    worked = self.run_once()
                except Exception:
                    # A database/network outage must not kill the worker; retry
                    # the loop so it recovers when PostgreSQL returns.
                    log.exception("Worker loop error; retrying in 5s")
                    stop.wait(5)
                    continue
                if once:
                    return
                if not worked:
                    stop.wait(2)
        finally:
            self.browser.close()

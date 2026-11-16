from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from v2.urls import canonicalize, fingerprint


class LeaseLost(RuntimeError):
    pass


class Database:
    """Short transactions; never hold a transaction during network/model work."""

    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def connect(self):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10) as conn:
            yield conn

    def initialize(self):
        with self.connect() as c:
            # Serialize initialization by API and worker containers.
            c.execute("SELECT pg_advisory_xact_lock(2026111201)")
            c.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))

    def create_job(self, plan, processing_key):
        with self.connect() as c:
            inserted = c.execute(
                "INSERT INTO v2_jobs(id,plan,processing_key) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                (plan.job_id, Jsonb(plan.model_dump()), processing_key)).fetchone()
            if not inserted:
                old = c.execute("SELECT * FROM v2_jobs WHERE id=%s", (plan.job_id,)).fetchone()
                if old["plan"] != plan.model_dump() or old["processing_key"] != processing_key:
                    raise ValueError("job_id already exists with a different plan or processing configuration")
                return plan.job_id
            for url in plan.seed_urls:
                self._insert_task(c, plan.job_id, url, 0)
        return plan.job_id

    @staticmethod
    def _insert_task(c, job_id, url, depth):
        return c.execute(
            "INSERT INTO v2_tasks(id,job_id,url,url_hash,depth) VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT(job_id,url_hash) DO NOTHING RETURNING id",
            (str(uuid4()), job_id, url, fingerprint(url), depth)).fetchone()

    @staticmethod
    def guard(c, task):
        row = c.execute("SELECT id FROM v2_tasks WHERE id=%s AND lease_token=%s "
                        "AND status='RUNNING' AND lease_until>now() FOR UPDATE",
                        (task["id"], task["lease_token"])).fetchone()
        if not row:
            raise LeaseLost("Task ownership expired or was replaced")

    def claim(self, worker_id, lease_seconds, max_attempts):
        token = str(uuid4())
        with self.connect() as c:
            c.execute("UPDATE v2_tasks SET status='DEAD_LETTER',reason='attempt budget exhausted',"
                      "lease_token=NULL,lease_until=NULL,updated_at=now() "
                      "WHERE attempts>=%s AND ((status='RUNNING' AND lease_until<=now()) "
                      "OR status IN ('PENDING','RETRY_WAIT'))", (max_attempts,))
            row = c.execute("""
                WITH candidate AS (
                  SELECT id FROM v2_tasks WHERE attempts<%s AND
                  ((status IN ('PENDING','RETRY_WAIT') AND available_at<=now())
                   OR (status='RUNNING' AND lease_until<=now()))
                  ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE v2_tasks t SET status='RUNNING',attempts=attempts+1,
                  worker_id=%s,lease_token=%s,lease_until=now()+%s*interval '1 second',updated_at=now()
                FROM candidate WHERE t.id=candidate.id RETURNING t.*
                """, (max_attempts, worker_id, token, lease_seconds)).fetchone()
            if row:
                row.update(c.execute("SELECT plan,processing_key FROM v2_jobs WHERE id=%s",
                                     (row["job_id"],)).fetchone())
            return row

    def heartbeat(self, task, seconds):
        with self.connect() as c:
            row = c.execute("UPDATE v2_tasks SET lease_until=now()+%s*interval '1 second' "
                            "WHERE id=%s AND lease_token=%s AND status='RUNNING' AND lease_until>now() RETURNING id",
                            (seconds, task["id"], task["lease_token"])).fetchone()
            if not row:
                raise LeaseLost("Cannot renew expired lease")

    def stage(self, task, stage):
        with self.connect() as c:
            self.guard(c, task)
            c.execute("UPDATE v2_tasks SET stage=%s,updated_at=now() WHERE id=%s", (stage, task["id"]))

    def checkpoints(self, task):
        with self.connect() as c:
            rows = c.execute("SELECT stage,artifact FROM v2_checkpoints WHERE task_id=%s", (task["id"],)).fetchall()
            return {r["stage"]: r["artifact"] for r in rows}

    def checkpoint(self, task, stage, ref):
        with self.connect() as c:
            self.guard(c, task)
            c.execute("INSERT INTO v2_checkpoints(task_id,stage,artifact) VALUES(%s,%s,%s) "
                      "ON CONFLICT(task_id,stage) DO UPDATE SET artifact=EXCLUDED.artifact,completed_at=now()",
                      (task["id"], stage, Jsonb(ref)))

    def invalidate_checkpoints(self, task, stages):
        with self.connect() as c:
            self.guard(c, task)
            c.execute("DELETE FROM v2_checkpoints WHERE task_id=%s AND stage=ANY(%s)", (task["id"], stages))

    def finish(self, task, status, reason=None):
        if status not in {"COMPLETED", "UNCHANGED", "BLOCKED", "REVIEW_REQUIRED", "FAILED"}:
            raise ValueError("Invalid terminal state")
        with self.connect() as c:
            self.guard(c, task)
            c.execute("UPDATE v2_tasks SET status=%s,reason=%s,lease_token=NULL,lease_until=NULL,"
                      "updated_at=now() WHERE id=%s", (status, reason, task["id"]))

    def retry(self, task, reason, max_attempts, delay):
        with self.connect() as c:
            self.guard(c, task)
            status = "DEAD_LETTER" if task["attempts"] >= max_attempts else "RETRY_WAIT"
            c.execute("UPDATE v2_tasks SET status=%s,reason=%s,available_at=now()+%s*interval '1 second',"
                      "lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=%s",
                      (status, reason[:2000], delay, task["id"]))

    def enqueue_links(self, task, urls):
        plan = task["plan"]
        if task["depth"] >= plan["max_depth"]:
            return 0
        with self.connect() as c:
            self.guard(c, task)
            # One locked job row makes the cap exact across all workers.
            c.execute("SELECT id FROM v2_jobs WHERE id=%s FOR UPDATE", (task["job_id"],))
            used = c.execute("SELECT count(*) n FROM v2_tasks WHERE job_id=%s", (task["job_id"],)).fetchone()["n"]
            added = 0
            for url in urls:
                if used + added >= plan["max_pages"]:
                    break
                added += bool(self._insert_task(c, task["job_id"], canonicalize(url), task["depth"] + 1))
            return added

    def reserve_domain(self, domain, interval):
        with self.connect() as c:
            c.execute("INSERT INTO v2_domains(domain) VALUES(%s) ON CONFLICT DO NOTHING", (domain,))
            row = c.execute("SELECT greatest(0,extract(epoch FROM next_allowed_at-now())) delay "
                            "FROM v2_domains WHERE domain=%s FOR UPDATE", (domain,)).fetchone()
            delay = float(row["delay"])
            if delay <= 0:
                c.execute("UPDATE v2_domains SET next_allowed_at=now()+%s*interval '1 second' WHERE domain=%s",
                          (interval, domain))
            return delay

    def cool_domain(self, domain, seconds):
        with self.connect() as c:
            c.execute("INSERT INTO v2_domains(domain,next_allowed_at) VALUES(%s,now()+%s*interval '1 second') "
                      "ON CONFLICT(domain) DO UPDATE SET next_allowed_at=greatest(v2_domains.next_allowed_at,EXCLUDED.next_allowed_at)",
                      (domain, seconds))

    def current(self, doc_id):
        with self.connect() as c:
            return c.execute("SELECT d.*,v.content_hash,v.processing_key,v.manifest,v.collection_name "
                             "FROM v2_documents d LEFT JOIN v2_versions v ON v.id=d.current_version WHERE d.id=%s",
                             (doc_id,)).fetchone()

    def documents(self, limit=50, offset=0, query=None):
        limit = max(1, min(int(limit), 200))
        pattern = f"%{query}%" if query else None
        with self.connect() as c:
            return c.execute(
                "SELECT d.id,d.url,d.page_title,d.current_version,d.last_checked_at,"
                "v.chunk_count,v.collection_name,v.published_at "
                "FROM v2_documents d JOIN v2_versions v ON v.id=d.current_version "
                "WHERE d.current_version IS NOT NULL "
                "AND (%s::text IS NULL OR d.url ILIKE %s OR COALESCE(d.page_title,'') ILIKE %s) "
                "ORDER BY v.published_at DESC NULLS LAST, d.url "
                "LIMIT %s OFFSET %s",
                (query, pattern, pattern, limit, max(0, int(offset))),
            ).fetchall()

    def document_count(self, query=None):
        pattern = f"%{query}%" if query else None
        with self.connect() as c:
            return c.execute(
                "SELECT count(*) n FROM v2_documents d JOIN v2_versions v ON v.id=d.current_version "
                "WHERE d.current_version IS NOT NULL "
                "AND (%s::text IS NULL OR d.url ILIKE %s OR COALESCE(d.page_title,'') ILIKE %s)",
                (query, pattern, pattern),
            ).fetchone()["n"]

    def document(self, document_id):
        with self.connect() as c:
            return c.execute(
                "SELECT d.*,v.chunk_count,v.collection_name,v.published_at,v.content_hash "
                "FROM v2_documents d JOIN v2_versions v ON v.id=d.current_version WHERE d.id=%s",
                (document_id,),
            ).fetchone()

    def stage_version(self, task, version, manifest_ref, count, collection, content_hash, page_title=None):
        with self.connect() as c:
            self.guard(c, task)
            c.execute(
                "INSERT INTO v2_documents(id,url,page_title) VALUES(%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET page_title=COALESCE(EXCLUDED.page_title, v2_documents.page_title)",
                (task["url_hash"], task["url"], page_title),
            )
            c.execute("INSERT INTO v2_versions(id,document_id,content_hash,processing_key,collection_name,manifest,chunk_count,task_created_at) "
                      "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                      (version, task["url_hash"], content_hash, task["processing_key"], collection,
                       Jsonb(manifest_ref), count, task["created_at"]))

    def publish(self, task, version, fetch):
        with self.connect() as c:
            self.guard(c, task)
            doc = c.execute("SELECT * FROM v2_documents WHERE id=%s FOR UPDATE", (task["url_hash"],)).fetchone()
            current = c.execute("SELECT * FROM v2_versions WHERE id=%s", (doc["current_version"],)).fetchone()
            # A slower, older Job must never replace the result of a newer Job.
            if current and current["task_created_at"] > task["created_at"]:
                c.execute("UPDATE v2_versions SET status='RETIRED' WHERE id=%s AND status='STAGED'", (version,))
                return False
            if current and current["id"] != version:
                c.execute("UPDATE v2_versions SET status='RETIRED' WHERE id=%s", (current["id"],))
            c.execute("UPDATE v2_versions SET status='PUBLISHED',published_at=now() WHERE id=%s", (version,))
            c.execute("UPDATE v2_documents SET current_version=%s,etag=%s,last_modified=%s,fetcher=%s,"
                      "last_checked_at=now(),updated_at=now() WHERE id=%s",
                      (version, fetch.get("etag"), fetch.get("last_modified"), fetch.get("fetcher"), task["url_hash"]))
            return True

    def checked(self, task, fetch):
        with self.connect() as c:
            self.guard(c, task)
            c.execute("UPDATE v2_documents d SET last_checked_at=now(), "
                      "etag=CASE WHEN %s=304 THEN d.etag ELSE %s END, "
                      "last_modified=CASE WHEN %s=304 THEN d.last_modified ELSE %s END, "
                      "fetcher=CASE WHEN %s=304 THEN d.fetcher ELSE %s END "
                      "FROM v2_versions v WHERE d.id=%s AND v.id=d.current_version AND v.task_created_at<=%s",
                      (fetch["status"], fetch.get("etag"), fetch["status"], fetch.get("last_modified"),
                       fetch["status"], fetch.get("fetcher"), task["url_hash"], task["created_at"]))

    def active_versions(self, collection, source_url=None):
        with self.connect() as c:
            rows = c.execute("SELECT v.id FROM v2_versions v JOIN v2_documents d ON d.current_version=v.id "
                             "WHERE v.collection_name=%s AND v.status='PUBLISHED' AND (%s::text IS NULL OR d.url=%s)",
                             (collection, source_url, source_url)).fetchall()
            return [r["id"] for r in rows]

    def retired(self):
        with self.connect() as c:
            return c.execute("SELECT id,collection_name FROM v2_versions WHERE status='RETIRED' ORDER BY created_at LIMIT 100").fetchall()

    def duplicate_candidates(self, exclude_document):
        with self.connect() as c:
            return c.execute("SELECT d.url,v.manifest FROM v2_documents d JOIN v2_versions v ON d.current_version=v.id "
                             "WHERE d.id!=%s ORDER BY v.published_at DESC LIMIT 100", (exclude_document,)).fetchall()

    def mark_cleaned(self, version):
        with self.connect() as c:
            c.execute("UPDATE v2_versions SET status='CLEANED' WHERE id=%s AND status='RETIRED'", (version,))

    def event(self, task_id, event, details):
        with self.connect() as c:
            c.execute("INSERT INTO v2_events(task_id,event,details) VALUES(%s,%s,%s)", (task_id, event, Jsonb(details)))

    def jobs(self):
        with self.connect() as c:
            return c.execute("SELECT j.id,j.plan,j.created_at, "
                             "(SELECT jsonb_object_agg(status,n) FROM (SELECT status,count(*) n FROM v2_tasks "
                             "WHERE job_id=j.id GROUP BY status) s) states FROM v2_jobs j ORDER BY created_at DESC LIMIT 100").fetchall()

    def tasks(self, job_id=None, review_only=False):
        with self.connect() as c:
            return c.execute("SELECT id,job_id,url,status,stage,attempts,reason,updated_at FROM v2_tasks "
                             "WHERE (%s::text IS NULL OR job_id=%s) AND (%s=false OR status='REVIEW_REQUIRED') "
                             "ORDER BY created_at DESC LIMIT 1000", (job_id, job_id, review_only)).fetchall()

    def requeue(self, task_id):
        with self.connect() as c:
            row = c.execute("UPDATE v2_tasks SET status='PENDING',attempts=0,reason=NULL,available_at=now(),updated_at=now() "
                            "WHERE id=%s AND status IN ('REVIEW_REQUIRED','FAILED','DEAD_LETTER','BLOCKED') RETURNING id", (task_id,)).fetchone()
            if row:
                c.execute("DELETE FROM v2_checkpoints WHERE task_id=%s AND stage != 'FETCH'", (task_id,))
            return bool(row)

    def recent_events(self, limit=200, events=None, url=None, task_id=None):
        limit = max(1, min(int(limit), 1000))
        clauses, params = [], []
        if events:
            clauses.append("event = ANY(%s)")
            params.append(list(events))
        if url:
            clauses.append("details->>'url' ILIKE %s")
            params.append(f"%{url}%")
        if task_id:
            clauses.append("task_id=%s")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as c:
            return c.execute(
                f"SELECT * FROM v2_events {where} ORDER BY id DESC LIMIT %s", params
            ).fetchall()

    def event_types(self):
        with self.connect() as c:
            return [row["event"] for row in
                    c.execute("SELECT DISTINCT event FROM v2_events ORDER BY event").fetchall()]

    def clear_events(self):
        with self.connect() as c:
            row = c.execute("DELETE FROM v2_events RETURNING id").fetchall()
            return len(row)

    def job_cancelled(self, job_id):
        with self.connect() as c:
            row = c.execute("SELECT cancelled_at FROM v2_jobs WHERE id=%s", (job_id,)).fetchone()
            return bool(row and row["cancelled_at"])

    def cancel_job(self, job_id):
        """Mark a job cancelled and stop every non-terminal task.

        A RUNNING task's worker notices the cancellation on its next check and
        cannot publish because of the lease/publish guards.
        """
        with self.connect() as c:
            updated = c.execute(
                "UPDATE v2_jobs SET cancelled_at=now() WHERE id=%s RETURNING id", (job_id,)
            ).fetchone()
            if not updated:
                return 0
            rows = c.execute(
                "UPDATE v2_tasks SET status='BLOCKED', reason='Cancelled by user', "
                "lease_token=NULL, lease_until=NULL, updated_at=now() "
                "WHERE job_id=%s AND status IN ('PENDING','RETRY_WAIT','RUNNING') RETURNING id",
                (job_id,),
            ).fetchall()
            return len(rows)

    def delete_url(self, url, delete_version):
        """Delete one URL's tasks, checkpoints, versions, vectors and events.

        Artifacts are content-addressed and left in place; they are harmless
        and may be shared with other documents. Works even when the URL never
        published (e.g. a cancelled Job).
        """
        with self.connect() as c:
            doc = c.execute("SELECT id FROM v2_documents WHERE url=%s", (url,)).fetchone()
            document_id = doc["id"] if doc else fingerprint(url)
            tasks = c.execute("SELECT id FROM v2_tasks WHERE url_hash=%s", (document_id,)).fetchall()
            task_ids = [row["id"] for row in tasks]
            versions = c.execute(
                "SELECT id,collection_name FROM v2_versions WHERE document_id=%s", (document_id,)
            ).fetchall()
            for version in versions:
                try:
                    delete_version(version["id"], version["collection_name"])
                except Exception:
                    # Vector cleanup is best-effort; metadata deletion still proceeds.
                    pass
            events = 0
            if task_ids:
                events = len(c.execute(
                    "DELETE FROM v2_events WHERE task_id=ANY(%s) RETURNING id", (task_ids,)
                ).fetchall())
                c.execute("DELETE FROM v2_checkpoints WHERE task_id=ANY(%s)", (task_ids,))
                c.execute("DELETE FROM v2_tasks WHERE id=ANY(%s)", (task_ids,))
            if versions:
                c.execute("DELETE FROM v2_versions WHERE document_id=%s", (document_id,))
            if doc:
                c.execute("DELETE FROM v2_documents WHERE id=%s", (document_id,))
            return {"deleted": bool(task_ids or versions or doc), "versions": len(versions),
                    "tasks": len(task_ids), "events": events}

    def delete_tasks_for_urls(self, urls):
        """V1 `--reset` equivalent: clear queue state for the given URLs only."""
        ids = [fingerprint(url) for url in urls]
        with self.connect() as c:
            tasks = c.execute("SELECT id FROM v2_tasks WHERE url_hash=ANY(%s)", (ids,)).fetchall()
            task_ids = [row["id"] for row in tasks]
            if not task_ids:
                return 0
            c.execute("DELETE FROM v2_events WHERE task_id=ANY(%s)", (task_ids,))
            c.execute("DELETE FROM v2_checkpoints WHERE task_id=ANY(%s)", (task_ids,))
            c.execute("DELETE FROM v2_tasks WHERE id=ANY(%s)", (task_ids,))
            return len(task_ids)

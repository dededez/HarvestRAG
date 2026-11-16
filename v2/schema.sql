CREATE TABLE IF NOT EXISTS v2_schema_version (version integer PRIMARY KEY);
INSERT INTO v2_schema_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS v2_jobs (
 id text PRIMARY KEY, plan jsonb NOT NULL, processing_key text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE v2_jobs ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
CREATE TABLE IF NOT EXISTS v2_tasks (
 id text PRIMARY KEY, job_id text NOT NULL REFERENCES v2_jobs(id),
 url text NOT NULL, url_hash text NOT NULL, depth integer NOT NULL DEFAULT 0,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN
 ('PENDING','RUNNING','RETRY_WAIT','COMPLETED','UNCHANGED','BLOCKED','REVIEW_REQUIRED','FAILED','DEAD_LETTER')),
 stage text NOT NULL DEFAULT 'FETCH', attempts integer NOT NULL DEFAULT 0,
 lease_token text, lease_until timestamptz, worker_id text,
 available_at timestamptz NOT NULL DEFAULT now(), reason text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(job_id,url_hash)
);
CREATE INDEX IF NOT EXISTS v2_tasks_due ON v2_tasks(status,available_at,created_at);
CREATE TABLE IF NOT EXISTS v2_checkpoints (
 task_id text NOT NULL REFERENCES v2_tasks(id), stage text NOT NULL,
 artifact jsonb NOT NULL, completed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(task_id,stage)
);
CREATE TABLE IF NOT EXISTS v2_documents (
 id text PRIMARY KEY, url text NOT NULL, current_version text,
 etag text, last_modified text, fetcher text,
 last_checked_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE v2_documents ADD COLUMN IF NOT EXISTS page_title text;
CREATE TABLE IF NOT EXISTS v2_versions (
 id text PRIMARY KEY, document_id text NOT NULL REFERENCES v2_documents(id),
 content_hash text NOT NULL, processing_key text NOT NULL, collection_name text NOT NULL,
 manifest jsonb NOT NULL, status text NOT NULL DEFAULT 'STAGED'
 CHECK(status IN ('STAGED','PUBLISHED','RETIRED','CLEANED')),
 chunk_count integer NOT NULL, task_created_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz
);
CREATE INDEX IF NOT EXISTS v2_versions_document ON v2_versions(document_id);
CREATE TABLE IF NOT EXISTS v2_events (
 id bigserial PRIMARY KEY, task_id text, event text NOT NULL, details jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS v2_domains (
 domain text PRIMARY KEY, next_allowed_at timestamptz NOT NULL DEFAULT now()
);

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseModel):
    database_url: str = "postgresql://scraper:scraper@localhost:5432/scraper"
    milvus_uri: str = "http://localhost:19530"
    milvus_token: str = ""
    artifact_dir: Path = ROOT / "data" / "v2" / "artifacts"
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_enabled: bool = True
    rerank_min_score: float = Field(0.2, ge=0, le=1)
    collection_prefix: str = "scraper_v2"
    lease_seconds: int = Field(120, ge=15)
    heartbeat_seconds: int = Field(20, ge=1)
    retry_base_seconds: int = Field(10, ge=1)
    max_attempts: int = Field(3, ge=1, le=20)
    domain_interval: float = Field(1.0, ge=0)
    request_timeout: float = Field(30, gt=0)
    max_body_bytes: int = Field(10_000_000, ge=1024)
    max_chunk_chars: int = Field(2000, ge=200, le=8000)
    min_text_chars: int = Field(40, ge=1)
    extraction_mode: str = "structured"
    browser_enabled: bool = True
    allow_private_network: bool = False
    api_key: str = ""
    processing_revision: str = "v2.1"
    near_duplicate_enabled: bool = False
    near_duplicate_threshold: float = Field(0.92, ge=0, le=1)
    embedding_fallback_model: str = "all-MiniLM-L6-v2"
    allow_embedding_fallback: bool = True

    @classmethod
    def from_env(cls):
        # Reuse the project's .env convention without printing credentials.
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        values = {name: os.environ["V2_" + name.upper()]
                  for name in cls.model_fields if "V2_" + name.upper() in os.environ}
        # V1's versioned retry policy remains the default when V2_* is absent.
        if "max_attempts" not in values or "retry_base_seconds" not in values:
            try:
                from harvest_rag.site_config import orchestrator_config
                retry = orchestrator_config().get("retry", {})
                values.setdefault("max_attempts", retry.get("max_attempts", 2))
                values.setdefault("retry_base_seconds", retry.get("backoff_seconds", 5))
            except Exception:
                pass
        return cls(**values)

    def processing_key(self, plan=None):
        values = {k: getattr(self, k) for k in (
            "embedding_model", "max_chunk_chars", "extraction_mode", "processing_revision",
            "min_text_chars", "near_duplicate_enabled", "near_duplicate_threshold")}
        if plan is not None:
            values["plan_extraction"] = plan.extraction_strategy
            values["plan_fetch"] = plan.fetch_strategy
        sources = [ROOT / "config" / "agents.json", ROOT / "harvest_rag" / "content.py",
                   ROOT / "pipeline" / "strip.py", ROOT / "pipeline" / "main_extract.py",
                   ROOT / "pipeline" / "chunk.py", ROOT / "agents" / "extractor.py",
                   ROOT / "agents" / "validator.py"]
        for path in [*sources, *sorted((ROOT / "agents" / "prompts").glob("*.txt"))]:
            values[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

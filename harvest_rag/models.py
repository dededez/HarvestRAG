from __future__ import annotations

from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, Field, ConfigDict, model_validator
from harvest_rag.urls import canonicalize, host_of


class CrawlPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(default_factory=lambda: str(uuid4()))
    goal: str = "Manual URL ingestion"
    seed_urls: list[str] = Field(min_length=1, max_length=1000)
    allowed_domains: list[str] = Field(default_factory=list)
    max_pages: int = Field(50, ge=1, le=10000)
    max_depth: int = Field(0, ge=0, le=10)
    discovery_strategy: Literal["seeds", "links", "sitemap", "links+sitemap"] = "seeds"
    fetch_strategy: Literal["auto", "http"] = "auto"
    extraction_strategy: Literal["structured", "llm"] = "structured"
    output_target: Literal["rag"] = "rag"

    @model_validator(mode="after")
    def normalize(self):
        self.seed_urls = list(dict.fromkeys(canonicalize(u) for u in self.seed_urls))
        self.allowed_domains = sorted(set(d.lower().rstrip(".").encode("idna").decode()
                                          for d in self.allowed_domains)) or sorted({host_of(u) for u in self.seed_urls})
        if any(host_of(u) not in self.allowed_domains for u in self.seed_urls):
            raise ValueError("Every seed must be in allowed_domains")
        if len(self.seed_urls) > self.max_pages:
            raise ValueError("max_pages must cover all seed URLs")
        return self


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    k: int = Field(5, ge=1, le=30)
    candidates: int = Field(15, ge=1, le=300)
    source_url: str | None = None
    answer: bool = False


class DiscoverRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=4000)
    max_pages: int = Field(50, ge=1, le=10000)
    max_depth: int = Field(1, ge=0, le=5)
    multi_query: bool = True

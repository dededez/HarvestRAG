from functools import lru_cache
from pipeline.embed import LocalEmbedder
from harvest_rag.config import Settings
from harvest_rag.db import Database
from harvest_rag.store import MilvusStore
from harvest_rag.retrieval import Retriever


@lru_cache(maxsize=1)
def settings():
    return Settings.from_env()


@lru_cache(maxsize=1)
def database():
    return Database(settings().database_url)


@lru_cache(maxsize=1)
def components():
    cfg = settings()
    configured = cfg.embedding_model
    try:
        embedder = LocalEmbedder(configured)
    except Exception as exc:
        if not cfg.allow_embedding_fallback:
            raise
        embedder = LocalEmbedder(cfg.embedding_fallback_model)
        cfg.embedding_model = cfg.embedding_fallback_model
        try:
            database().event(None, "embedding_fallback",
                             {"from": configured, "to": cfg.embedding_fallback_model,
                              "error": str(exc)[:500]})
        except Exception:
            pass
    store = MilvusStore(cfg, embedder)
    store.ensure()
    return store, embedder


@lru_cache(maxsize=1)
def retriever():
    store, embedder = components()
    return Retriever(settings(), database(), store, embedder)

from __future__ import annotations

import threading
from harvest_rag.urls import canonicalize


def answer_evidence(question, evidence):
    """Generate a grounded answer from evidence rows; validate citations.

    Returns (answer_dict_or_None, message_or_None). Kept separate so /live can
    synthesize an answer from freshly fetched evidence, as V1's UI did.
    """
    from agents.answer import generate_answer
    from agents.config_loader import load_agent_config
    from agents.llm.client import LiteLLMClient

    if not evidence:
        return None, "No evidence to answer from"
    cfg = load_agent_config()
    value = generate_answer(question, [{"text": r["text"], "provenance": r["metadata"]} for r in evidence],
                            client=LiteLLMClient(cfg), agent_cfg=cfg)
    if not value:
        return None, "Answer generation failed; retrieved evidence is preserved"
    allowed = {r["metadata"].get("source_url") for r in evidence}
    if all(c.source_url in allowed for c in value.citations):
        return value.model_dump(mode="json"), None
    return None, "Answer rejected: citation outside retrieved evidence"


class Retriever:
    def __init__(self, settings, db, store, embedder, reranker=None):
        self.cfg, self.db, self.store, self.embedder = settings, db, store, embedder
        self.reranker = reranker
        self.lock = threading.Lock()

    def warmup(self):
        with self.lock:
            self.embedder.embed(["warmup"])
            if self.cfg.reranker_enabled and self.reranker is None:
                from sentence_transformers import CrossEncoder
                self.reranker = CrossEncoder(self.cfg.reranker_model)
                self.reranker.predict([("warmup", "warmup")])

    def query(self, request):
        source = canonicalize(request.source_url) if request.source_url else None
        active = self.db.active_versions(self.store.collection, source)
        if not active:
            return {"evidence": [], "answer": None, "message": "No published documents match this query scope"}
        with self.lock:
            vector = self.embedder.embed([request.question])[0]
            rows = self.store.search(request.question, vector, active, max(request.k, request.candidates))
            # Publication may have changed while Milvus searched. Never return retired evidence.
            still_active = set(self.db.active_versions(self.store.collection, source))
            rows = [r for r in rows if r["version"] in still_active]
            if self.cfg.reranker_enabled and rows:
                if self.reranker is None:
                    from sentence_transformers import CrossEncoder
                    self.reranker = CrossEncoder(self.cfg.reranker_model)
                scores = self.reranker.predict([(request.question, r["text"]) for r in rows])
                for row, score in zip(rows, scores):
                    row["rerank_score"] = float(score)
                rows.sort(key=lambda r: r["rerank_score"], reverse=True)
                # P2-1: do not hand weak/noisy matches to the answer agent.
                rows = [r for r in rows if r["rerank_score"] >= self.cfg.rerank_min_score]
        rows = rows[:request.k]
        if not rows:
            return {"evidence": [], "answer": None,
                    "message": "No sufficiently relevant evidence found"}
        answer = None
        message = None
        if request.answer:
            answer, message = answer_evidence(request.question, rows)
        return {"evidence": rows, "answer": answer, "message": message}

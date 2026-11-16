r"""Offline retrieval evaluation for choosing a rerank threshold.

Runs the same retrieval stack as the API (local embedder + Milvus hybrid search
+ reranker) over a small labelled question set, then reports in-corpus hit rate
and out-of-corpus evidence rate for several candidate thresholds.

Usage (PowerShell, from the project root):

    $env:V2_DATABASE_URL = <dsn>
    $env:V2_MILVUS_URI = 'http://127.0.0.1:19530'
    $env:V2_RERANKER_MODEL = '<local reranker dir>'
    $env:CUDA_VISIBLE_DEVICES = ''      # CPU keeps it usable while the GPU is busy
    .\.venv\Scripts\python.exe tools\eval_retrieval.py

The dataset is JSONL: {"question": ..., "expected_urls": [...], "kind": "in"|"out"}.
Out-of-corpus questions have an empty expected_urls list.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(ROOT / "tests" / "eval" / "retrieval_questions.jsonl"))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--candidates", type=int, default=20)
    parser.add_argument("--thresholds", default="0,0.05,0.1,0.15,0.2,0.3,0.5")
    args = parser.parse_args()

    from sentence_transformers import CrossEncoder

    from pipeline.embed import LocalEmbedder
    from v2.config import Settings
    from v2.db import Database
    from v2.store import MilvusStore

    cfg = Settings.from_env()
    db = Database(cfg.database_url)
    embedder = LocalEmbedder(cfg.embedding_model)
    store = MilvusStore(cfg, embedder)
    store.ensure()
    reranker = CrossEncoder(cfg.reranker_model) if cfg.reranker_enabled else None

    questions = [
        json.loads(line)
        for line in Path(args.dataset).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    active = db.active_versions(store.collection)
    print(f"collection={store.collection} active_versions={len(active)} "
          f"questions={len(questions)} reranker={'on' if reranker else 'off'}")

    results = []
    for item in questions:
        question = item["question"]
        vector = embedder.embed([question])[0]
        rows = store.search(question, vector, active, args.candidates)
        if reranker is not None and rows:
            scores = reranker.predict([(question, row["text"]) for row in rows])
            for row, score in zip(rows, scores):
                row["rerank_score"] = float(score)
            rows.sort(key=lambda row: row["rerank_score"], reverse=True)
        else:
            for row in rows:
                row["rerank_score"] = None
        results.append((item, rows))
        top = rows[0] if rows else None
        score = f"{top['rerank_score']:.3f}" if top and top["rerank_score"] is not None else "-"
        host = (top["metadata"].get("source_url", "") if top else "-")[:70]
        print(f"[{item.get('kind', '?')}] {question[:46]!r} top={score} {host}")

    print("\nthreshold  in_hit   out_evidence   avg_in_top  avg_out_top")
    for threshold in [float(value) for value in args.thresholds.split(",")]:
        in_total = in_hit = out_total = out_evidence = 0
        in_scores: list[float] = []
        out_scores: list[float] = []
        for item, rows in results:
            kept = [
                row for row in rows
                if row["rerank_score"] is None or row["rerank_score"] >= threshold
            ][: args.k]
            expected = item.get("expected_urls") or []
            if expected:
                in_total += 1
                urls = {row["metadata"].get("source_url") for row in kept}
                in_hit += any(url in urls for url in expected)
                if rows and rows[0]["rerank_score"] is not None:
                    in_scores.append(rows[0]["rerank_score"])
            else:
                out_total += 1
                out_evidence += bool(kept)
                if rows and rows[0]["rerank_score"] is not None:
                    out_scores.append(rows[0]["rerank_score"])
        avg_in = sum(in_scores) / len(in_scores) if in_scores else 0.0
        avg_out = sum(out_scores) / len(out_scores) if out_scores else 0.0
        print(f"{threshold:9.2f}  {in_hit}/{in_total:<4}  {out_evidence}/{out_total:<4} "
              f"        {avg_in:.3f}       {avg_out:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

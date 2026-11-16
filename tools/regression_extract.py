"""Regression checks for generic extraction on stored sample pages.

Usage:
    $env:V2_DATABASE_URL = <dsn>
    python tools/regression_extract.py

Reads the latest FETCH artifact for each sample URL, runs the current
pipeline.strip + v2.content quality gate, and checks the structural outcome.
Exit code 1 if an expectation fails.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAMPLES = {
    "marketplace": "https://github.com/marketplace",
    "aws": "https://aws.amazon.com/cn/blogs/china/amazon-quick-build-llm-wiki-knowledge-base/",
    "cqvip": "https://www.cqvip.com/doc/journal/7202345820?sign=09ae0b21fe4de725809d907cbee1ab8645529a72547815d6e483dfefdcf8ab65&expireTime=1795304082636&resourceId=7202345820&type=1",
    "tencent": "https://cloud.tencent.cn/developer/article/2658818",
    "term": "https://www.term.org.cn/CN/10.12339/j.issn.1673-8578.2026.05.035",
}

_CHECKS = {
    "marketplace": {
        "must_contain": ["Enhance your workflow", "Zube", "Agile project management"],
        "must_not_contain": ["ProductsPlatform", "Footer navigation"],
        "expect_review": False,
    },
    "aws": {"must_contain": ["LLM Wiki", "Amazon Quick"], "must_not_contain": [], "expect_review": False},
    "cqvip": {"must_contain": [], "must_not_contain": [], "expect_review": True},
    "tencent": {"must_contain": ["LLM Wiki", "RAG"], "must_not_contain": [], "expect_review": False},
    "term": {"must_contain": ["术语", "图书馆"], "must_not_contain": [], "expect_review": False},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", nargs="*", default=list(SAMPLES),
                        help="Sample keys to run (default: all)")
    args = parser.parse_args()

    import psycopg
    from psycopg.rows import dict_row

    from pipeline.chunk import chunk_result
    from harvest_rag.artifacts import ArtifactStore
    from harvest_rag.content import parse, quality, structured_result
    from harvest_rag.fetch import ReviewRequired

    dsn = os.environ.get("V2_DATABASE_URL")
    if not dsn:
        print("V2_DATABASE_URL is required")
        return 2
    store = ArtifactStore(ROOT / "data" / "v2" / "artifacts")
    failed = False
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        for key in args.samples:
            url = SAMPLES[key]
            row = connection.execute(
                """SELECT c.artifact FROM v2_tasks t
                   JOIN v2_checkpoints c ON c.task_id=t.id AND c.stage='FETCH'
                   WHERE t.url=%s ORDER BY t.created_at DESC LIMIT 1""", (url,)).fetchone()
            if not row:
                print(f"[FAIL] {key}: no stored FETCH artifact")
                failed = True
                continue
            fetched = store.get(row["artifact"])
            parsed = parse(fetched)
            review = None
            try:
                quality(parsed, 40)
            except ReviewRequired as exc:
                review = str(exc)
            chunks = chunk_result(structured_result(parsed, fetched), max_chunk_chars=2000)
            text = parsed["text"]
            check = _CHECKS[key]
            problems = []
            for phrase in check["must_contain"]:
                if phrase not in text:
                    problems.append(f"missing {phrase!r}")
            for phrase in check["must_not_contain"]:
                if phrase in text:
                    problems.append(f"contains noise {phrase!r}")
            if check["expect_review"] and not review:
                problems.append("expected REVIEW_REQUIRED")
            if not check["expect_review"] and review:
                problems.append(f"unexpected review: {review}")
            status = "PASS" if not problems else "FAIL"
            failed = failed or bool(problems)
            print(f"[{status}] {key}: chars={len(text)} blocks={len(parsed['sections'])} "
                  f"chunks={len(chunks)} main={parsed['main_found']}/{parsed['main_method']} "
                  f"link={parsed['link_ratio']:.3f} items={parsed['item_count']} "
                  f"review={review or '-'}")
            for problem in problems:
                print("       ", problem)

            if key == "marketplace":
                zube = [chunk for chunk in chunks if "Zube" in chunk.content]
                if not zube or "Agile project management" not in zube[0].content:
                    print("        FAIL: Zube name/description are not in one chunk")
                    failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

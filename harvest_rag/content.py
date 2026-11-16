from __future__ import annotations

import base64
import hashlib
import io
import re

from pipeline.strip import strip_html, looks_like_css_js
from schemas.extraction import ExtractionResult, Section
from harvest_rag.fetch import ReviewRequired


def parse(fetched, logger=None):
    ctype = fetched["content_type"]
    title = None
    main_found = False
    main_method = "body"
    link_ratio = 0.0
    item_count = 0
    if "html" in ctype:
        # Match V1: when a browser render was used, its visible text is a
        # better fallback than lxml's text_content() (which includes CSS/JS).
        stripped = strip_html(fetched["text"], url=fetched["url"],
                              browser_visible_text=fetched.get("visible_text"))
        if logger is not None:
            logger.log_event("strip_attempt", url=fetched["url"],
                             outcome=stripped.outcome.value,
                             details={"method": stripped.method,
                                      "chars_before": stripped.chars_before,
                                      "chars_after": stripped.chars_after,
                                      "num_blocks": stripped.num_blocks,
                                      "main_found": getattr(stripped, "main_found", False),
                                      "main_method": getattr(stripped, "main_method", "body"),
                                      "link_ratio": round(getattr(stripped, "link_ratio", 0.0), 3),
                                      "item_count": getattr(stripped, "item_count", 0)})
        text, title = stripped.text, stripped.title
        main_found = getattr(stripped, "main_found", False)
        main_method = getattr(stripped, "main_method", "body")
        link_ratio = getattr(stripped, "link_ratio", 0.0)
        item_count = getattr(stripped, "item_count", 0)
        sections = [{"heading": b.heading, "level": b.level, "content": b.text}
                    for b in stripped.blocks if b.text.strip()]
    elif "pdf" in ctype:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(base64.b64decode(fetched["raw"])))
        sections = [{"heading": f"Page {i+1}", "content": p.extract_text() or "", "level": 1}
                    for i, p in enumerate(reader.pages)]
        sections = [s for s in sections if s["content"].strip()]
        text = "\n\n".join(s["content"] for s in sections)
    elif ctype.startswith("text/") or any(t in ctype for t in ("json", "xml")):
        text = fetched["text"]
        sections = []
    else:
        raise ReviewRequired(f"Unsupported content type: {ctype}")
    text = text.strip()
    if not sections and text:
        sections = [{"heading": title, "content": text, "level": None}]
    return {"text": text, "title": title, "sections": sections,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "main_found": main_found, "main_method": main_method,
            "link_ratio": link_ratio, "item_count": item_count}


def structured_result(parsed, fetched):
    ctype = "pdf" if "pdf" in fetched["content_type"] else "html" if "html" in fetched["content_type"] else "text"
    return ExtractionResult(source_url=fetched["url"], scrape_timestamp=fetched["scraped_at"],
                            page_title=parsed["title"], content_type=ctype,
                            sections=[Section(**s) for s in parsed["sections"]], confidence=1.0,
                            extraction_notes="Deterministic parser; confidence is not a source-trust score")


def quality(parsed, min_chars):
    text = parsed["text"]
    if len(text) < min_chars:
        raise ReviewRequired(f"Body too short: {len(text)} characters")
    # P0-2: never publish text that is mostly CSS/JS source (fallback leak).
    if looks_like_css_js(text):
        raise ReviewRequired("Extracted text is dominated by CSS/JS boilerplate; review required")
    link_ratio = float(parsed.get("link_ratio", 0) or 0)
    item_count = int(parsed.get("item_count", 0) or 0)
    # Navigation-heavy pages (no item structure, short text, most of it is link
    # labels) are not knowledge-base content. Marketplace/list pages have
    # item_count > 0 and are exempt.
    if item_count == 0 and len(text) < 2500 and link_ratio > 0.28:
        raise ReviewRequired(
            f"Navigation-heavy text (link ratio {link_ratio:.2f}); review required")
    sections = parsed.get("sections") or []
    if (item_count == 0 and len(text) < 800 and link_ratio > 0.15
            and not any(section.get("heading") for section in sections)):
        raise ReviewRequired(
            "Short navigation-like page with no heading structure; review required")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    duplicate_ratio = 1 - len(set(lines)) / max(1, len(lines))
    score = round(max(0, 1 - duplicate_ratio), 3)
    if len(lines) > 10 and duplicate_ratio > 0.8:
        raise ReviewRequired("Excessive repeated lines")
    return {"score": score, "characters": len(text), "duplicate_line_ratio": duplicate_ratio}


def near_similarity(left, right):
    from datasketch import MinHash
    def signature(text):
        words = re.findall(r"\w+", text.lower())
        tokens = [" ".join(words[i:i+3]) for i in range(max(1, len(words)-2))]
        m = MinHash(num_perm=128)
        for token in tokens:
            m.update(token.encode())
        return m
    return signature(left).jaccard(signature(right))

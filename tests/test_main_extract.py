"""Generic main-region extraction, item blocks and navigation quality gate."""
import pytest

from pipeline.strip import strip_html
from v2.content import parse, quality, structured_result
from v2.fetch import ReviewRequired


def article_html() -> str:
    body = ("This article explains the library knowledge base pipeline in detail. " * 8)
    return f"""<html><body>
      <header><nav><a href='/'>Home</a><a href='/products'>Products</a><a href='/pricing'>Pricing</a></nav></header>
      <main><article><h1>Library knowledge base</h1><p>{body}</p>
      <h2>Retrieval</h2><p>Hybrid retrieval combines dense vectors and BM25.</p></article></main>
      <aside><a href='/ad'>Buy now</a></aside>
      <footer><a href='/terms'>Terms</a><a href='/privacy'>Privacy</a></footer>
    </body></html>"""


def cards_html() -> str:
    cards = "".join(
        f"<div class='card'><h3>App {index}</h3><p>Description {index} keeps the app name and "
        f"description together in the same entry.</p></div>"
        for index in range(1, 5)
    )
    return f"""<html><body><nav><a href='/'>Menu</a></nav>
      <main><h1>Marketplace</h1>{cards}</main>
      <footer>Footer links</footer></body></html>"""


def test_main_region_excludes_outside_navigation_and_footer():
    result = strip_html(article_html(), url="https://example.com/article")
    assert result.main_found is True
    assert result.main_method == "main"
    assert "Library knowledge base" in result.text
    assert "Hybrid retrieval" in result.text
    assert "Products" not in result.text
    assert "Buy now" not in result.text
    assert "Privacy" not in result.text


def test_header_inside_main_with_heading_is_kept():
    html = ("<html><body><main><header><h1>Kept title</h1></header>"
            + "<p>" + ("Long article body text. " * 20) + "</p></main></body></html>")
    result = strip_html(html, url="https://example.com/header")
    assert "Kept title" in result.text
    assert result.main_found is True


def test_repeated_cards_become_complete_item_chunks():
    fetched = {"url": "https://example.com/marketplace", "content_type": "text/html",
               "text": cards_html(), "raw": "", "scraped_at": "2026-11-13T10:00:00+00:00"}
    parsed = parse(fetched)
    assert parsed["item_count"] == 4
    chunks = [c for c in __import__("pipeline.chunk", fromlist=["chunk_result"]).chunk_result(
        structured_result(parsed, fetched), max_chunk_chars=2000)]
    assert len(chunks) == 4
    for index, chunk in enumerate(chunks, start=1):
        assert f"App {index}" in chunk.content
        assert f"Description {index}" in chunk.content
        assert chunk.content.count(f"App {index}") == 1


def test_navigation_heavy_short_page_is_flagged():
    parsed = {
        "text": "Home\nProducts\nPricing\nDocs\nBlog\nLogin\nSign up\nAbout\nContact",
        "sections": [{"heading": None, "level": None, "content": "Home Products Pricing"}],
        "link_ratio": 0.55,
        "item_count": 0,
    }
    with pytest.raises(ReviewRequired, match="Navigation"):
        quality(parsed, 10)


def test_code_block_keeps_newlines_and_indentation():
    html = ("<html><body><main><h1>Code</h1>"
            "<p>" + ("Technical documentation paragraph. " * 8) + "</p>"
            "<pre><code>def add(a, b):\n    result = a + b\n    return result\n</code></pre>"
            "<p>Tail paragraph.</p></main></body></html>")
    result = strip_html(html, url="https://example.com/code")
    assert "```" in result.text
    assert "    result = a + b" in result.text
    assert "    return result" in result.text

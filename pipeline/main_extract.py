"""Generic main-content region extraction and item-aware blocks.

This is the structured fallback used when Trafilatura cannot parse a page.
It works from semantics (main/article/role/itemprop), then from container
scoring (text volume, link density, paragraph/heading counts), and finally
falls back to a contextually-cleaned body. It never uses per-site strings.

Noise removal is contextual: header/nav/footer/aside are kept inside the main
region when they carry headings or substantial prose, and removed when they
are link-heavy menus, breadcrumbs or small sidebars.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field

import lxml.html

_NONCONTENT = ("script", "style", "noscript", "template", "svg", "iframe",
               "canvas", "object", "embed")
_NOISE_TOKENS = {"nav", "navigation", "menu", "sidebar", "footer", "breadcrumb",
                 "breadcrumbs", "comment", "comments", "cookie", "consent"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el) -> str:
    return " ".join(" ".join(el.itertext()).split())


def _normalize(text: str) -> str:
    lines = []
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            lines.append(stripped)
            continue
        if in_code:
            lines.append(line.rstrip())
        else:
            lines.append(re.sub(r"[ \t]+", " ", line).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _link_text(el) -> str:
    return " ".join(_text(anchor) for anchor in el.xpath(".//a"))


def _link_ratio(el) -> float:
    text = _text(el)
    if not text:
        return 0.0
    return min(1.0, len(_link_text(el)) / len(text))


def _tokens(el) -> set[str]:
    raw = f"{el.get('id') or ''} {el.get('class') or ''}".lower()
    return set(re.findall(r"[a-z0-9_-]+", raw))


def _remove(el) -> int:
    parent = el.getparent()
    if parent is None:
        return 0
    size = len(_text(el))
    parent.remove(el)
    return size


def _remove_noncontent(root) -> None:
    for tag in _NONCONTENT:
        for el in list(root.xpath(f"//{tag}")):
            _remove(el)


def _is_noise(el, root_chars: int) -> bool:
    tag = _local(el.tag)
    role = (el.get("role") or "").lower()
    if role in {"navigation", "banner", "contentinfo", "complementary", "search"}:
        return True
    if (el.get("aria-hidden") or "").lower() == "true":
        return True
    if "breadcrumb" in _tokens(el) or "breadcrumbs" in _tokens(el):
        return True
    text_len = len(_text(el))
    link_ratio = _link_ratio(el)
    if tag == "nav":
        return link_ratio > 0.5 or text_len < 0.15 * root_chars
    if tag == "footer":
        return link_ratio > 0.4 or text_len < 0.1 * root_chars
    if tag == "aside":
        return link_ratio > 0.35 or text_len < 0.2 * root_chars
    if tag == "form":
        return True
    if tag == "header":
        if el.xpath(".//h1|.//h2|.//h3"):
            return False
        return link_ratio > 0.5
    return False


def _clean_root(root) -> int:
    root_chars = len(_text(root))
    removed = 0
    for el in list(root.iter()):
        if el is root or not isinstance(el.tag, str) or el.getparent() is None:
            continue
        if _local(el.tag) in _NONCONTENT:
            removed += _remove(el)
            continue
        if _is_noise(el, root_chars):
            removed += _remove(el)
    return removed


def _candidate_root(tree):
    candidates = []
    for xpath in ("//main", "//*[@role='main']", "//article",
                  "//*[@role='article']", "//*[@itemprop='articleBody']"):
        for el in tree.xpath(xpath):
            if len(_text(el)) >= 200:
                candidates.append(el)
    if candidates:
        return max(candidates, key=lambda el: len(_text(el))), "main"

    body = tree.body if tree.body is not None else tree
    best, best_score = body, 0.0
    for el in tree.xpath("//div|//section"):
        if el is body:
            continue
        text_len = len(_text(el))
        if text_len < 300:
            continue
        link = _link_ratio(el)
        if link > 0.5:
            continue
        tokens = _tokens(el)
        penalty = 0.4 if tokens & _NOISE_TOKENS else 1.0
        paragraphs = len(el.xpath(".//p"))
        headings = len(el.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6"))
        score = (text_len * (1.0 - link) + paragraphs * 40 + headings * 80) * penalty
        if score > best_score:
            best, best_score = el, score
    return best, ("container" if best is not body else "body")


def _render_table(table) -> str:
    rows = []
    for row in table.xpath(".//tr"):
        cells = [_text(cell) for cell in row.xpath("./th|./td")]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _plain_blocks(root) -> list[dict]:
    blocks: list[dict] = []
    current = {"heading": None, "level": None, "text": ""}
    handled: set = set()

    def flush() -> None:
        nonlocal current
        text = _normalize(current["text"])
        # Short headed blocks are usually cards/list entries; keep the heading
        # in the body so a later chunk never loses the item name.
        if current["heading"] and text and len(text) <= 300 and not text.startswith(current["heading"]):
            text = f"{current['heading']}\n{text}"
        if current["heading"] or text:
            current["text"] = text
            blocks.append(dict(current))
        current = {"heading": None, "level": None, "text": ""}

    def append(value: str) -> None:
        value = " ".join(value.split())
        if value:
            current["text"] = current["text"] + ("\n\n" if current["text"] else "") + value

    def append_code(value: str) -> None:
        code = value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if code.strip():
            fenced = "```\n" + code + "\n```"
            current["text"] = current["text"] + ("\n\n" if current["text"] else "") + fenced

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = _local(el.tag)
        if tag in _HEADINGS:
            flush()
            current["heading"] = _text(el)
            current["level"] = int(tag[1])
        elif tag == "pre":
            if any(ancestor in handled for ancestor in el.iterancestors()):
                continue
            append_code(el.text_content())
            handled.add(el)
        elif tag in {"p", "blockquote"}:
            if any(ancestor in handled for ancestor in el.iterancestors()):
                continue
            append(_text(el))
            handled.add(el)
        elif tag == "li":
            if any(ancestor in handled for ancestor in el.iterancestors()):
                continue
            append(_text(el))
            handled.add(el)
        elif tag == "table":
            if any(ancestor in handled for ancestor in el.iterancestors()):
                continue
            append(_render_table(el))
            handled.add(el)
    flush()
    if not blocks and _text(root):
        blocks = [{"heading": None, "level": None, "text": _normalize(_text(root))}]
    return blocks


def _item_groups(root) -> list:
    groups: dict[tuple[str, str, int], list] = {}
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = _local(el.tag)
        if tag not in {"div", "article", "li", "section"}:
            continue
        text_len = len(_text(el))
        if not 30 <= text_len <= 2000:
            continue
        classes = " ".join(sorted((el.get("class") or "").split()))
        if not classes and not el.get("role"):
            continue
        parent = el.getparent()
        groups.setdefault((tag, classes, id(parent) if parent is not None else 0), []).append(el)

    best = []
    best_score = (0, 0.0)
    for (_, _, _), elements in groups.items():
        unique = {}
        for el in elements:
            digest = hashlib.sha1(_text(el).encode("utf-8", "ignore")).hexdigest()
            unique.setdefault(digest, el)
        elements = list(unique.values())
        if len(elements) < 4:
            continue
        lengths = [len(_text(el)) for el in elements]
        if max(lengths) > 6 * max(1, min(lengths)):
            continue
        headings = sum(1 for el in elements
                       if el.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6"))
        if headings < max(3, int(0.8 * len(elements))):
            continue
        average = sum(lengths) / len(lengths)
        if not 30 <= average <= 600:
            continue
        score = (len(elements), -average)
        if score > best_score:
            best, best_score = elements, score
    return best


def _item_blocks(root) -> tuple[list[dict], int]:
    items = _item_groups(root)
    if not items:
        return _plain_blocks(root), 0

    for el in items:
        el.set("data-v2-item", "1")
    clone = copy.deepcopy(root)
    for el in clone.xpath("//*[@data-v2-item]"):
        _remove(el)
    blocks = _plain_blocks(clone)

    for el in items:
        heading_el = None
        for candidate in el.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6"):
            heading_el = candidate
            break
        heading = _text(heading_el) if heading_el is not None else None
        level = int(_local(heading_el.tag)[1]) if heading_el is not None else None
        content = _normalize(_text(el))
        if heading and not content.startswith(heading):
            content = f"{heading}\n{content}"
        blocks.append({"heading": heading, "level": level, "text": content})
    return blocks, len(items)


@dataclass
class MainExtract:
    text: str = ""
    blocks: list[dict] = field(default_factory=list)
    method: str = "body"
    main_found: bool = False
    link_ratio: float = 0.0
    item_count: int = 0
    nav_removed_chars: int = 0


def extract_main(html: str) -> MainExtract | None:
    try:
        tree = lxml.html.fromstring(html)
    except Exception:
        return None
    _remove_noncontent(tree)
    root, method = _candidate_root(tree)
    removed = _clean_root(root)
    text = _normalize(_text(root))
    blocks, item_count = _item_blocks(root)
    return MainExtract(
        text=text,
        blocks=blocks,
        method=method,
        main_found=method != "body",
        link_ratio=_link_ratio(root),
        item_count=item_count,
        nav_removed_chars=removed,
    )

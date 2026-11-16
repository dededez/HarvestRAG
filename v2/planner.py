from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import httpx
from pydantic import BaseModel, Field

from agents.config_loader import load_agent_config
from agents.llm.client import LiteLLMClient
from agents.llm.env import get_env
from v2.models import CrawlPlan
from v2.urls import canonicalize

logger = logging.getLogger(__name__)


class Selection(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)


class SearchQueries(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=3)


def deepseek_search(query):
    key = get_env("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("自主发现需要本地环境变量 DEEPSEEK_API_KEY")
    with httpx.Client(timeout=30) as http:
        response = http.post("https://api.deepseek.com/v1/tools/search",
                             headers={"Authorization": f"Bearer {key}"},
                             json={"query": query, "count": 10, "need_content": False})
        if not response.is_success:
            raise ValueError(f"DeepSeek 搜索接口返回 HTTP {response.status_code}，请检查账号权限或余额")
        return response.json()["results"]


def _decompose_queries(goal: str, client, max_queries: int = 3) -> list[str]:
    goal = goal.strip()
    if not goal:
        raise ValueError("采集目标不能为空")
    prompt_path = Path(__file__).resolve().parents[1] / "agents/prompts/search_query.txt"
    query_prompt = prompt_path.read_text(encoding="utf-8")
    try:
        value = client.complete_structured(
            messages=[{"role": "system", "content": query_prompt}, {"role": "user", "content": goal}],
            tool_name="search_query",
            tool_schema=SearchQueries.model_json_schema(),
            temperature=0,
            max_tokens=500,
        )
    except Exception as exc:
        logger.warning("Query decomposition failed, falling back to truncated goal: %s", exc)
        return [goal[:128]]
    raw_queries = value.get("queries") if isinstance(value, dict) else None
    if not raw_queries and isinstance(value, dict) and value.get("query"):
        raw_queries = [value["query"]]
    cleaned: list[str] = []
    for item in raw_queries or []:
        q = str(item).strip()[:128]
        if q and q not in cleaned:
            cleaned.append(q)
        if len(cleaned) >= max_queries:
            break
    return cleaned or [goal[:128]]


def _parallel_search(queries: list[str], search_fn) -> list[list[dict]]:
    if len(queries) == 1:
        return [search_fn(queries[0])]
    results: list[list[dict]] = []
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=min(3, len(queries))) as pool:
        futures = [pool.submit(search_fn, q) for q in queries]
        for fut in futures:
            try:
                results.append(fut.result())
            except Exception as exc:
                errors.append(exc)
    if not results and errors:
        raise errors[-1]
    return results


def _merge_candidates(per_query_results: list[list[dict]], max_candidates: int = 25) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for batch in per_query_results:
        for rank, result in enumerate(batch or []):
            try:
                url = canonicalize(result["url"])
            except (KeyError, TypeError, ValueError):
                continue
            entry = merged.get(url)
            if entry is None:
                entry = {
                    "title": str(result.get("title") or ""),
                    "hits": 0,
                    "best_rank": rank,
                }
                if result.get("snippet"):
                    entry["snippet"] = str(result["snippet"])[:300]
                merged[url] = entry
            else:
                if not entry["title"] and result.get("title"):
                    entry["title"] = str(result["title"])
                entry["best_rank"] = min(entry["best_rank"], rank)
            entry["hits"] += 1
    ranked = sorted(merged.items(), key=lambda kv: (-kv[1]["hits"], kv[1]["best_rank"]))
    return dict(ranked[:max_candidates])


def discover(goal, max_pages=50, max_depth=1, *, search=None, client=None,
             multi_query=True):
    goal = goal.strip()
    if not goal:
        raise ValueError("采集目标不能为空")
    cfg = load_agent_config()
    client = client or LiteLLMClient(cfg)
    if search is None:
        queries = _decompose_queries(goal, client) if (multi_query or len(goal) > 128) else [goal]
        per_query_results = _parallel_search(queries, deepseek_search)
    else:
        queries = [goal]
        per_query_results = [search(goal)]
    candidates = _merge_candidates(per_query_results)
    if not candidates:
        raise ValueError("Search returned no valid HTTP(S) URLs")
    prompt = (Path(__file__).resolve().parents[1] / "agents/prompts/plan.txt").read_text(encoding="utf-8")
    result = client.complete_structured(
        messages=[{"role": "system", "content": prompt},
                  {"role": "user", "content": json.dumps({
                      "goal": goal,
                      "queries": queries,
                      "search_results": candidates,
                  }, ensure_ascii=False)}],
        tool_name="select_sources",
        tool_schema=Selection.model_json_schema(),
        temperature=0,
        max_tokens=1500,
    )
    selected = Selection.model_validate(result)
    urls = list(dict.fromkeys(canonicalize(u) for u in selected.urls))
    if any(u not in candidates for u in urls):
        raise ValueError("Planner selected a URL not present in search evidence")
    return CrawlPlan(
        goal=goal,
        seed_urls=urls[:max_pages],
        max_pages=max_pages,
        max_depth=max_depth,
        discovery_strategy="links+sitemap" if max_depth else "seeds",
    )

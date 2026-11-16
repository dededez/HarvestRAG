from types import SimpleNamespace
import httpx
import pytest
from harvest_rag import planner


def test_search_uses_deepseek_key_and_official_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-secret")
    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        assert request.headers["authorization"] == "Bearer test-deepseek-secret"
        return httpx.Response(200, json={"results": [{"url": "https://docs.python.org/", "title": "Python"}]})
    original = httpx.Client
    monkeypatch.setattr(planner.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    assert planner.deepseek_search("Python docs")[0]["title"] == "Python"


def test_long_goal_is_compressed_before_search(monkeypatch):
    seen = []
    monkeypatch.setattr(planner, "deepseek_search", lambda q: seen.append(q) or [{"url": "https://docs.python.org/"}])
    def complete(**kwargs):
        return {"query": "Python official documentation"} if kwargs["tool_name"] == "search_query" else {"urls": ["https://docs.python.org/"]}
    plan = planner.discover("Python documentation " * 20, client=SimpleNamespace(complete_structured=complete))
    assert seen == ["Python official documentation"]
    assert plan.seed_urls == ["https://docs.python.org/"]


def test_search_error_does_not_expose_credentials(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-secret")
    original = httpx.Client
    monkeypatch.setattr(planner.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw))
    with pytest.raises(ValueError, match="401") as exc:
        planner.deepseek_search("Python")
    assert "test-deepseek-secret" not in str(exc.value)


def test_multi_query_decomposition_merges_and_deduplicates_candidates(monkeypatch):
    searched = []
    captured_candidates = {}

    def fake_search(q):
        searched.append(q)
        if "overview" in q:
            return [
                {"url": "https://docs.python.org/3/#top", "title": "Python Docs"},
                {"url": "https://realpython.com/async/", "title": "Async Guide"},
            ]
        return [
            {"url": "https://docs.python.org/3/", "title": "Python Docs"},
            {"url": "https://peps.python.org/pep-0492/", "title": "PEP 492"},
        ]

    monkeypatch.setattr(planner, "deepseek_search", fake_search)

    def complete(**kwargs):
        if kwargs["tool_name"] == "search_query":
            return {"queries": ["Python asyncio overview", "Python asyncio PEP 492", "Python asyncio overview"]}
        payload = __import__("json").loads(kwargs["messages"][1]["content"])
        captured_candidates.update(payload["search_results"])
        return {"urls": ["https://docs.python.org/3/", "https://peps.python.org/pep-0492/"]}

    plan = planner.discover(
        "Python asyncio architecture",
        client=SimpleNamespace(complete_structured=complete),
    )
    assert searched == ["Python asyncio overview", "Python asyncio PEP 492"]
    assert list(captured_candidates.keys())[0] == "https://docs.python.org/3/"
    assert captured_candidates["https://docs.python.org/3/"]["hits"] == 2
    assert len(captured_candidates) == 3
    assert plan.seed_urls == ["https://docs.python.org/3/", "https://peps.python.org/pep-0492/"]


def test_multi_query_partial_search_failure_still_succeeds(monkeypatch):
    def fake_search(q):
        if "fail" in q:
            raise ValueError("temporary search error")
        return [{"url": "https://example.org/guide", "title": "Guide"}]

    monkeypatch.setattr(planner, "deepseek_search", fake_search)

    def complete(**kwargs):
        if kwargs["tool_name"] == "search_query":
            return {"queries": ["fail query", "working query"]}
        return {"urls": ["https://example.org/guide"]}

    plan = planner.discover("Example topic", client=SimpleNamespace(complete_structured=complete))
    assert plan.seed_urls == ["https://example.org/guide"]

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from syvrio.agent.network_tools import SEARCH_NOTICE, register_network_tools
from syvrio.agent.registry import ToolRegistry
from syvrio.infra.search.base import FetchResult, ProviderError, SearchResult
from syvrio.infra.search.fetch_router import FetchRouter
from syvrio.infra.search.pauses import ProviderPauses
from syvrio.infra.search.router import SearchRouter


@pytest.mark.parametrize("tool", ["search", "fetch_web"])
@pytest.mark.parametrize("fallback", [True, False])
def test_model_and_tool_display_receive_quota_notice(service, tool, fallback):
    url = "https://1.1.1.1/sample"
    error = ProviderError("exa", "quota", "NO_MORE_CREDITS", pause_reason="NO_MORE_CREDITS")
    exhausted = Mock(capabilities={"search", "fetch"})
    exhausted.search.side_effect = exhausted.fetch.side_effect = error
    providers = {"exa": exhausted}
    if fallback:
        available = Mock(capabilities={"search", "fetch"}, last_partial_failures=[])
        available.search.return_value = [SearchResult("Synthetic report", url, "Synthetic report")]
        available.fetch.return_value = FetchResult(url, url, "Synthetic report", "Content")
        providers["backup"] = available
    pauses = ProviderPauses(service.paths)
    registry = ToolRegistry()
    register_network_tools(
        registry,
        service.config.search,
        service.config.fetch,
        service.paths,
        5,
        SearchRouter(providers, provider_pauses=pauses),
        FetchRouter(providers, provider_pauses=pauses),
    )
    args = {"query": "Synthetic report"} if tool == "search" else {"url": url}
    outcome = registry.execute(tool, args)
    payload = json.loads(outcome.content.removeprefix(SEARCH_NOTICE + "\n"))
    assert outcome.ok is fallback
    assert payload["quota_exhausted"] == ["exa"]
    assert "Quota exhausted: exa" in outcome.display
    if fallback:
        assert payload["provider"] == "backup"
    schema = next(
        item["function"] for item in registry.schemas() if item["function"]["name"] == tool
    )
    assert "quota_exhausted" in schema["description"]

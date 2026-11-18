"""多厂商 Search/Fetch 集成回归（卡 13）：全部走假件与 Mock，不联网。"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from syvrio.infra.config import Config, FetchConfig, LLMConfig, SearchConfig
from syvrio.infra.search.base import (
    FetchResult,
    SearchResult,
)
from syvrio.infra.search.fetch_router import (
    build_fetch_registry,
)
from syvrio.infra.search.router import _EXTERNAL_PROVIDERS, build_registry

pytestmark = pytest.mark.search

KEY = "test-key-placeholder"

_SEARCH_EXTERNALS = {"baidu_search", "tavily", "exa", "you", "volcengine"}
_FETCH_EXTERNALS = {"tavily", "exa", "you", "volcengine"}


def _install_keys_and_empty_keyring(monkeypatch):

    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, name: None
    monkeypatch.setitem(sys.modules, "keyring", fake)
    for env_name in {entry[1] for entry in _EXTERNAL_PROVIDERS.values()}:
        monkeypatch.setenv(env_name, KEY)


def test_all_five_search_providers_register_with_keys(monkeypatch):
    _install_keys_and_empty_keyring(monkeypatch)
    config = SearchConfig(providers=["open_websearch", *_SEARCH_EXTERNALS])
    registry = build_registry(config)
    assert _SEARCH_EXTERNALS <= set(registry)
    assert registry["open_websearch"].name == "open_websearch"
    # Exa Search 保持固定 type=auto 的请求契约。
    assert "search" in registry["exa"].capabilities


def test_all_four_fetch_providers_register_with_keys(monkeypatch):
    _install_keys_and_empty_keyring(monkeypatch)
    config = SearchConfig()
    registry = build_fetch_registry(
        FetchConfig(providers=["open_websearch", *_FETCH_EXTERNALS]), config
    )
    assert _FETCH_EXTERNALS <= set(registry)
    assert "fetch" in registry["open_websearch"].capabilities


def test_search_and_fetch_orders_are_independent(monkeypatch):
    _install_keys_and_empty_keyring(monkeypatch)
    search_config = SearchConfig(providers=["tavily", "exa"])
    fetch_config = FetchConfig(providers=["volcengine"])
    assert set(build_registry(search_config)) == {"tavily", "exa"}
    assert set(build_fetch_registry(fetch_config, search_config)) == {"volcengine"}


def test_unkeyed_provider_never_registers_or_fires(monkeypatch):
    _install_keys_and_empty_keyring(monkeypatch)
    monkeypatch.delenv("TAVILY_API_KEY")
    search_config = SearchConfig(providers=["open_websearch", "tavily"])
    assert "tavily" not in build_registry(search_config)

    fetch_config = FetchConfig(providers=["tavily"])
    assert "tavily" not in build_fetch_registry(fetch_config, search_config)


def test_agent_tool_names_and_parameters_compat(tmp_path):
    """search / fetch_web 工具名与参数结构保持兼容。"""
    from syvrio.agent.session import SessionStore
    from syvrio.agent.tools import build_tools
    from syvrio.infra.config import ChatConfig, Config, LLMConfig
    from syvrio.memory.service import MemoryService

    class _FetchOnly:
        name = "fake"
        capabilities = {"search", "fetch"}
        supports_fetch = True

        def search(self, query):
            return [SearchResult("标题", "https://example.com", "摘要")]

        def fetch(self, query):
            return FetchResult(query.url, query.url, "Page", "正文")

    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(),
        search=SearchConfig(enabled=True),
        fetch=FetchConfig(),
    )
    service = MemoryService(config)
    service.init_vault()
    session = SessionStore(config.vault).create()
    tools = build_tools(service, session, search_router=_FetchOnly())

    schemas = {schema["function"]["name"]: schema["function"] for schema in tools.schemas()}
    assert "search" in schemas and "fetch_web" in schemas
    fetch_params = schemas["fetch_web"]["parameters"]
    assert set(fetch_params["properties"]) == {"url", "max_chars"}
    assert fetch_params["required"] == ["url"]


def _load_smoke_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_search_apis.py"
    spec = importlib.util.spec_from_file_location("smoke_search_apis", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_script_dry_run_lists_rows_without_network(monkeypatch, tmp_path, capsys):
    module = _load_smoke_module()
    _install_keys_and_empty_keyring(monkeypatch)
    config = Config(
        vault=Path(tmp_path),
        llm=LLMConfig(),
        search=SearchConfig(providers=["open_websearch", "tavily"]),
        fetch=FetchConfig(),
    )
    exit_code = module.main(["--provider", "tavily", "--operation", "search"], config=config)
    output = capsys.readouterr().out
    report = json.loads(output)
    assert exit_code == 0
    assert report["mode"] == "dry"
    assert report["rows"][0]["provider"] == "tavily"
    assert report["rows"][0]["key_present"] is True
    assert report["rows"][0]["would_register"] is True


def test_smoke_script_live_uses_injected_fake(monkeypatch, tmp_path, capsys):
    module = _load_smoke_module()
    _install_keys_and_empty_keyring(monkeypatch)

    class FakeSearchProvider:
        name = "tavily"
        capabilities = {"search"}
        calls = 0

        def search(self, query):
            type(self).calls += 1
            return [SearchResult("T", "https://example.com", "")]

    module.build_registry = lambda config: {"tavily": FakeSearchProvider()}
    config = Config(
        vault=Path(tmp_path), llm=LLMConfig(), search=SearchConfig(), fetch=FetchConfig()
    )
    exit_code = module.main(
        ["--provider", "tavily", "--operation", "search", "--live"], config=config
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["mode"] == "live"
    assert output["reports"][0]["ok"] is True
    assert output["reports"][0]["result_count"] == 1
    assert FakeSearchProvider.calls == 1


def test_smoke_script_live_requires_single_provider(tmp_path, capsys):
    module = _load_smoke_module()
    config = Config(
        vault=Path(tmp_path), llm=LLMConfig(), search=SearchConfig(), fetch=FetchConfig()
    )
    exit_code = module.main(["--live"], config=config)
    assert exit_code == 2
    assert "--provider" in capsys.readouterr().out


@pytest.mark.parametrize(
    "name,key_env",
    [
        ("anysearch", "ANYSEARCH_API_KEY"),
        ("parallel_mcp", "PARALLEL_API_KEY"),
    ],
)
def test_fetch_smoke_dry_optional_key_and_one_live_call(
    monkeypatch,
    tmp_path,
    capsys,
    name,
    key_env,
):
    module = _load_smoke_module()
    _install_keys_and_empty_keyring(monkeypatch)
    monkeypatch.delenv(key_env, raising=False)
    config = Config(vault=tmp_path, fetch=FetchConfig(providers=[name]))
    args = ["--provider", name, "--operation", "fetch"]
    assert module.main(args, config=config) == 0
    (row,) = json.loads(capsys.readouterr().out)["rows"]
    assert row["operation"] == "fetch" and row["provider"] == name
    assert row["key_present"] is False and row["would_register"] is True

    calls = []

    def fetch(query):
        calls.append(query.url)
        return FetchResult(query.url, query.url, "Title", "Body")

    monkeypatch.setattr(
        module,
        "build_fetch_registry",
        lambda *_: {
            name: types.SimpleNamespace(fetch=fetch),
        },
    )
    monkeypatch.setattr(module, "assert_public_fetch_url", lambda url: None)
    assert module.main([*args, "--live", "--max-requests", "3"], config=config) == 0
    (row,) = json.loads(capsys.readouterr().out)["reports"]
    assert row["ok"] and row["requests"] == 1
    assert calls == ["https://example.com"]

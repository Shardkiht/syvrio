<div align="center">

<img src="web/public/syvrio.svg" alt="Syvrio logo" width="112" />

# Syvrio

**A local-first memory agent that remembers you.**

Turn everyday conversations into long-term Markdown memories you can inspect, trace, and roll back. Use the terminal chat, the private web client, or connect an MCP client.

[![test](https://github.com/Shardkiht/syvrio/actions/workflows/test.yml/badge.svg)](https://github.com/Shardkiht/syvrio/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/syvrio)](https://pypi.org/project/syvrio/)
![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

[中文](README.md) | **English**

</div>

Stop repeating your preferences and plans every time you start a chat. Tell it “I prefer unsweetened hot drinks,” open another conversation, and it can look that preference up. Open the Markdown files to inspect what it remembers, correct a fact, or retire an entry while preserving its history.

![Syvrio Web: save a drink preference and recall it in a new chat](docs/assets/syvrio-demo.gif)

Recorded in the actual Web client with an isolated synthetic Vault and local model replay. The app performs the memory write, cross-session retrieval, and streaming. [View the still screenshot](docs/assets/syvrio-demo.png).

## Quick start

These instructions describe the current source checkout. With Python 3.12+, uv and Git installed, run the following from the repository directory.

```sh
uv sync --frozen
uv run syvrio setup
uv run syvrio chat
```

Setup selects the Vault and model. Supply provider credentials using the system credential store or the provider's environment variable. Sending the first message creates a session. The terminal, web interface, CLI and MCP client connect to the same resident backend and share sessions, memories, job progress and configuration.

To run the web interface from source, install Node.js and Corepack, build the assets once, then start the server.

```sh
uv run python scripts/build_web.py
uv run syvrio web
```

The default URL is `http://127.0.0.1:8765/`. Use `syvrio backend status` to inspect the backend for the current Vault, and `syvrio backend start` or `syvrio backend stop` to manage it. The web guide covers deployment, LAN access and authentication. [Web client guide](docs/web.md)

## Chat and attachments

- **Sending and undoing.** Press Enter during a reply to queue a message for the end of the turn. Ctrl+Enter requests insertion at a safe point in the current turn. Tool calls must finish before insertion. `/undo` interrupts the latest turn, restores eligible local file changes and puts the sent text back in the composer. In the browser, hover over the latest user message to undo or edit and resend it. Later edits or references from another session can block the entire undo.
- **Documents and images.** Click the circular **+** inside the web composer or press **Ctrl+O** in the TUI to attach local files. Each message supports up to eight files, 20 MiB per file and 40 MiB in total. Models can read documents. Vision-capable models can also analyze PNG, JPEG, WebP and GIF images. The `/import` chat command opens the same local file picker as **+**. Selected files stay in the draft until you press Send or Enter. The model can then read or summarize the files, or save facts when you ask it to.
- **Sessions and models.** Browse and manage conversations with `/sessions`. Search the available model list with `/model` and select a supported reasoning level with `/thinking`. Reasoning and tool details can be expanded in the conversation. Type `/` to see the commands available in your client.
- **Context compaction.** Long conversations use summaries to fit the model's context window. Use `/compact` to request a summary. The original messages remain in the session JSONL. Background distillation processes each original user message and commits its progress with the facts it saved. Switching sessions, exiting or restarting the backend can resume unfinished work.

## Memory, review and profiles

Formal memories are Markdown entries organized by topic. Each has a stable ID, text, topic and status. Optional context covers event date, validity dates, location and feelings, including separate wording for the event and for later recollection. Dates retain the stated precision of year, month or day. A chat-derived entry links to its source session and user message. Recall returns formal facts and supports date ranges, location and emotion filters.

| Action | Result |
| --- | --- |
| Record and manage a fact | `memorize` saves a fact. `correct` corrects an error. `transition` records a change over time. `deprecate` retires a fact while retaining its history. |
| `/pending` | Select a candidate to review its question and related facts. Confirm the draft, edit its text and confirm, or reject it. Direct approval retains its context. For edited text, a background job can fill empty context fields from the final fact and its trusted source. |
| `/inferences` | Review hypotheses with a category and source fact IDs. Confirmed inferences are stored separately from formal facts. |
| `/profile` | Read the most recently published full profile. |
| `/dream` | First update the full profile from active facts and confirmed inferences, then generate new hypotheses in batches. Use `/dream status`, `/dream cancel` and `/dream resume` to inspect, cancel or resume a job. |

A model proposes a pending item when a question still needs the user's decision after reviewing recalled facts and active candidates. Invalid input such as credentials is blocked at write time. Deprecation preserves memory IDs. An undo of the latest turn can restore the previous files, including removal of a newly created ID, after checking for intervening changes and references.

## CLI and MCP

These commands use the resident backend for the current Vault. Run `syvrio --help` for the full command reference.

```sh
syvrio memorize "I prefer unsweetened hot drinks" --topic preferences
syvrio recall "drink preferences"
syvrio recall "museum visits" --from 2024 --to 2025 --location Hangzhou
syvrio pending
syvrio profile
syvrio dream --requests 5
syvrio status
```

The standalone `syvrio import ./notes` command accepts a document or a directory. Directory imports read files at the top level. By default it calls the model and previews the results. Add `--apply` to write them to the Vault.

For a client that supports MCP over stdio, add the following configuration.

```json
{
  "mcpServers": {
    "syvrio": { "command": "syvrio", "args": ["serve"] }
  }
}
```

The MCP server exposes recall, topic reading, recording, correction, transitions, deprecation and pending review. Model-initiated writes first review relevant facts and active pending items. The client exposes the individual tool descriptions.

## Vault and backups

| Data | Location |
| --- | --- |
| Formal memories and active candidates | `memories/<topic>.md` and `memories/pending.md` |
| Rejected candidates, inferences and full profile | `archived/pending.md`, `inferences/<kind>/<ID>.md` and `profile.md` |
| Sessions and formal memory changes | `sessions/YYYY/MM/<ID>.jsonl` and `records/memory-changes.jsonl` |
| Original attachments belonging to each session | `attachments/`, excluded from Vault Git sync |

To move a Vault with attachments, copy the whole Vault directory or back up and restore `attachments/` separately. Permanent session deletion lists related candidates and, when confirmed, removes them along with that session's attachment copies. A reference from formal memory blocks deletion. Indexes, locks and background jobs live in a separate Runtime directory. Optional Git synchronization runs alongside the resident backend. [Vault format](docs/vault-v2.md)

## Configuration and Git

Use `syvrio setup` to configure the model, API base URL and Vault. `SYVRIO_CONFIG`, `SYVRIO_VAULT` and `SYVRIO_RUNTIME` select the configuration file, Vault and Runtime respectively. Retrieval supports keywords, local embeddings and embedding APIs. An optional `SOUL.md` holds a user-written assistant persona. For a Vault with a Git remote, `/git` shows status and synchronization actions. Back up attachments separately.

## Web search

The `search` tool queries public web pages through a local [open-websearch](https://github.com/Aas-ee/open-webSearch) daemon by default and needs no API key. Start the daemon in a separate terminal:

```bash
npx -y open-websearch@latest serve
# or: npm i -g open-websearch && open-websearch serve
```

Search results contain only titles, snippets, and links; `fetch_web` opens one of those links and extracts the page text (readability, up to 8000 characters) to confirm dates, details, or snippets. open-websearch fetching is available over the HTTP transport only; the MCP interface still exposes just `search`. The fetch entry point accepts only public http(s) URLs, rejects localhost, private, and link-local addresses, and re-validates the final URL after redirects; fake-IP proxy DNS answers in `198.18.0.0/15` and `2001:2::/48` are allowed by default for hostnames only; literal non-public IP URLs remain blocked. `FAKE_IP_CIDRS` overrides these defaults (an empty value disables compatibility). Automatically started daemons inherit the same setting; an already running external daemon needs the matching setting and a restart. Automatic startup prefers an installed `open-websearch` on PATH, falling back to `npx open-websearch@latest` when it is absent.

### AnySearch and Parallel MCP

AnySearch and Parallel MCP both support Search and Fetch. Enable them independently in `[search].providers` or `[fetch].providers`. Both operations use the same optional API key:

- `anysearch`: calls AnySearch REST search (`https://api.anysearch.com/v1/search`). It sends no `Authorization` header without a key; setting `ANYSEARCH_API_KEY` switches to Bearer authentication. `zone` and `language` come from `anysearch_zone` / `anysearch_language` under `[search]`, defaulting to `cn` / `zh-CN`. The original query is never rewritten.
- `parallel_mcp`: Search keeps `web_search` on the standalone Search MCP (`https://search.parallel.ai/mcp`), which Parallel still supports for compatibility. Setting `PARALLEL_API_KEY` adds Bearer authentication to both Search and Fetch. Search prefers `structuredContent.results` and also accepts JSON in `content[].text`; duplicate URLs are merged, long excerpts are truncated, and unverifiable publish dates remain empty.

Example (the default order is unchanged; a provider is enabled only when explicitly listed):

```toml
[search]
providers = ["parallel_mcp", "anysearch", "open_websearch"]

[fetch]
providers = ["anysearch", "parallel_mcp", "open_websearch"]
```

Search uses the existing quality gate, fallback chain, request budget, cache and `UsageLimiter`. Each has a local default protection cap of 20 requests per day, adjustable through `[search.usage_limits.<provider>]`; 0 means unlimited. These values are local request protection, not vendor free-tier entitlements or billing caps, and a configured key may incur vendor-side charges. Authentication failures (401/403) are reported explicitly and fall back to the next provider; the key is never silently dropped to retry anonymously. Search and Fetch share the vendor counter. When the vendor reports exhausted quota, Syvrio pauses it and tries the next configured Fetch provider.

- **AnySearch Fetch** posts the URL to the [Extract API](https://www.anysearch.com/docs/api-endpoints/v1-extract) at `https://api.anysearch.com/v1/extract`, extracts `data.content`, and strips HTML.
- **Parallel Fetch** calls `web_fetch` through [Parallel MCP 2.0](https://docs.parallel.ai/integrations/mcp/quickstart) at `https://mcp.parallel.ai/mcp`. It requests Markdown for one URL and uses excerpts if full text is unavailable.

The adapters use the redirect destination when the service reports it, otherwise they keep the requested URL without probing for redirects. Fetch validates the input and any reported destination and truncates at `max_chars`. OpenWebSearch (HTTP), Tavily, Exa, You.com, and Volcengine continue to support Fetch; Baidu Qianfan and SearXNG are search-only.

The user previously verified both anonymous endpoints from the current mainland-China network with proxy-free curl, but that only reflects that network at that time. Availability across carriers, times and regions still requires live verification; the code does not require a VPN or extra proxy. `syvrio setup` can enable either anonymous provider without entering a key.

### Self-hosted SearXNG

`searxng` treats the whole self-hosted instance as one provider. Engines such as Baidu, Bing and DuckDuckGo inside the instance are aggregated by SearXNG itself; Syvrio does not add a separate adapter for each internal engine. It is disabled by default and never calls a public instance by default — Syvrio only requests it after `searxng` is added to `search.providers` and a self-hosted address is configured:

```toml
[search]
providers = ["searxng", "open_websearch"]
searxng_base_url = "http://127.0.0.1:8888"
searxng_timeout = 10.0
```

SearXNG's JSON API may be disabled out of the box. Minimal local deployment example (Syvrio never installs or starts Docker/WSL/Redis/SearXNG):

```bash
docker run --rm -d --name searxng -p 8888:8080 \
  -v "$PWD/searxng:/etc/searxng" searxng/searxng
```

After the first start, edit `settings.yml` in the mounted directory to enable `json` under `search.formats`, then restart the container:

```yaml
search:
  formats:
    - html
    - json
```

When JSON is not enabled, `format=json` may return HTTP 403. Syvrio reports this as a SearXNG configuration error and points at `search.formats`; it is never treated as a normal empty result. A connection failure produces a hint to check whether the instance is running and whether `searxng_base_url` is reachable. 429 and 5xx responses are treated as upstream failures and fall back to the next provider.

Network namespace caveat: `127.0.0.1` in Windows, WSL and Docker does not necessarily point at the same service. With `-p 8888:8080`, Syvrio running on the Docker host can use `http://127.0.0.1:8888`; if Syvrio runs in a different WSL distribution or container, use an address reachable from that environment (such as `host.docker.internal` or the actual gateway IP) instead of assuming `127.0.0.1` works.

The adapter only calls the configured instance address and never treats URLs inside a query as service addresses. It does not follow HTTP redirects, caps the response body at 2 MiB, and bounds each request by both `searxng_timeout` and the global `timeout_budget`. `SearchQuery.lang` maps to `language`, while `freshness` maps to `time_range` only for values SearXNG supports (day/week/month/year); values that cannot be mapped equivalently are omitted. Multiple internal engines still share Syvrio's unified deduplication, quality gate, request budget and result-count cap, and partial engine failures are retained in `failed`. SearXNG provides search only, not `web_fetch`; page fetching still follows the `[fetch]` settings.

Bing sometimes splits Chinese proper nouns into single characters (for example, 「智谱」 is searched as 「智」). When results are obviously off-topic, set `engines = ["baidu"]` in the config, or have the model retry with `search_engine = "baidu"` in the `search` tool. `search_engine` also accepts an array such as `["baidu", "bing", "sogou"]`, merged within one request, with no engine-count limit by default; set `search.max_search_engines` to cap an explicit list (0 means unlimited); the MCP `search` tool supports the same.

Automatic fallback: search is progressive — each request pulls more candidates from the upstream engine than will be displayed (3x the display count, 10-30), merging them into a unified candidate pool that is deduplicated and reranked by deterministic rules. When the primary engine (the first entry of `engines`) returns nothing, results miss the query's core terms or match them too weakly (the candidate pool falls short of the early-stop bar), or a request fails, the router retries through `fallback_engines`, trying at most `max_engines` engines; all fallbacks share the `timeout_budget`, each single request's timeout is capped by the remaining budget, and the router returns the candidates collected by the total deadline and does not cache truncated results, and all providers together are capped by `max_requests` upstream requests per search — once exceeded, the best candidates so far are returned with an explicit truncation hint (such results are not cached, so the next search issues fresh requests). For short person names, handles, or product codenames the `search` tool accepts `context` (proper nouns confirmed in the conversation) and can derive up to two query variants within the same request budget; an explicit `search_engine` keeps single-request semantics and never switches engines on its own.

The LLM may pass `providers` to the `search` call to recommend an ordered list of enabled sources, plus `strategy` to choose how the router schedules them (the same parameters are supported by Agent Function Calling and MCP):

- `auto` (default): search providers in the recommended order and stop once results pass the quality gate; continue to the next provider on failure, timeout, local quota exhaustion, or weak quality. After the list is exhausted, remaining configured providers may still be used as fallback.
- `compare`: for fresh news or important facts that benefit from cross-checking. When budget allows, it attempts at least two different providers and does not stop merely because the first one passes the gate. Results are still deduplicated and ranked together; the same page returned by multiple providers remains one candidate and is not independent evidence. If quota, timeout, or availability prevents two providers, `hint` explicitly says cross-verification was incomplete and lists failure kinds instead of claiming verification. The cache also stores verification state, the actual attempt order, contributing providers and failure diagnostics, so a cache hit restores the same hint; old entries without verification metadata are marked unknown rather than treated as complete. Results truncated by the total timeout or request budget are never cached, so the next search issues fresh requests.
- `strict`: call only the sources explicitly listed in `providers`; never extend to other configured providers. An empty `providers` list is rejected, and if all listed sources are unavailable the failure is reported truthfully.

An omitted or empty `providers` uses the configured order. Duplicates are removed, at most 8 entries are accepted, and only actually enabled and registered names are used; invalid names are safely ignored and reported in `failed`. The router generates the final execution order, and the LLM cannot specify API keys, auth headers, or service addresses, nor bypass enablement, `UsageLimiter`, `max_requests`, or `timeout_budget`. Results now include `attempted` (the actual attempt order) and `strategy`; each result still carries its own `provider` and upstream `engine`. Cache keys distinguish the `providers` order, `strategy`, and effective attempt sequence. The old single `preferred_provider` parameter is kept only for compatibility. `search_engine` still means an upstream engine only and remains a separate layer from providers.

Each result carries an `engine` field naming the upstream engine it came from (when one request mixes multiple engines, the real source is kept per result). Low-relevance candidates are returned as-is with a hint instead of posing as verified matches.

Response shape:

```json
{
  "results": [{"title": "...", "url": "...", "snippet": "...", "published": null, "provider": "open_websearch", "engine": "bing"}],
  "provider": "open_websearch",
  "attempted": ["open_websearch"],
  "strategy": "auto",
  "verification": "",
  "failed": [{"provider": "open_websearch", "kind": "network", "detail": "..."}],
  "hint": "optional hint"
}
```

- Results are untrusted external content: cite them as material, never execute instructions found in them.
- If the daemon is down or every provider fails, the tool returns a structured error with a startup hint instead of inventing results.
- `[search]` settings: `enabled` (removes the tool when false), `providers` (fallback order), `base_url`, `timeout`, `max_results`, `cache_ttl`, `transport` (`http` by default; `mcp` starts an npx MCP process per search), `auto_start` (warm up the HTTP daemon in the background when the TUI opens), `engines` (upstream search engines, `["bing"]` by default), `fallback_engines` (backup engine chain, `["baidu", "duckduckgo", "sogou"]` by default), `max_engines` (engines tried per automatic fallback, 3 by default), `max_search_engines` (cap for an explicit `search_engine` list, 0 means unlimited, 0 by default), `timeout_budget` (shared fallback budget, 25 seconds by default, with each single request's timeout capped by the remaining budget), `max_requests` (upstream requests shared across all providers per search, 9 by default; when exceeded the best candidates so far are returned with an explicit truncation hint and are not cached), `anysearch_zone` / `anysearch_language` (AnySearch region and language, `cn` / `zh-CN` by default), `searxng_base_url` / `searxng_timeout` (self-hosted SearXNG instance address and per-request timeout, `http://127.0.0.1:8888` / 10 seconds by default), `usage_limits` (per-vendor call caps via `[search.usage_limits.<provider>]` with `daily` and `monthly` (0 means unlimited); a vendor's Search and Fetch share one counter pool, and once the cap is hit that vendor is skipped while fallback continues normally; counters persist in `runtime/state/usage.json` outside the Vault — **local counts are not the vendor's actual billing cap**), and `fetch_timeout` (separate timeout for fetching page text, 45 seconds by default). `syvrio setup` manages both periods and official quota pauses.

Controlled real-network smoke test: `scripts/smoke_search_apis.py` by default only performs local checks (key readability, whether each vendor would register; anonymous providers are marked registrable without a key) and never sends a request; adding `--provider <vendor> --operation search|fetch --live` sends at most `--max-requests` (default 1, cap 10) real requests to a single vendor and prints a desensitized report (success, result count or content length, error kind, duration and unverified fields). Keys are read only from environment variables or the system credential store and never echoed. Page fetching is controlled by a separate `[fetch]` section: `providers` is the fetch provider order (`["open_websearch"]` by default), independent of the search provider order; official vendors with a configured key (tavily / exa / you / volcengine) join fetching only once listed here.
- A daemon started by `auto_start` has no visible console window on Windows and stays resident for reuse after the TUI exits.
- Search results are cached in `runtime/cache/search/search-cache.json` as derived data that can be deleted. Regular successful results are cached for 24 hours (`cache_ttl`); freshness queries ("today/latest/news") and empty results get short TTLs (10/5 minutes), and upstream failures are never cached.


Fetching independently follows `[fetch].providers`. Official fetch adapters and open-websearch use `search.fetch_timeout`; search uses `search.timeout`. Only open-websearch fetching requires HTTP transport; configured official fetch adapters are independent of that transport.

### Local search API limits and official quota pauses

These runtime request-count protection defaults also apply when providers are enabled by editing TOML. They are not vendor free-tier or billing entitlements.

| Provider | Daily | Monthly |
| --- | ---: | ---: |
| baidu_search | 50 | 0 |
| tavily | 0 | 800 |
| exa | 0 | 1000 |
| you | 0 | 1500 |
| volcengine | 0 | 450 |
| anysearch | 20 | 0 |
| parallel_mcp | 20 | 0 |
| searxng | 0 | 0 |
| open_websearch | 0 | 0 |

Use `daily` / `monthly` under `[search.usage_limits.<provider>]` to override defaults. Explicit zero disables that period's cap. Either exhausted period blocks dispatch. Search and Fetch share counts; cache hits are free of local charges; dispatched search failures count conservatively, while a failed fetch (no page content returned) releases its pre-charged count. Local calendar days/months use the runtime machine's timezone. State persists in `runtime/state/usage.json`; check-and-count is synchronized within one process, without a strict cross-process guarantee.

```toml
[search.usage_limits.tavily]
daily = 0
monthly = 800
```

Legacy `period` / `max_calls` settings override defaults and save in the new format. Legacy records preserve their known period; the other period's unknown history is labeled rather than invented, with newly observed requests counted thereafter. Tavily Credits, Exa dollar credits and You.com balances are not interchangeable with request counts: balances may run out, or charges may accrue, before the local limit.

In `syvrio setup`, select search/fetch setup then quota and official-pause management to view defaults, effective limits, tracked usage, remaining counts and pause reasons; edit both caps (0 means unlimited), restore defaults, or clear an official pause without resetting counters. Enabling a provider no longer assigns a universal daily cap of 20.

Only documented official quota/balance/spending-limit errors persist a pause. Ordinary rate limits, timeouts, 5xx and authentication failures do not. Local-limit and official-pause reasons differ; automatic fallback continues, while explicit provider selection never silently substitutes another provider. Cached results remain available. A pause expires automatically only with a reliable recovery timestamp. The checked vendor documents do not establish exact reset instants/timezones for these account errors, so current recognized errors require manual recovery rather than guessed calendar resets.


## Development and verification

```sh
uv sync --frozen
uv run ruff check src tests scripts evals
uv run pytest
```

Automated tests use temporary Vaults and test credentials. Install optional local vector retrieval with `uv sync --extra local-embed`; keyword retrieval is available when the vector model is unavailable. The evaluation guide describes the retrieval corpus, metrics and limitations. [Evaluation guide](evals/README.md) · [Architecture](docs/architecture.md)

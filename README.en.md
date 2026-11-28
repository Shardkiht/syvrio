<div align="center">

<img src="web/public/syvrio.svg" alt="Syvrio logo" width="112" />

# Syvrio

**A local-first memory agent that remembers you.**

Turn everyday conversations into long-term local memories you can inspect, trace, and roll back. Use the terminal chat, the private web client, or connect an MCP client.

[![test](https://github.com/Shardkiht/syvrio/actions/workflows/test.yml/badge.svg)](https://github.com/Shardkiht/syvrio/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/syvrio)](https://pypi.org/project/syvrio/)
![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

[中文](README.md) | **English**

</div>

Stop repeating your preferences and plans every time you start a chat. Tell it “I prefer unsweetened hot drinks,” open another conversation, and it can look that preference up. Inspect what it remembers in the topic browser. Correct or retire a fact through the client, or read the exported history in Git.

![Syvrio Web remembers a preference for unsweetened hot drinks and recommends roasted barley tea in a new chat](docs/assets/syvrio-demo.gif)

Recorded in the Web client with a synthetic Vault and local model replay. The second chat asks only “What should I order to drink?” Syvrio calls `recall` to find the preference saved in the first chat. [View the recall result](docs/assets/syvrio-demo.png).

## Quick start

These instructions describe the current source checkout. With Python 3.12+, uv and Git installed, run the following from the repository directory.

```sh
uv sync --frozen
uv run syvrio setup
uv run syvrio chat
```

Each deployment uses one Vault. Git provides a read-only export. To recover the complete dataset, stop the backend and use `syvrio restore` with a new destination. Setup configures the model. To choose a custom Vault directory, run `syvrio init --vault <directory>` on first deployment. Save provider API keys to the credential store through Settings or `syvrio setup`. Sending the first message creates a session. The terminal, web interface, CLI and MCP client connect to the same resident backend and share sessions, memories, job progress and configuration. Any client can start the backend on first connection. The terminal connects directly. The browser loads its pages from the Web gateway and uses the same API through it. When the browser and terminal open the same conversation, their messages run in the order received. Different conversations can generate replies concurrently. You can open another conversation or start a new one during a reply. The original conversation continues processing its queue and shows the latest reply when you return.

To run the web interface from source, install Node.js and Corepack, build the assets once, then start the server.

```sh
uv run python scripts/build_web.py
uv run syvrio web
```

The default URL is `http://127.0.0.1:8765/`. Use `syvrio backend status` to inspect the backend for the current Vault, and `syvrio backend start` or `syvrio backend stop` to manage it. The web guide covers deployment, LAN access and authentication. [Web client guide](docs/web.md)

Syvrio maintains a single release line on `main`. Install and update the `syvrio` package from PyPI.

With a PyPI installation and a configured ngrok v3 agent, run `syvrio web share --domain demo.ngrok-free.dev --github-id 12345`. Confirm to save the hostname and GitHub numeric user ID, then use `syvrio web share` on subsequent starts. The tunnel protects the loopback Web gateway with GitHub login. Ordinary `syvrio web` starts locally. [Deployment and updates](docs/web.md#ngrok)

The development frontend runs at `http://127.0.0.1:7987/`. Start its gateway with `syvrio web --port 8746`. You can also set the gateway port with `SYVRIO_WEB_PORT`.

Enter `/update` in the Web app or TUI to check for stable releases on PyPI. In the Web app, you can also choose **Settings → About → Check for updates**. If a systemd user service manages the installation, choose Install and confirm the target version to start the upgrade. Connected clients briefly disconnect during the restart. See the [update guide](docs/web.md#pypi-自动更新).


`syvrio update` shows progress as it downloads, verifies, installs dependencies and restarts the service. Downloads show a percentage when the size is known. The interactive terminal shows elapsed time while it waits. Updates started from the Web app or TUI also report progress in the update panel.

## Configuration

The default entry is `~/.syvrio/config.toml`. Select another entry with `--config` or `SYVRIO_CONFIG`. Local files override individual fields from the [packaged defaults](src/syvrio/infra/config/defaults/).

| File beside the entry | Settings |
|---|---|
| `config.toml` | Vault, Runtime, Git, web search and fetch |
| `models.toml` | Model provider, endpoint and request options. API keys come from the credential store |
| `chat.toml` | Context window, compaction, output reserve and tool budgets |
| `memory.toml` | Retrieval, embeddings, automatic recall, distillation and Dream |

Save model API keys in Settings → Providers or `syvrio setup`. Manage search and fetch keys in `syvrio setup`. After upgrading, use the corresponding settings to save any keys previously supplied only through environment variables. **Remove API key** deletes the saved key. You can add it again on the same provider card. If the active model uses that key, switch to a model that uses a different key first.

The `api_key_env` model setting names an entry in the credential store. Keys are saved in the system keyring by default. On Linux or WSL without a keyring backend, Syvrio can encrypt keys with `systemd-creds --user`. Encrypted credentials are stored under `syvrio/credentials/` in the user data directory. Set `SYVRIO_CREDENTIALS_DIR` to use a dedicated absolute path. Add your keys again when moving to another machine or OS account.

Each entry has its own file, such as `EXA_API_KEY.cred`. Its name is readable and its encrypted value is bound to that name. To upgrade an installation with hashed filenames, stop credential writes, back up the directory and run `python -m syvrio.infra.security.migrate_credential_names EXA_API_KEY` under the original account using the upgraded Python environment. Pass one or more entry names. The tool verifies each new file before removing its predecessor and can be run again. System keyring entries need no migration.

For example, set `default_limit = 15` under `[retrieval]` in `memory.toml` to request up to 15 results by default. Chat prefetch uses `[recall].auto_recall` independently.

Dream has its own `[dream].context_window` in `memory.toml`, defaulting to 200,000 tokens. It generates the profile first, then proposes inferences. Each stage sends all its evidence in one request when its input and output allowance fit. Larger inputs resume in batches. A smaller advertised model window takes precedence.

A model change takes effect on the next Dream request. After a failure, switch models and run `/dream run` to resume from the last saved position using the same evidence version. Facts and inferences that change during the run enter the next run.

For a nondefault directory, set `SYVRIO_CONFIG` to the entry or place the group files under `~/.syvrio/` with `config.toml` as the entry. Removing an override or an optional group file restores the corresponding defaults on the next load. A missing explicitly selected entry stops startup. Repair the installation if a packaged defaults file is missing.

## Chat and attachments

Open **Commands** in the top-right corner to find and run a command. Open **Settings → Shortcuts** for key bindings and alternatives to browser-reserved keys.

Select images with `+` or `/import` to preview them in the web composer. In chats and archived conversations, select a thumbnail to see the full image or its filename to download the original. Markdown images use the same thumbnail size. Choose **Actual size** to inspect details in a long diagram or screenshot.

Each outgoing message keeps its delivery ID across retries. The web app retains the text and attachments when a connection drops and retries on reconnect. You can also choose Retry delivery. In the terminal, Ctrl+R retries a delivery while preserving the next draft. Replies already in progress resume from the same turn.

- **Sending and undoing.** In the browser, Send becomes Stop in the same position while a reply is in progress. Press Enter during a reply to queue a message for the end of the turn. Ctrl+Enter requests insertion at a safe point in the current turn. Tool calls must finish before insertion. `/undo` interrupts the latest turn, reverts eligible memory and conversation changes from that turn and puts the sent text back in the composer. In the browser, hover over the latest user message to undo or edit and resend it. The edit replaces the original turn when it is saved. If saving fails, the original messages, attachments and memory changes remain available. You can revise or retry the draft after refreshing. Later edits or references from another session can block the entire undo.
- **Documents and images.** Click the circular **+** inside the web composer or press **Ctrl+O** in the TUI to attach local files. Each message supports up to eight files, 20 MiB per file and 40 MiB in total. Models can read documents. Vision-capable models can also analyze PNG, JPEG, WebP and GIF images. The `/import` chat command opens the same local file picker as **+**. Selected files stay in the draft until you press Send or Enter. The model can then read or summarize the files, or save facts when you ask it to.
- **Sessions and models.** Browse and manage conversations with `/sessions`. After the first complete exchange, the model suggests a short title in the language of the conversation. Edit the title in the Web header or use `/rename`. Titles can contain up to 80 characters. Longer titles prompt you to revise them. While a reply is streaming, use `/model` to select a model, `/think` to choose the reasoning level, and `/context` to set the context window. Both the TUI and Web can select from the full provider model catalog. Web filters the loaded list as you type. Refresh reloads the catalog, and changing a provider key or endpoint reloads it the next time you open the list. File memory extraction and replies after tool calls use the model selected when the turn began. Queued messages use the latest settings when their turn starts. Expand reasoning and tool details in the conversation. Type `/` to see the available commands.
- **Context compaction.** New messages, follow-ups after tool calls, and `/compact` use the same threshold. File reads return the full text by default and accept an optional offset and length. Tool results are recorded in full and sent to the model intact below the threshold. The estimate counts only the reasoning history the active model can replay. When estimated input reaches 80% of the available input budget (the default), completed conversation history and tool results since the previous summary are folded into the summary. The input budget reserves room for the model's output. The original messages remain in SQLite as session events. Background distillation processes each original user message, checks evidence in batches when it exceeds the model window, and commits its progress with the facts it saved. Switching sessions, exiting or restarting the backend can resume unfinished work.

## Memory, review and profiles

Formal memories are SQLite records organized by topic. Each has a stable ID, text, topic and status. Optional context covers event date, validity dates, location and feelings, including separate wording for the event and for later recollection. Dates retain the stated precision of year, month or day. A chat-derived entry links to its source session and user message. By default, `recall` returns up to 10 formal facts. Use `limit` to choose a different count. Results can be filtered by date range, location and emotion.

| Action | Result |
| --- | --- |
| Record and manage a fact | `memorize` saves a fact. `correct` revises the same fact and keeps its earlier versions. `transition` records a change over time. `deprecate` retires a fact while retaining its history. |
| `/pending` | Open Pending in the upper right of the web app. Search by topic, text or ID. `/pending edit <id>` opens the full text. Saving the original wording requires choosing Save unchanged. Edited facts can receive context from their trusted source in the background. Open Maintenance suggestions to check for duplicate or conflicting facts, compare their sources and confirm or ignore a proposed resolution. |
| `/inferences` | Scroll the full list, filter by status, and search by text, category or ID. Confirmed inferences are stored separately from formal facts. |
| `/profile` | Read the most recently published full profile. |
| `/topics` | Browse and search active memory text across topics or within one topic. Results are paginated. Add memories or edit their text and confirm before saving. In inference details, **Compare sources** shows the cited and current version of each source. |
| `/dream` | Alias for `/dream run`. Confirm to let the backend schedule profile updates and generate hypotheses for review. Use `/dream status` to see progress, retries or failures. |

Each chat memory proposal has its own ID. After reviewing the retrieved evidence, the model can decide several proposals together, save facts, queue questions for review, or skip them. Retrying returns the existing status or receipt.

A model proposes a pending item when a question still needs the user's decision after reviewing recalled facts and active candidates. Invalid input such as credentials is blocked at write time. Deprecation preserves memory IDs. Undo can roll back newly created facts from the latest turn after checking for intervening changes and references.

## CLI and MCP

These commands use the resident backend for the current Vault. Run `syvrio --help` for the full command reference.

```sh
syvrio memorize "I prefer unsweetened hot drinks" --topic preferences
syvrio recall "drink preferences"
syvrio recall "museum visits" --from 2024 --to 2025 --location Hangzhou
syvrio pending
syvrio maintenance run
syvrio maintenance status
syvrio profile
syvrio dream status
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

Each deployment stores its conversations, facts, candidates, inferences, published profile, jobs and undo records in `Vault/syvrio.sqlite3`. The same database stores search provider pauses and local Git sync state. Vector embeddings and web search results are cached separately in `Runtime/cache/vectors.sqlite3` and `Runtime/cache/web-search.sqlite3`. Uploaded originals are addressed by SHA-256 under `Vault/attachments/objects/`. Archived conversations remain available in Archive. A fact that cites a conversation prevents permanent deletion.

Git operates in `Vault/export/` on Markdown and JSONL rendered from a single committed database snapshot. Each change to a fact is recorded explicitly in its audit entry. Git uses that field to export the effective changes. After a successful `/undo`, the audit retains the original entry and its reversal, while the effective change list excludes the reverted operation. Rolled-back transactions never appear in the export. For a complete backup run `syvrio backup DEST --config CONFIG`. Restore into a new directory with `syvrio restore DEST NEW_VAULT`. Chat search and distillation indexes are created together with a new database. For a fresh installation, use an empty Vault and a dedicated Runtime. [Storage and recovery](docs/storage.md)

In the Web sidebar, use the magnifying glass beside the logo to search, then click a title or message result to open its conversation. Selecting a result keeps the query. Click outside the search or press Esc to close it and return to the conversation list.

## Configuration and Git

Use `syvrio setup` to configure the model and API base URL. The wizard and `/think` share the active model’s reasoning choices, using provider-declared levels when available and protocol presets otherwise. `SYVRIO_CONFIG` selects the entry configuration file and `SYVRIO_RUNTIME` selects its paired Runtime. When the configuration omits `vault`, `SYVRIO_VAULT` supplies the deployment default. Retrieval supports keywords, local embeddings and embedding APIs. An optional `SOUL.md` holds a user-written assistant persona. For a Vault with a Git remote, `/git` shows status and synchronization actions. Complete backups include referenced originals. Model-generated commit summaries include the file statistics. If a diff exceeds the model window, the summary uses excerpts from individual files so smaller changes later in the diff remain visible.

For Qianwen AI Platform pay-as-you-go, open **Settings → Providers → 千问 AI 平台** and select **Add key**. Open **Model**, refresh the catalog, then search by name and select a model. Terminal users can save the key with `syvrio setup`. Syvrio stores Qianwen and Bailian credentials separately. [Qianwen API key guide](https://platform.qianwenai.com/docs/api-reference/preparation/api-key)

## Web search

The `search` tool queries public web pages through a local [open-websearch](https://github.com/Aas-ee/open-webSearch) daemon by default and needs no API key. Start the daemon in a separate terminal:

```bash
npx -y open-websearch@latest serve
# or: npm i -g open-websearch && open-websearch serve
```

Search results contain only titles, snippets, and links; `fetch_web` opens one of those links and extracts the page text to confirm dates, details, or snippets. open-websearch fetching is available over the HTTP transport only; the MCP interface still exposes just `search`. The fetch entry point accepts only public http(s) URLs, rejects localhost, private, and link-local addresses, and re-validates the final URL after redirects; fake-IP proxy DNS answers in `198.18.0.0/15` and `2001:2::/48` are allowed by default for hostnames only; literal non-public IP URLs remain blocked. `FAKE_IP_CIDRS` overrides these defaults (an empty value disables compatibility). Automatically started daemons preserve the configured CIDR order. open-websearch 2.2 stops matching when it encounters a different address family. With no override, Syvrio passes only the IPv4 range on hosts without a route to the synthetic IPv6 range, and prefers IPv6 when that route exists. For a dual-stack proxy that returns only synthetic IPv4 addresses, set its IPv4 range explicitly. Restart an external daemon after changing its settings. Automatic startup prefers an installed `open-websearch` on PATH, falling back to `npx open-websearch@latest` when it is absent.

### AnySearch and Parallel MCP

AnySearch and Parallel MCP both support Search and Fetch. Enable them independently in `[search].providers` or `[fetch].providers`. Both operations use the same optional API key:

- `anysearch`: calls AnySearch REST search (`https://api.anysearch.com/v1/search`). It sends no `Authorization` header without a key; saving `ANYSEARCH_API_KEY` in the credential store switches to Bearer authentication. `zone` and `language` come from `anysearch_zone` / `anysearch_language` under `[search]`, defaulting to `cn` / `zh-CN`. The original query is never rewritten.
- `parallel_mcp`: Search keeps `web_search` on the standalone Search MCP (`https://search.parallel.ai/mcp`), which Parallel still supports for compatibility. Saving `PARALLEL_API_KEY` in the credential store adds Bearer authentication to both Search and Fetch. Search prefers `structuredContent.results` and also accepts JSON in `content[].text`; duplicate URLs are merged, aggregated results and the session receive all excerpts supplied by the service, and unverifiable publish dates remain empty.

Example (the default order is unchanged; a provider is enabled only when explicitly listed):

```toml
[search]
providers = ["parallel_mcp", "anysearch", "open_websearch"]

[fetch]
providers = ["anysearch", "parallel_mcp", "open_websearch"]
```

AnySearch and Parallel MCP searches use the same quality checks, fallback rules, per-search request budget and cache as other providers. On an authentication failure (401/403), the router reports the error and follows the configured fallback order.

- **AnySearch Fetch** posts the URL to the [Extract API](https://www.anysearch.com/docs/api-endpoints/v1-extract) at `https://api.anysearch.com/v1/extract`, extracts `data.content`, and strips HTML.
- **Parallel Fetch** calls `web_fetch` through [Parallel MCP 2.0](https://docs.parallel.ai/integrations/mcp/quickstart) at `https://mcp.parallel.ai/mcp`. It requests Markdown for one URL and the fetch router tries other providers if it receives only excerpts.

The adapters use the redirect destination when the service reports it, otherwise they keep the requested URL without probing for redirects. Fetch validates the input and any reported destination. By default the session receives all text returned by the provider. Specify `max_chars` for a shorter preview. If every provider returns partial content, the result sets `truncated=true` and identifies either page text (`content_kind=page`) or excerpts (`content_kind=excerpts`). The open-websearch 2.2 HTTP API returns up to 200,000 characters per request, and Syvrio passes its truncation flag to the model. OpenWebSearch (HTTP), Tavily, Exa, You.com, and Volcengine continue to support Fetch; Baidu Qianfan and SearXNG are search-only.

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

The adapter only calls the configured instance address and never treats URLs inside a query as service addresses. It does not follow HTTP redirects, caps the response body at 2 MiB, and bounds each request by both `searxng_timeout` and the global `timeout_budget`. `SearchQuery.lang` maps to `language`, while `freshness` maps to `time_range` only for values SearXNG supports (day/week/month/year); values that cannot be mapped equivalently are omitted. Multiple internal engines still share Syvrio's unified URL deduplication, request budget and result-count cap, and partial engine failures are retained in `failed`. SearXNG provides search only, not `web_fetch`; page fetching still follows the `[fetch]` settings.

Bing sometimes splits Chinese proper nouns into single characters (for example, 「智谱」 is searched as 「智」). When results are obviously off-topic, set `engines = ["baidu"]` in the config, or set `search_engine = "baidu"` only when explicitly choosing the Baidu upstream in OpenWebSearch. To choose Baidu Qianfan or Volcengine as providers, pass `providers` alone and leave `search_engine` unset or `auto`; explicit engine selection filters out providers that do not support it. `search_engine` also accepts an array such as `["baidu", "bing", "sogou"]`, merged within one request, with no engine-count limit by default; set `search.max_search_engines` to cap an explicit list (0 means unlimited); the MCP `search` tool supports the same.

Automatic fallback: each request pulls more candidates from the upstream engine than will be displayed (3x the display count, 10-30). A single provider keeps its adapter's order; multiple providers are interleaved by priority as A1, B1, A2, B2. The router only normalizes fields and URLs, deduplicates stably, and truncates: a duplicate URL keeps its first position in the interleaved sequence, deduplication happens before truncation, and no title, snippet, core-term or date scoring, content-similarity deduplication, or extra requests for ranking are used. Failures, empty responses, or too few unique results trigger further requests through `fallback_engines` and provider priority; reaching the display count or exhausting the budget stops the search, after at most `max_engines` engines. All fallbacks share `timeout_budget`, and each request's timeout is capped by the remaining budget. Total-budget truncation returns the deduplicated results collected so far and skips caching. All providers also share `max_requests`; exhausting it returns current results with a truncation hint and does not cache them. For short person names, handles, or product codenames the `search` tool accepts `context` (proper nouns confirmed in the conversation) and can derive up to two query variants within the same request budget; an explicit `search_engine` keeps single-request semantics and never switches engines on its own.

The LLM may pass `providers` to the `search` call to recommend an ordered list of enabled sources, plus `strategy` to choose how the router schedules them (the same parameters are supported by Agent Function Calling and MCP):

Choose providers by the target source, not the question language. Omit `providers` for ordinary queries or when unsure so the user's configured order remains in effect. Tool guidance mentions only enabled providers: prefer Volcengine for Chinese trending topics and Baidu Qianfan for Chinese official sites and policy originals; AnySearch can supplement Chinese research. Prefer Exa for English technical material, use Tavily as a backup, and use Parallel MCP to supplement complex research.

- `auto` (default): request providers in the recommended order and stop once deduplication yields enough results. Continue on failure, empty results, or too few unique results until the budget is exhausted or no sources remain. After the list is exhausted, remaining configured providers may still be used as fallback.
- `compare`: for fresh news or important facts that benefit from cross-checking. When budget allows, it attempts to obtain non-empty responses from at least two providers and does not stop merely because the first one has enough results. Results are interleaved by provider priority and deduplicated; the same page returned by multiple providers keeps only its first position and is not independent evidence. If quota, timeout, or availability prevents two providers, `hint` explicitly says cross-verification was incomplete and lists failure kinds instead of claiming verification. The cache also stores verification state, the actual attempt order, contributing providers and failure diagnostics, so a cache hit restores the same hint; entries without verification metadata trigger a fresh search. Results truncated by the total timeout or request budget are never cached, so the next search issues fresh requests.
- `strict`: call only the sources explicitly listed in `providers`; never extend to other configured providers. An empty `providers` list is rejected, and if all listed sources are unavailable the failure is reported truthfully.

An omitted or empty `providers` uses the configured order. Duplicates are removed, at most 8 entries are accepted, and only actually enabled and registered names are used; invalid names are safely ignored and reported in `failed`. The router produces final results only from provider priority, adapter order, and unique-result counts; it does not judge content relevance. The LLM cannot specify API keys, auth headers, or service addresses, nor bypass enablement, provider pauses, `max_requests`, or `timeout_budget`. Results include `attempted` (the actual attempt order) and `strategy`; each result still carries its own `provider` and upstream `engine`. Cache keys distinguish the `providers` order, `strategy`, and effective attempt sequence; a cache-key version change invalidates results produced by older aggregation rules. `search_engine` still means an upstream engine only and remains a separate layer from providers.

An explicit engine selection only uses providers that can honor it. Each result keeps the upstream engine reported by its provider. When the provider omits this field, a single selected engine can supply its name. The source stays empty for a multi-engine request.

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
- `[search]` settings: `enabled` (removes the tool when false), `providers` (fallback order), `base_url`, `timeout`, `max_results`, `cache_ttl`, `transport` (`http` by default; `mcp` starts an npx MCP process per search), `auto_start` (the resident backend starts the HTTP daemon in the background when enabled search or fetching uses open-websearch over HTTP), `engines` (upstream search engines, `["bing"]` by default), `fallback_engines` (backup engine chain, `["baidu", "duckduckgo", "sogou"]` by default), `max_engines` (engines tried per automatic fallback, 3 by default), `max_search_engines` (cap for an explicit `search_engine` list, 0 means unlimited, 0 by default), `timeout_budget` (shared fallback budget, 25 seconds by default, with each single request's timeout capped by the remaining budget), `max_requests` (upstream requests shared across all providers per search, 9 by default; when exceeded the current deduplicated results are returned with an explicit truncation hint and are not cached), `anysearch_zone` / `anysearch_language` (AnySearch region and language, `cn` / `zh-CN` by default), `searxng_base_url` / `searxng_timeout` (self-hosted SearXNG instance address and per-request timeout, `http://127.0.0.1:8888` / 10 seconds by default), and `fetch_timeout` (separate timeout for fetching page text, 45 seconds by default).

Controlled real-network smoke test: `scripts/smoke_search_apis.py` by default only performs local checks (key readability, whether each vendor would register; anonymous providers are marked registrable without a key) and never sends a request; adding `--provider <vendor> --operation search|fetch --live` sends at most `--max-requests` (default 1, cap 10) real requests to a single vendor and prints a desensitized report (success, result count or content length, error kind, duration and unverified fields). The script reads keys from the credential store and reports only the check results. Page fetching is controlled by a separate `[fetch]` section: `providers` is the fetch provider order (`["open_websearch"]` by default), independent of the search provider order; official vendors with a configured key (tavily / exa / you / volcengine) join fetching only once listed here.
- A daemon started by `auto_start` has no visible console window on Windows and stays resident for reuse after the TUI exits.
- Web search results are cached in `Runtime/cache/web-search.sqlite3`. Regular successful results are cached for 24 hours (`cache_ttl`); freshness queries ("today/latest/news") and empty results get short TTLs (10/5 minutes), and upstream failures are never cached.


Fetching independently follows `[fetch].providers`. Official fetch adapters and open-websearch use `search.fetch_timeout`; search uses `search.timeout`. Only open-websearch fetching requires HTTP transport; configured official fetch adapters are independent of that transport.

### Search provider pauses and recovery

When a provider reports an exhausted quota, insufficient balance or a spending limit, Syvrio pauses it if the adapter recognizes the error. Search and Fetch share the pause across restarts and follow their own fallback policies. Chat tools and MCP search return the affected provider names in `quota_exhausted`. The model is asked to mention these providers briefly even when another provider succeeds. Cached search results remain available. Ordinary rate limits, timeouts, server errors and authentication failures apply to the current request.

Search and Fetch can retry once a provider-supplied recovery time has passed. After adding credits or changing plans, you can also open the search and fetch settings in `syvrio setup` to see the pause reason and resume the provider manually. The provider account controls free allowances, overage charges and spending limits.

## Development and verification

```sh
uv sync --frozen
uv run ruff check src tests scripts evals
uv run pytest
```

Clients request application data from the backend. Configuration and defaults, model access, web search, retrieval, session storage, and Vault services have separate packages under `src/syvrio/infra/`. Python tests are grouped by responsibility under `tests/`. Web features and their component tests live together under `web/src/features/`.

Automated tests use temporary Vaults and test credentials. Install optional local vector retrieval with `uv sync --extra local-embed`; keyword retrieval is available when the vector model is unavailable. The evaluation guide describes the retrieval corpus, metrics and limitations. [Evaluation guide](evals/README.md) · [Architecture](docs/architecture.md)

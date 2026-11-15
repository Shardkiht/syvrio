# Memory Schema v2 and Vault Layout v2

Each top-level unordered list item in a memory file is one record. Start with free Markdown, then place a standalone `**Core**` paragraph inside the same item. Optional `**Context**` and `**Extensions**` paragraphs follow in that order. Indent their field lists so they belong to the entry. Code fences and ordinary nested lists in the body are content.

```markdown
# Exhibitions

* I visited a **paper sculpture** exhibit.

  **Core**
  * `id`: `mem-example`
  * `status`: `active`
  * `source`: `note`
  * `confidence`: `0.95`

  **Context**
  * `temporal.event_time`: `2024-06`
  * `spatial.location`: `Example Harbor`
  * `affective.emotion`: `delighted`

  **Extensions**
  * `custom.marker`: `cobalt-fox`
  * `custom.marker`: `second-marker`
  * `entities.person`:
    * `name`: `Sample Agent`
```

Core has exactly four fields. The ID must be unique across the Vault and should never change. Status is `active`, `superseded` or `deleted`. Source is a nonempty string and confidence is a finite number between 0 and 1. Fields use snake_case and may have dot-separated namespaces. Optional sections allow ordered, repeated fields and nested groups. Use inline code spans for scalar values. A longer backtick delimiter can contain a shorter one; literal line breaks are written with the reserved `~syvrio-escaped~` prefix and percent encoding. There is no frontmatter or embedded JSON metadata.

`syvrio.recorded_at`, `syvrio.supersedes`, `syvrio.session`, `syvrio.message` and `syvrio.change_kind` preserve write time, links and provenance. They are not substitutes for event time. All recorded times are UTC. The retriever indexes record body text for recall. The selected records are passed whole as untrusted Markdown references, with their Vault-relative paths and line numbers. Oversized records are omitted rather than clipped. Status edits replace the value at its source location; unrelated record bytes and LF/CRLF are retained.

```text
~/.config/syvrio/syvrio.toml
~/.syvrio/
  vault/
    README.md
    SOUL.md
    memories/<category>/<topic>.md
    sessions/YYYY/MM/<id>.jsonl
    sessions/legacy/undated/<id>.jsonl
    records/memory/{pending,resolved,audit,inferences}.jsonl
  runtime/
    indexes/vectors.npz
    cache/search/
    session-reasoning/<session-id>/<record-id>.json
    state/{usage,vault-binding}.json
    locks/{vault,git,client}.lock
    logs/
```

Categories can be nested. Use the Vault-relative Markdown path when topic names repeat. Git tracks only the Vault and sees memory, session and record changes. Stop running Syvrio clients before switching the active Vault. A Runtime bound to a different Vault invalidates its known derived files; Vault data stays intact. Deleting Runtime while Syvrio is stopped does not delete facts or sessions, but does remove local counters and derived caches.

Assistant records carry a `reasoning_local: true` marker in the session JSONL; the reasoning text itself (thinking text, fragments, seconds) is stored only in the Runtime at `runtime/session-reasoning/<session-id>/<record-id>.json` and never enters the Vault, Git history or commit-summary input. The model context is never replayed reasoning — including old inline `reasoning` objects that may remain in sessions written by earlier versions. Missing or corrupt attachments do not block loading: the message body still loads, only the thinking display is absent. Deleting a session deletes its attachments. Because the reasoning text is Runtime-local, losing the Runtime only loses the ability to display past thinking; sessions and facts remain complete. Note that sessions written by older versions may still contain inline reasoning text in their JSONL and in already-committed Git history; this fix does not rewrite historical data.

Configuration lookup: `SYVRIO_CONFIG`, local `syvrio.toml`, then `~/.config/syvrio/syvrio.toml`. `vault` and `runtime` are independent top-level settings; `SYVRIO_RUNTIME` overrides the runtime path. Runtime must be outside any Git working tree and separate from the Vault. See [config.example.toml](../config.example.toml).

## Upgrade an existing Vault

1. Stop Syvrio writers. Archive the whole Vault, including hidden files and `.git`, and any existing Runtime. Verify SHA-256 and restore the archive into a test directory.
2. Run the commands below on the restored copy, using an empty destination pair. The destinations must share a parent. Use `--source-runtime` when the old logs or state also live outside the Vault.
3. Inspect counts and the private report in the new Runtime. Compare IDs, session bytes and record logs, and validate the test copy. Re-running `--apply` is safe on an identical, untouched pair. Verification detects edits, conflicts or changes to the source; use a fresh destination for a new migration. A run interrupted after publishing Runtime can resume using its verified report.
4. If the migrated Vault still stores local-time timestamps (pre-UTC data), run the timestamp migration on the migrated copy, then confirm the new program reads the migrated data. Note: the current program requires explicit UTC offsets everywhere — a pre-UTC Vault cannot be opened even for reading until this migration has run, so the full read verification only makes sense after it. `--dry-run` writes nothing; `--apply` requires `--backup-dir` pointing at a directory that does not exist yet and lies outside both the Vault and the Runtime:

```sh
python scripts/migrate_timestamps_to_utc.py --vault /path/to/vault-v2 --source-timezone Asia/Shanghai --dry-run
python scripts/migrate_timestamps_to_utc.py --vault /path/to/vault-v2 --source-timezone Asia/Shanghai --backup-dir /path/to/new-utc-backup --apply
```

5. Only after the migrated data has been verified: update the local config to the verified pair. Keep the prior configuration and archive for rollback. The v1 reader cannot open v2 records; a rollback must restore the old program and its old Vault together.

```sh
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --dry-run
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --apply
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --verify
```

The default Runtime destination is `vault-v2-runtime`; select a different sibling with `--runtime-destination`. The script does not write the input, change configuration, or contact a remote. It preserves original memory files in the Runtime's private legacy archive and copies session and record JSONL byte for byte. Legacy files with no explicit ID use the previous reader's deterministic ID; duplicate IDs abort migration. Unknown source becomes `migration:unknown`; missing/invalid confidence receives the conservative storage value `0` with `migration.review_required`. These are review markers, not inferred facts. Unknown keys and values that cannot be represented directly are retained as encoded extension values and in the original archived files.

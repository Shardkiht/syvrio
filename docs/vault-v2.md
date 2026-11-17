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

Core has exactly four fields. The file must be UTF-8. A decoding error names the affected Vault-relative file so it can be saved again as UTF-8. The ID must be unique across the Vault and should never change. Status is `active`, `superseded` or `deleted`. Source is a nonempty string and confidence is a finite number between 0 and 1. Fields use snake_case and may have dot-separated namespaces. Optional sections allow ordered, repeated fields and nested groups. Use inline code spans for scalar values. A longer backtick delimiter can contain a shorter one; literal line breaks are written with the reserved `~syvrio-escaped~` prefix and percent encoding. There is no frontmatter or embedded JSON metadata.

`syvrio.recorded_at`, `syvrio.supersedes`, `syvrio.session`, `syvrio.message` and `syvrio.change_kind` preserve write time, links and provenance. They are not substitutes for event time. The program uses temporal context for current/history views and indexes body, context and extensions for recall. The selected records are passed whole as untrusted Markdown references, with their Vault-relative paths and line numbers. Oversized records are omitted rather than clipped. Status edits replace the value at its source location; unrelated record bytes and LF/CRLF are retained.

```text
~/.syvrio/
  config.toml
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
    cache/profile/profile-summary.json
    state/{reflection,fingerprints,git-sync,usage,vault-binding}.json
    jobs/dreaming/jobs.jsonl
    session-reasoning/<session-id>/<message-id>.json
    turn-undo/<session-id>/<first-user-message-id>.json
    locks/{client,vault,git}.lock
    logs/
```

Categories can be nested. Use the Vault-relative Markdown path when topic names repeat. Git tracks only the Vault and sees memory, session and record changes. Stop running Syvrio clients before switching the active Vault. A Runtime bound to a different Vault invalidates its known derived files; Vault data stays intact. Deleting Runtime while Syvrio is stopped preserves facts and session text but removes local reasoning attachments, counters, and job progress. Back up Runtime separately if you need those records.

Assistant records may contain a `reasoning_local` flag that points to a private Runtime attachment. The attachment contains reasoning and optional protocol fragments. Keep the Runtime if you need to reopen this reasoning after a restore. Older sessions need no JSONL rewrite: assistant records without the flag still render their replies. If the attachment or its ID is missing, the reply still loads without reasoning. A legacy inline reasoning field is not used for display or model replay.

Supplemental user messages may carry a `turn_root` reference to the first user message. Tool records may carry `turn_undo_recorded` to indicate that rollback depends on the matching private Runtime journal. These optional fields require no migration. The journal records the original state and expected changes before local tool writes. Undo preflights the affected files and inferences, refuses later conflicts, and appends session and audit events. Older turns without a journal marker still support conversation-only undo. Deleting a session also removes its local journal.

Configuration lookup: `SYVRIO_CONFIG`, local `syvrio.toml`, then `~/.syvrio/config.toml`. If the last file does not exist, the previous platform configuration path is still accepted. `vault` and `runtime` are independent top-level settings; `SYVRIO_RUNTIME` overrides the runtime path. Runtime must be outside any Git working tree and separate from the Vault. See [config.example.toml](../config.example.toml).

## Upgrade an existing Vault

1. Stop Syvrio writers. Archive the whole Vault, including hidden files and `.git`, and any existing Runtime. Verify SHA-256 and restore the archive into a test directory.
2. Run the commands below on the restored copy, using an empty destination pair. The destinations must share a parent. Use `--source-runtime` when the old logs or state also live outside the Vault.
3. Inspect counts and the private report in the new Runtime. Compare IDs, session bytes and record logs, and validate the test copy before switching configuration. Re-running `--apply` is safe on an identical, untouched pair. Verification detects edits, conflicts or changes to the source; use a fresh destination for a new migration. A run interrupted after publishing Runtime can resume using its verified report.
4. Update the local config to the verified pair. Keep the prior configuration and archive for rollback. The v1 reader cannot open v2 records; a rollback must restore the old program and its old Vault together.

```sh
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --dry-run
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --apply
python scripts/migrate_vault_v2.py --source /path/to/restore --destination /path/to/vault-v2 --verify
```

The default Runtime destination is `vault-v2-runtime`; select a different sibling with `--runtime-destination`. The script does not write the input, change configuration, or contact a remote. It preserves original memory files in the Runtime's private legacy archive and copies session and record JSONL byte for byte. Legacy files with no explicit ID use the previous reader's deterministic ID; duplicate IDs abort migration. Unknown source becomes `migration:unknown`; missing/invalid confidence receives the conservative storage value `0` with `migration.review_required`. These are review markers, not inferred facts. Unknown keys and values that cannot be represented directly are retained as encoded extension values and in the original archived files.

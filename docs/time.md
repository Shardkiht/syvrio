# Time and timestamp migration

Use `syvrio.time.utc_now()` or `utc_timestamp()` when creating an absolute
timestamp. Persist it with `serialize_timestamp()` as ISO 8601 with `+00:00`.
Use `parse_timestamp()` for input and comparisons. It requires an explicit
offset, accepts `Z`, and returns a UTC datetime. Missing optional fields retain
their meaning. An invalid or offset-free value in an absolute timestamp field
requires explicit data correction.

Display dates and times in the viewer's local timezone, using `YYYY-MM-DD HH:mm`
and adding seconds only where relevant. The TUI and CLI use
`format_local_timestamp()`. The Web receives UTC timestamps and uses
`web/src/time.ts` in the browser. Both use the 00–23 hour cycle. TUI conversation
labels may display just `HH:mm`. Local display has no effect on ordering.

Calendar dates (including day-precision imported records), date ranges, Git's
daily wall-clock schedule, search quota day boundaries, Unix seconds, elapsed
time and timeouts retain their existing types and meanings. For an explicitly
offset-bearing input to a relative-date expression, use the source calendar.
For a message stored in UTC, the active client's local calendar supplies the
date anchor for new relative-date interpretation.

## Field inventory

| Storage | Absolute timestamp fields | Previous representation |
| --- | --- | --- |
| `sessions/**/*.jsonl` | `started_at`, `at`; list `updated_at` and `last_message_at` derived from these | Naive local creation, local offset for messages and control records |
| `memories/**/*.md` | Extensions `syvrio.recorded_at` | Naive local, offset-aware or day precision in imported records |
| `records/memory/pending.jsonl`, `resolved.jsonl` | `created_at`, `resolved_at` where applicable | Naive local |
| `records/memory/audit.jsonl` | `at` | Naive local |
| `records/memory/inferences.jsonl` | `created_at`, `updated_at` on snapshots, including `revision.records[]` | Local offset |
| Runtime `state/reflection.json` | `last_checked_at`; `profile` and `dream` fields `last_attempt_at`, `last_success_at`, `next_retry_at` | Local offset |
| Runtime `jobs/dreaming/jobs.jsonl`, `cache/profile/profile-summary.json` | `at`, `generated_at` respectively | Local offset |
| Runtime `state/git-sync.json` | `last_commit_at`, `last_push_at`, `last_sync_at`, `retry_after` | Git commit offset or local offset |
| Runtime diagnostic and latency logs | `time` | UTC; existing logs already use UTC |
| Search cache and usage | Cache age in Unix seconds; local day/month quota keys | Unchanged |
| Web API | Session time fields and command payload timestamps | Now sent as UTC, with display localization in the browser |

Memory's event and validity dates stay calendar fields. Updating a memory status
updates the status and produces a UTC audit event where applicable. It does not
change the original recorded time. Add new persisted time fields to this
inventory and the migration field map when applicable.

## Migrate before opening an existing Vault with the strict build

For a v1 Vault, complete and verify the existing Vault v2 migration first. Stop
TUI, Web, MCP, scheduled Git synchronization and other writers of the selected
Vault and Runtime for the whole migration. Use the actual storage roots and
confirm the source timezone before applying. The script never loads the active
configuration or infers a source timezone from the machine.

```bash
.venv/bin/python scripts/migrate_timestamps_to_utc.py \
  --vault /path/to/vault --runtime /path/to/runtime \
  --source-timezone Asia/Shanghai --dry-run

.venv/bin/python scripts/migrate_timestamps_to_utc.py \
  --vault /path/to/vault --runtime /path/to/runtime \
  --source-timezone Asia/Shanghai --apply --backup-dir /path/to/new-backup
```

- `--vault` includes the standard session tree. Use `--sessions` to select a
  separate session tree. Include the active Runtime with `--runtime` to migrate
  its state and acquire its client, Git and Vault locks when applying to both.
- Use `--vault-source-timezone`, `--sessions-source-timezone` or
  `--runtime-source-timezone` for separate scopes. Repeat `--timezone-for`
  with scope-relative prefixes such as `sessions/legacy=America/New_York`.
  The most specific matching path takes precedence.
- The default is a read-only preview. Invalid values, missing source zones
  for naive values, malformed records and ambiguous or nonexistent DST times
  stop the batch before data writes. Resolve them using independent evidence
  of the original timezone or offset, then rerun the preview.
- Apply requires a new backup directory outside the data roots. It backs up
  affected files before replacing them, keeps their relative paths under
  `vault/`, `sessions/` and `runtime/`, and uses atomic file replacement.
  It preserves unknown fields, missing values and calendar dates. Matching
  inference evidence fingerprints are carried forward and stale fingerprints
  remain stale. Runtime profile caches may regenerate.
- To recover after an interrupted or unwanted apply, stop writers and copy
  each backed-up file to the same relative path under its original root.
  The `sessions/` backup maps to the explicit session root or the Vault's
  `sessions/` directory. Preserve the backup until you have verified the
  restored data. Apply is idempotent. A final dry-run should report zero
  changes after a successful migration.

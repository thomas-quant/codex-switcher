# Codex Switch Isolated Add, Parallel Refresh, And Safe Remove Design

Date: 2026-04-13

## Summary

Extend `codex-switch` in three focused ways:

- add `codex-switch add <alias> --isolated` so alias capture can run while a Codex process is active
- make `codex-switch list --refresh` probe unresolved aliases concurrently instead of one at a time
- allow `codex-switch remove <alias>` while Codex is running, but only when the target alias can be proven not to be the live account

The design keeps `use` guarded and mutating. It keeps plain `add` on the current live-auth path and only introduces isolation when the user explicitly asks for it. It also strengthens `list --refresh` so probing is explicitly isolated and non-mutating, which makes parallel refresh safe.

## Goals

- Let users capture a new alias without touching live `~/.codex/auth.json` by opting into `add --isolated`.
- Preserve composition with existing add login mode flags, especially `--device-auth`.
- Speed up `list --refresh` when multiple aliases need probing.
- Allow removal of inactive aliases while Codex is running.
- Prevent removal of the live account with a safeguard stronger than `state.json` alone.

## Non-Goals

- No change to `codex-switch use`; it must continue to require that Codex is not running.
- No silent auto-fallback from plain `add` to isolated add.
- No background refresh daemon changes.
- No attempt to guess the live alias when Codex is running and the live account identity cannot be determined.
- No new persistent store for account identity beyond the existing automation cache.

## User Experience

### Add

Current default behavior remains:

```text
codex-switch add work
codex-switch add work --device-auth
```

New isolated variants:

```text
codex-switch add work --isolated
codex-switch add work --isolated --device-auth
```

Behavior:

- Plain `add` keeps the current live-auth backup, login, capture, and restore flow.
- Plain `add` still refuses to run when a Codex process is active.
- When plain `add` is blocked by the process guard, the error should explicitly suggest `--isolated`.
- `add --isolated` does not mutate live `~/.codex/auth.json` or `state.json`.
- `--isolated` and `--device-auth` are independent flags and may be used together.

### List Refresh

`codex-switch list --refresh` keeps the same command surface and output shape. The user-visible change is only that multiple unresolved aliases can refresh in parallel.

Behavior remains:

- cache-first
- best-effort refresh
- no command failure just because telemetry is missing
- output order stays stable

### Remove

`codex-switch remove <alias>` changes as follows:

- it still rejects removing the active alias recorded in `state.json`
- it no longer requires Codex to be stopped for every removal
- when Codex is running, removal is allowed only if the target alias can be proven not to be the live account
- if the live account cannot be identified while Codex is running, removal fails with an explicit safety error instead of guessing

## Why This Approach

Three approaches were considered:

1. Keep plain `add` and `use` unchanged, add an explicit isolated add mode, parallelize refresh only after making it non-mutating, and allow guarded remove with proof-based identity checks.
2. Auto-switch plain `add` into isolated mode when Codex is running and allow optimistic remove based on `state.json` plus snapshot digest only.
3. Make every add isolated and remove the process guard from remove entirely.

Option 1 is the chosen design. It preserves current semantics unless the user opts into isolation, keeps safety boundaries explicit, and avoids turning ambiguous live-account situations into destructive behavior. Option 2 is convenient but implicit. Option 3 changes too much existing behavior for too little benefit.

## Architecture

The change should stay within existing boundaries, with one small extraction for reuse:

- `cli.py`
  - parse `add --isolated`
  - continue to parse `--device-auth`
  - thread both flags into manager calls
- `manager.py`
  - support isolated add
  - refresh unresolved aliases in parallel
  - remove inactive aliases without a blanket process guard
  - enforce the stronger live-account safeguard on remove
- `codex_login.py`
  - remain the only module that builds the concrete `codex login` command
  - accept an optional environment override for isolated login
- shared isolated Codex-home helper
  - host the temporary `HOME` and `CODEX_HOME` logic that is currently embedded in `cli.py`
  - support isolated probing and isolated login capture from arbitrary auth bytes
- existing RPC-first, PTY-fallback probing logic
  - remain the source of account identity and rate-limit observations

No new daemon policy or new persistent schema is required.

## Add Flow Design

### Plain Add

Plain `add` keeps the existing transactional behavior:

1. ensure Codex is not running
2. verify alias is missing
3. synchronize the active snapshot from live auth if needed
4. back up live auth
5. run `codex login` in the real Codex home
6. capture the resulting `auth.json` into the new alias snapshot
7. restore the previous live auth and state
8. roll back the alias on failure

The only user-facing change is the failure hint when a Codex process is active.

### Isolated Add

`add --isolated` uses a separate path:

1. verify alias is missing
2. create a temporary home and `.codex` directory
3. run `codex login` or `codex login --device-auth` inside that isolated environment
4. require the isolated login flow to leave `auth.json` in the temporary Codex home
5. write the captured auth into the new alias snapshot
6. remove the temporary directory

Important rules:

- do not call the live-auth process guard for isolated add
- do not back up or restore live `~/.codex/auth.json`
- do not synchronize the active alias snapshot from live auth before isolated add
- do not change `state.json`
- keep the existing rollback guarantee if snapshot creation fails after capture

This is an explicit trade-off: isolated add is safe to run while Codex is active because it does not touch the live home, but it also does not opportunistically persist fresh live-session token updates into the currently active alias snapshot.

### Login Mode Composition

Login mode and isolation mode are separate concerns:

- login mode answers how Codex logs in
- isolation answers where Codex logs in

The manager should represent them separately rather than overloading `LoginMode`. `--device-auth` remains the actual CLI flag name and should work with both plain and isolated add.

## List Refresh Design

### Make Refresh Explicitly Non-Mutating

Parallel refresh is only safe if refresh probing never swaps live auth under the real Codex home.

The current default probe already reads auth bytes and launches RPC or PTY inside a temporary `HOME` and `CODEX_HOME`. This design makes that the required refresh model for `list --refresh`.

Manager behavior should change from:

- probe alias
- if direct probe fails for an inactive alias, optionally fall back to backup, replace, probe, and restore live auth

to:

- probe each alias through isolated auth bytes only
- never mutate live auth as part of `list --refresh`

This is a deliberate tightening of behavior, not just an implementation detail.

### Parallel Probe Flow

`CodexSwitchManager.list_aliases(refresh=True)` should:

1. read aliases and cached metadata
2. build cache-derived list entries
3. identify unresolved aliases
4. submit isolated probes for unresolved aliases through a bounded worker pool
5. collect successful observations
6. persist observations back into the automation database serially
7. rebuild entries from the refreshed cache

Recommended worker-pool behavior:

- use a small fixed upper bound such as 4 workers
- preserve final output ordering by rebuilding from the original alias list after refresh
- treat each alias probe failure independently

### Persistence Strategy

Probe work may run concurrently, but database writes should remain serialized.

The automation store currently opens a new SQLite connection per operation. That is fine for single-threaded use, but concurrent probe result persistence introduces avoidable lock contention. The design therefore keeps SQLite writes on the main thread after worker results are collected.

## Remove Safety Design

### Baseline Rules

These rules always apply:

- if `alias == state.active_alias`, reject removal
- if the alias snapshot does not exist, keep the current missing-alias error

### When Codex Is Not Running

If no Codex process is running, `remove` may delete any non-active alias without further live-account checks.

### When Codex Is Running

If Codex is running, `remove` must prove that the target alias is not the live account before deleting it.

Recommended proof order:

1. Reject immediately if `alias == state.active_alias`.
2. If live `auth.json` is missing, fail with a safety error because the live account cannot be identified.
3. Compare the target snapshot digest to the live `auth.json` digest.
4. If the digests match, reject removal.
5. If the digests differ, run isolated identity probes for:
   - live `auth.json`
   - the target alias snapshot
6. Compare identities:
   - prefer account fingerprint when both sides provide one
   - otherwise compare account email when both sides provide one
7. If the identities match, reject removal.
8. If either side cannot be identified reliably, fail with a safety error instead of proceeding.
9. Only allow removal when the identities are known and different.

This matches the desired policy: when Codex is running, removal should fail if the live account cannot be identified.

### Identity Source

The remove safeguard should not rely only on cached automation metadata. It should compare live and target identities directly from isolated probes of the actual auth payloads being evaluated.

Cached metadata may still be reused as an optimization or diagnostic aid, but it should not be the only proof path.

## Process Guard Changes

The current process guard is raise-only. `add` and `use` can keep that API, but `remove` needs to distinguish:

- Codex is definitely not running
- Codex is running

The simplest design is:

- add a non-raising `is_codex_running()` helper
- keep `ensure_codex_not_running()` as a thin wrapper around it

This avoids process-list duplication and keeps the guard logic centralized.

## Error Handling

### Add

- Plain `add` still raises the current process-running error, but with a command-specific hint to use `--isolated`.
- `add --isolated` preserves the current login failure behavior:
  - login launch failure is fatal
  - non-zero login exit is fatal
  - missing isolated `auth.json` after login is fatal
  - partial alias capture must be rolled back

### List

- `list --refresh` must not fail because one alias probe fails.
- `list --refresh` must not mutate live auth.
- failed probes leave the affected alias unresolved and render `?` where usage is missing.

### Remove

- removing the active alias is always fatal
- removing an alias that matches live auth by digest is fatal when Codex is running
- removing an alias that matches live identity by fingerprint or email is fatal when Codex is running
- inability to identify the live account while Codex is running is fatal

These remove failures should be phrased as safety errors, not as internal probing failures.

## Testing

Add focused tests in the existing style.

### Add

- parser accepts `add --isolated`
- CLI dispatch threads both `isolated` and `device-auth`
- isolated add succeeds while the process guard would fail
- isolated add preserves live auth and `state.json`
- isolated add composes with `--device-auth`
- plain add still fails with a process-running error and includes the `--isolated` hint

### List

- manager refresh probes unresolved aliases concurrently using coordination primitives rather than sleeps
- final alias output order remains unchanged after concurrent refresh
- failed probe for one alias does not block successful refresh of others
- inactive refresh no longer performs live-auth backup, replacement, or restore

### Remove

- remove succeeds for an inactive alias while Codex is running when live and target identities are known and different
- remove rejects the `state.json` active alias while Codex is running
- remove rejects when the target snapshot digest matches live auth
- remove rejects when isolated identity probes show the same account despite different auth bytes
- remove rejects when live auth is missing while Codex is running
- remove rejects when live or target identity cannot be determined while Codex is running

Full-suite `pytest` remains the verification gate before merge.

## Implementation Notes

- Extract the isolated Codex-home helper out of `cli.py` so add, list refresh, and remove can share it.
- Keep the RPC-first, PTY-fallback probe contract; do not introduce a second identity-fetching mechanism.
- Prefer direct auth-byte probing over cached metadata for live-account safety checks.
- Preserve current exact output and command text stability except for the intentional `--isolated` flag and improved plain-add error hint.

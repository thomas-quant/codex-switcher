# Codex Switch Device Auth And Plan Display Design

Date: 2026-04-05

## Summary

Extend `codex-switch` in two small, focused ways:

- allow headless alias capture through `codex-switch add <alias> --device-auth`
- show known account plan types in `codex-switch list` as `alias -- type`

The design keeps the existing auth snapshot model and daemon telemetry model intact. Browser-based login remains the default. Device auth is an explicit opt-in mode for `add`. Account type display is cache-first and only attempts a targeted telemetry refresh for aliases whose plan type is still unknown.

## Goals

- Add a headless-friendly login path for alias capture without changing the default login UX.
- Surface known account plan types directly in `codex-switch list`.
- Reuse existing automation telemetry and probing logic instead of introducing a separate metadata store.
- Keep `list` safe and tolerant of partial telemetry.
- Preserve the current rollback and restore guarantees around `add`.

## Non-Goals

- No callback-URL paste flow in v1.
- No browser automation.
- No requirement that every alias always has a known plan type.
- No persistent background refresh initiated by `list`.
- No changes to daemon switching policy or handoff behavior.

## User Model

Users continue to manage aliases such as `work`, `backup-a`, and `backup-b`.

Normal login remains:

```text
codex-switch add work
```

Headless login becomes:

```text
codex-switch add work --device-auth
```

Alias listing remains stable and readable:

- `* beta -- plus`
- `  backup-a -- pro`
- `  backup-b`

If an alias has no known plan type yet, `list` prints only the alias name. It does not invent placeholders such as `unknown`.

## Why This Approach

Three approaches were considered:

1. Add an explicit `--device-auth` flag and keep list output cache-first with a targeted refresh for missing types.
2. Prompt interactively on every `add` for login mode and probe all aliases on every `list`.
3. Leave login unchanged and only show plan types after daemon/background telemetry eventually fills them in.

Option 1 is the chosen design. It keeps the normal path unchanged, provides a clean headless mode for users who need it, and avoids turning `list` into a slow or always-mutating command. Option 2 adds unnecessary friction and auth churn. Option 3 does not satisfy the requirement that `list` should try to fetch missing types when possible.

## Architecture

The change stays within the existing boundaries:

- `cli.py`
  - parse `add --device-auth`
  - format alias rows with an optional ` -- <plan_type>` suffix
- `manager.py`
  - route add requests by login mode
  - return list display data instead of raw names only
  - opportunistically refresh missing alias plan types through existing telemetry paths
- `codex_login.py`
  - remain the only module that knows the concrete `codex login` command arguments
- `automation_db.py`
  - remain the single cached source of alias plan metadata through the existing `aliases` table
- existing RPC/PTy probing logic
  - reused for one-shot plan-type refresh

No new datastore is introduced.

## Command Surface

### `add`

Current:

```text
codex-switch add <alias>
```

New:

```text
codex-switch add <alias> [--device-auth]
```

Behavior:

- default mode runs the normal `codex login` flow
- `--device-auth` runs `codex login --device-auth`
- `--device-auth` is optional and explicit
- `add` still does not change the active alias

### `list`

Current:

- prints aliases with active marker only

New:

- prints aliases with active marker and optional plan type suffix
- known type format is `alias -- type`
- unknown type format is just `alias`

Examples:

```text
* beta -- plus
  backup-a -- pro
  backup-b
```

## Add Flow Design

The existing `add` implementation is already transactional:

1. verify it is safe to mutate auth
2. preserve the active alias snapshot if needed
3. back up live auth
4. run login
5. capture the resulting `auth.json` into the new alias snapshot
6. restore previous live auth and state
7. roll back the new alias on failure

That flow remains unchanged. The only design change is that login execution becomes mode-aware.

The manager should accept a mode selection for `add`, for example:

- browser login
- device-auth login

The login module should translate that mode into the concrete subprocess call. This keeps CLI parsing and command construction separated.

## List Data Flow

`codex-switch list` should use this order:

1. Read the alias inventory from the snapshot store.
2. Read cached alias metadata from the automation database.
3. Attach known `account_plan_type` values to matching aliases.
4. If any alias still has no plan type, try a one-shot refresh for just those aliases.
5. Re-read or update the in-memory metadata map and render the final output.

The one-shot refresh is best-effort. If it fails, `list` still succeeds and prints plain aliases for unresolved entries.

## One-Shot Refresh Rules

The missing-type refresh should reuse the existing telemetry sources rather than invent a second fetching mechanism.

Recommended behavior:

- If the unresolved alias is already active, poll it directly through the existing RPC-first, PTy-fallback path.
- If the unresolved alias is not active, only probe it if the existing safety guard allows auth mutation.
- When probing a non-active alias:
  - remember the original active alias
  - temporarily `use` the unresolved alias
  - run the existing telemetry poll/probe path
  - persist alias observation if plan data is available
  - restore the original active alias before returning

This mirrors the daemon's existing safe refresh model and avoids adding a new class of partial state transitions.

If probing is unsafe because Codex is running or the mutation guard fails, skip the refresh for that alias and continue rendering `list`.

## Metadata Rules

Plan type display comes only from observed telemetry:

- `automation.aliases.account_plan_type`
- or the plan type field on a fresh RPC rate-limit/account payload that is persisted back into the same cache

The snapshot JSON stored under `accounts/<alias>.json` remains opaque. The design does not parse auth snapshots to guess plan type.

When no plan type is known:

- do not print `unknown`
- do not fail the command
- print only the alias text

## Error Handling

### `add --device-auth`

Device-auth login preserves the current `add` error contract:

- if process launch fails, surface `LoginCaptureError`
- if login exits unsuccessfully, surface `LoginCaptureError`
- if login exits without leaving `~/.codex/auth.json`, surface `LoginCaptureError`
- always attempt restore and rollback exactly as the current `add` flow does

### `list`

`list` must remain resilient:

- stale or missing telemetry is not fatal
- inability to safely mutate auth for probing is not fatal
- RPC or PTy unavailability during opportunistic refresh is not fatal
- malformed snapshot storage and other existing hard integrity failures remain fatal

This keeps `list` usable as an inventory command even when telemetry is partial.

## Testing

Add focused tests in the existing style:

- CLI parser test for `add --device-auth`
- CLI dispatch test proving `main(["add", "work", "--device-auth"])` routes the flag correctly
- login runner tests for:
  - normal mode runs `codex login`
  - device-auth mode runs `codex login --device-auth`
- manager add tests proving the selected login mode reaches the login runner
- list formatting tests for:
  - alias with plan type
  - alias without plan type
  - active alias with plan type suffix
- manager list tests for:
  - cached plan types are returned without probing
  - missing plan types trigger one-shot refresh
  - unsafe refresh skips probing and leaves the alias unresolved
  - failed refresh does not fail the list command

No test should depend on a real Codex login, live RPC access, or a real `~/.codex` directory.

## Rollout Notes

This is a narrow incremental change:

- no migration is required for existing aliases
- existing telemetry records become immediately useful for `list`
- users who do not need headless login see no behavior change in `add`

Callback-link paste support is deferred to a later design if headless login still needs more flexibility after device auth lands.

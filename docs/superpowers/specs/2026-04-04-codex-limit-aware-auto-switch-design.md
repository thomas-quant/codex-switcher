# Codex Limit-Aware Auto-Switch Design

Date: 2026-04-04

## Summary

Build a limit-aware automation layer on top of `codex-switch` that monitors the active Codex account's rate limits and thread token usage, switches to a fresher account when thresholds are reached, and resumes the exact same Codex thread after restart.

The v1 design keeps the user's normal `codex` entrypoint and uses a per-user `codex-switchd` daemon. The daemon talks directly to the experimental local Codex app-server protocol for account identity, rate limits, thread lifecycle, and token usage. If RPC access is unavailable, the daemon falls back to a PTY-driven `/status` probe for rate-limit collection only. A minimal Codex companion plugin/app is explicitly deferred unless direct daemon integration later proves insufficient for exact-thread handoff.

## Goals

- Keep the user's normal `codex` workflow instead of forcing a wrapper entrypoint.
- Query account rate limits automatically from Codex rather than from manual configuration.
- Track both Codex-reported rate limits and per-thread token usage.
- Switch accounts automatically when the active account gets within 5% of a tracked limit.
- Resume the exact same Codex thread after account rotation.
- Fail conservatively when telemetry is missing, stale, or ambiguous.
- Preserve the existing safety model around auth snapshot rotation.

## Non-Goals

- No browser automation or scraping of ChatGPT web UI.
- No guessing of account limits from token estimates alone.
- No mid-turn switching except after an actual limit-exceeded condition has already stopped the turn.
- No replacement of the stock Codex TUI or CLI with a custom client in v1.
- No support for switching multiple concurrent active Codex sessions in v1.
- No cloud-hosted coordination service.
- No OAuth API or ChatGPT web dashboard integration in v1.
- No required Codex plugin for rate-limit collection in v1.

## User Model

The user already manages multiple Codex aliases with `codex-switch`, for example `work-1`, `work-2`, and `backup`. One alias remains globally active at a time through `~/.codex/auth.json`.

With this feature enabled, the user continues launching plain `codex`. A background `codex-switchd` process observes the active thread and account telemetry through local Codex interfaces. When the active account crosses the configured threshold or actually exhausts its limit, the daemon coordinates a safe handoff: stop Codex, switch to a fresher alias, and resume the same thread.

## Why This Approach

Three approaches were considered:

1. Require all sessions to start via `codex-switch run`.
2. Build a full custom Codex client on the experimental app-server protocol.
3. Keep plain `codex`, add a background daemon that talks directly to Codex app-server and falls back to CLI PTY probing for rate-limit data.

Option 3 is the chosen design. Option 1 would be simpler but changes the user's workflow. Option 2 would provide maximum control but is too large and fragile for v1. Direct daemon integration preserves the stock Codex entrypoint while avoiding unsupported browser scraping. If direct daemon integration later proves insufficient for trustworthy exact-thread handoff, a minimal companion can be added in a follow-up iteration.

## Architecture Overview

The system is split into two required responsibilities:

- `codex-switch`
  - remains the source of truth for alias snapshots and auth rotation
  - gains daemon management and automation status commands
  - remains the only component allowed to mutate stored auth snapshots and `~/.codex/auth.json`
- `codex-switchd`
  - long-lived per-user daemon
  - persists automation state in SQLite under `~/.codex-switch/`
  - launches or connects to local Codex app-server RPC in read-only mode
  - falls back to PTY `/status` probes for rate-limit data when RPC is unavailable
  - applies switching policy and performs handoff orchestration

This separation keeps observation, policy, and mutation isolated.

A minimal Codex companion plugin/app is deferred and not part of required v1 scope. It becomes relevant only if direct daemon integration cannot observe the active thread reliably enough for exact-thread continuation.

## Codex Integration Model

The design depends on local Codex interfaces rather than undocumented web APIs or browser automation.

### Usage Data Paths

V1 daemon source order:

1. local Codex app-server RPC for account identity, rate limits, thread lifecycle, and token usage
2. CLI PTY `/status` probing for rate-limit windows and credits when RPC is unavailable

Deferred paths:

- OAuth API access via `auth.json`
- ChatGPT web dashboard scraping
- local session-log cost scanning for historical backfill

The daemon's primary RPC path should use a local read-only app-server process equivalent to:

- `codex -s read-only -a untrusted app-server`

The daemon must consume these RPC surfaces when available:

- `account/read`
- `account/rateLimits/read`
- `account/rateLimits/updated`
- `thread/tokenUsage/updated`
- thread lifecycle notifications sufficient to identify the active thread and safe checkpoints

The PTY fallback sends `/status` to a Codex TTY session and parses reported credits and limit windows. PTY fallback is sufficient for keeping alias rate-limit snapshots fresh, but it is not by itself trustworthy enough for exact-thread continuation.

The design assumes the daemon can obtain a stable active `thread_id` from direct RPC integration and that the exact same thread can be resumed after restart using the recorded thread identifier. If the protocol changes in a future Codex release, the daemon must fail closed rather than act on uncertain state.

## On-Disk Layout

Existing `codex-switch` files remain:

- `~/.codex-switch/state.json`
- `~/.codex-switch/accounts/<alias>.json`

New v1 automation files:

- `~/.codex-switch/automation.sqlite`
- `~/.codex-switch/daemon.pid` or equivalent lock file
- `~/.codex-switch/logs/` for daemon logs

Permissions stay private:

- `~/.codex-switch`: `0700`
- SQLite database: `0600`
- daemon lock/state files: `0600`

## Local Database Design

The existing JSON state file remains for simple alias state. Automation data moves to SQLite so the daemon can recover safely after restarts.

### `aliases`

Purpose: known alias inventory plus the best-known mapping to Codex account identity.

Suggested fields:

- `alias`
- `account_email`
- `account_plan_type`
- `account_fingerprint`
- `last_observed_at`

This table is best-effort metadata. Alias auth snapshots remain authoritative in the existing account snapshot store.

### `account_rate_limits`

Purpose: latest authoritative rate-limit snapshot per alias and limit bucket.

Suggested fields:

- `alias`
- `limit_id`
- `limit_name`
- `observed_via`
- `plan_type`
- `primary_used_percent`
- `primary_resets_at`
- `primary_window_duration_mins`
- `secondary_used_percent`
- `secondary_resets_at`
- `secondary_window_duration_mins`
- `credits_has_credits`
- `credits_unlimited`
- `credits_balance`
- `observed_at`

These rows are written only from Codex-reported rate-limit payloads.

### `thread_runtime`

Purpose: current durable view of each known thread's live runtime state.

Suggested fields:

- `thread_id`
- `cwd`
- `model`
- `current_alias`
- `last_turn_id`
- `last_known_status`
- `safe_to_switch`
- `last_total_tokens`
- `last_seen_at`

The daemon also keeps a notion of the currently attached thread, but thread rows remain durable after a session ends for recovery and audit purposes.

### `thread_turn_usage`

Purpose: append-only token usage history for reporting and debugging.

Suggested fields:

- `thread_id`
- `turn_id`
- `last_input_tokens`
- `last_cached_input_tokens`
- `last_output_tokens`
- `last_reasoning_output_tokens`
- `last_total_tokens`
- `total_input_tokens`
- `total_cached_input_tokens`
- `total_output_tokens`
- `total_reasoning_output_tokens`
- `total_tokens`
- `observed_at`

This table is observational telemetry. It does not replace Codex-reported account limits.

### `switch_events`

Purpose: audit trail of every switch decision and outcome.

Suggested fields:

- `id`
- `thread_id`
- `from_alias`
- `to_alias`
- `trigger_type`
- `trigger_limit_id`
- `trigger_used_percent`
- `requested_at`
- `switched_at`
- `resumed_at`
- `result`
- `failure_message`

### `handoff_state`

Purpose: exactly one durable in-flight handoff record for crash recovery.

Suggested fields:

- `thread_id`
- `source_alias`
- `target_alias`
- `phase`
- `reason`
- `retry_count`
- `updated_at`

Example phases:

- `pending_idle_checkpoint`
- `pending_stop`
- `pending_switch`
- `pending_resume`
- `failed_resume`

## Switching Policy

The daemon acts only on authoritative account rate-limit snapshots and live thread lifecycle signals.

### Startup

On daemon startup:

1. open the automation database
2. reconcile aliases from `codex-switch`
3. restore any unfinished `handoff_state`
4. start or connect to local Codex app-server RPC
5. if RPC succeeds, query `account/read` and `account/rateLimits/read`
6. subscribe to account and thread notifications
7. if RPC is unavailable, enter degraded mode and start PTY `/status` probing for rate-limit collection only

If RPC is unavailable, the daemon may continue updating alias limit snapshots through PTY fallback, but it must not attempt exact-thread handoff unless it has a trustworthy active `thread_id`.

### Normal Monitoring

While Codex is active, the daemon:

- updates `thread_runtime` from app-server thread lifecycle notifications
- appends token usage rows from `thread/tokenUsage/updated`
- refreshes `account_rate_limits` from `account/rateLimits/read` and `account/rateLimits/updated`
- uses PTY `/status` fallback only when RPC rate-limit telemetry is unavailable
- identifies whether the current thread is at a safe checkpoint for handoff

If the daemon only has PTY fallback and does not have trustworthy thread runtime visibility, it remains in observe-only mode and does not auto-switch.

### Trigger Conditions

Soft trigger:

- if either tracked rate-limit window reaches `>=95%` used, mark the current thread for switch at the next safe checkpoint

Hard trigger:

- if Codex reports a usage-limit-exceeded condition for the current turn, switch as soon as the turn has stopped

The daemon must not interrupt an in-flight healthy turn merely because the soft threshold has been crossed.

Automatic switching is armed only when the daemon has both:

- fresh authoritative rate-limit telemetry for the active alias and candidate backup aliases
- a trustworthy active `thread_id` and safe-checkpoint signal from RPC integration

### Target Alias Selection

Candidate aliases must satisfy all of:

- stored alias snapshot exists
- latest rate-limit snapshot is recent enough to trust
- not currently the active alias

Selection priority:

1. aliases below the soft threshold on the primary window
2. aliases below the soft threshold on the secondary window
3. lowest primary used percentage
4. lowest secondary used percentage
5. earliest reset time if every alias is near exhaustion

If no alias has fresh authoritative telemetry, the daemon must fail closed and notify the user rather than guess.

PTY-derived snapshots are acceptable for candidate selection only when RPC-derived rate-limit data is unavailable for that alias. They are never sufficient on their own to justify exact-thread resume behavior.

## Handoff Sequence

For a soft-triggered switch:

1. persist `handoff_state`
2. wait for a safe idle checkpoint
3. confirm the active `thread_id`
4. stop Codex cleanly
5. run `codex-switch use <target_alias>`
6. resume the recorded thread
7. clear `handoff_state`
8. append a successful `switch_events` row

For a hard-triggered switch after limit exhaustion:

1. persist `handoff_state`
2. wait only until the failed turn has fully stopped
3. switch aliases
4. resume the thread

The daemon should prefer the stock `codex resume <thread_id>` path if it is sufficient to restore the exact thread under the new auth state.

If the daemon cannot confirm a trustworthy `thread_id`, it must not perform the handoff sequence automatically.

## Failure Handling

The daemon must fail closed in ambiguous states.

Expected failures and responses:

- missing or stale rate-limit data for all backup aliases
  - refuse to switch automatically
  - record blocked state and notify the user
- app-server RPC unavailable
  - enter PTY fallback for rate-limit collection
  - disable exact-thread auto-switch until RPC visibility returns
- PTY `/status` parse failure
  - mark the affected alias telemetry stale
  - retry on the next refresh cycle rather than acting on partial data
- switch succeeds but resume fails
  - keep target alias active
  - persist `handoff_state=failed_resume`
  - expose a retry command using the stored thread id
- daemon crash mid-handoff
  - recover from `handoff_state`
  - retry or surface exact failed phase without guessing
- Codex runtime state does not identify a trustworthy active thread
  - do not switch
  - notify the user that exact-thread continuation is unavailable

## Command Surface

The existing commands remain unchanged.

Planned new commands:

- `codex-switch daemon install`
- `codex-switch daemon start`
- `codex-switch daemon stop`
- `codex-switch daemon status`
- `codex-switch auto status`
- `codex-switch auto history`
- `codex-switch auto source`
- `codex-switch auto retry-resume`

Deferred commands:

- `codex-switch auto pause`
- `codex-switch auto force-switch`
- `codex-switch auto doctor`

## Deferred Companion Contingency

V1 does not require a Codex plugin/app.

If direct daemon integration later proves insufficient for reliable active-thread identification or safe-checkpoint detection, a minimal companion may be introduced with these narrow responsibilities:

- expose the active thread more reliably to the daemon
- bridge live thread lifecycle state already visible inside Codex
- avoid adding any business logic that duplicates daemon policy

Even in that contingency design, the companion must not:

- mutate auth state
- choose target aliases
- directly resume threads on its own policy
- become the source of truth for rate-limit history

## Service Model

`codex-switchd` runs as a per-user background service started on login. The exact service mechanism can vary by platform, but the daemon must guarantee single-instance behavior and a clear health status.

The daemon should tolerate Codex starting before or after the service. It must also tolerate Codex being absent entirely and simply wait idle for a future session.

## Security Considerations

This feature still manages sensitive auth snapshots. Existing safeguards remain mandatory:

- only `codex-switch` mutates auth snapshots
- all auth and state writes stay atomic
- private filesystem permissions remain enforced
- the daemon stores metadata and telemetry, not raw auth tokens
- uncertain or stale telemetry must not cause speculative switching

## Testing Strategy

Testing should cover:

- database schema creation and migrations
- authoritative rate-limit snapshot ingestion from RPC and PTY fallback
- token usage ingestion and append-only history
- target alias ranking from mixed limit states
- soft-threshold switching at safe checkpoints
- hard switching after usage-limit-exceeded failures
- handoff recovery after daemon crash at each phase
- retrying failed resume attempts
- stale telemetry disabling automation
- preserving existing `codex-switch` mutation safety guarantees

Integration tests should stub app-server JSON-RPC and PTY `/status` output rather than require a live Codex session.

## Open Risks

- Codex app-server interfaces are experimental and may change.
- PTY `/status` parsing is a brittle fallback and should remain secondary to RPC.
- Exact thread resumption depends on stable thread identifiers and compatible resume semantics across account changes.
- Some account telemetry may be unavailable until a given alias has been observed at least once by the daemon.
- If direct daemon integration cannot observe active-thread state reliably enough, a small companion may still be required in a follow-up iteration.

These risks are acceptable for v1 because they are still materially safer and more reliable than browser automation or manually maintained quota configuration.

# codex-switch

`codex-switch` manages multiple Codex login snapshots behind short aliases and swaps the active login by rotating `~/.codex/auth.json`.

The tool is intentionally narrow:

- Only `~/.codex/auth.json` is rotated between aliases.
- Other Codex state such as config, history, logs, and related files remain shared in `~/.codex`.
- Mutating commands refuse to run while a Codex process is active.

## Install

For normal CLI use, install with `pipx` so `codex-switch` and `codex-switchd` are on your `PATH` without activating a virtual environment:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
python3 -m pipx install --editable .
```

If you are developing on the repository and want the traditional editable environment with test dependencies, use a local virtual environment instead:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

## Commands

### `codex-switch add <alias> [--device-auth]`

Captures a fresh `codex login` session into a named snapshot. The existing active login is restored after capture. Use `--device-auth` when you need the Codex device-code flow instead of the default browser login.

### `codex-switch list [--refresh]`

Lists configured aliases. The active alias is marked with `*`. `list` shows cached account plan type plus remaining 5-hour and weekly usage when telemetry is available. Missing usage values render as `?`.

Pass `--refresh` to re-probe aliases whose telemetry is missing or older than the freshness window before printing the list.

Display mode is controlled by `~/.codex-switch/config.json`:

```json
{
  "list_format": "labelled"
}
```

Accepted values are `labelled` and `table`. Missing or invalid config falls back to `labelled`.

### `codex-switch use <alias>`

Copies the stored snapshot for `<alias>` into `~/.codex/auth.json` and marks that alias active.

### `codex-switch status`

Shows the active alias, whether its snapshot exists, whether `~/.codex/auth.json` exists, and whether the live auth file has drifted from the stored snapshot.

### `codex-switch remove <alias>`

Deletes a stored alias snapshot. Removing the active alias is refused.

### `codex-switch daemon install`

Initializes automation state storage and daemon directories under `~/.codex-switch/`.

### `codex-switch daemon start|stop|status`

Manages the background `codex-switchd` process used for automation monitoring.

### `codex-switch auto status`

Shows automation readiness for the active alias, including latest telemetry source and whether a soft-switch trigger is armed.

### `codex-switch auto source`

Shows the latest telemetry source timestamp for each configured alias.

### `codex-switch auto history [--limit N]`

Shows recent recorded switch events from the local automation database.

### `codex-switch auto retry-resume`

Attempts `codex resume <thread_id>` when automation is in `failed_resume` handoff state and clears the handoff record on success.

## Usage

Check the current state first:

```bash
codex-switch status
codex-switch list
```

If `codex-switch` shows `active alias: none` but your live `~/.codex/auth.json` already exists, bootstrap that current login once before adding the others:

```bash
python3 - <<'PY'
from dataclasses import replace

from codex_switch.accounts import AccountStore
from codex_switch.manager import utc_now
from codex_switch.paths import resolve_paths
from codex_switch.state import StateStore

paths = resolve_paths()
accounts = AccountStore(paths.accounts_dir)
state = StateStore(paths.state_file)
current = state.load()

accounts.assert_missing("alpha")
accounts.write_snapshot_from_file("alpha", paths.live_auth_file)
state.save(replace(current, active_alias="alpha", updated_at=utc_now()))
PY
```

Example first-time setup:

```bash
codex-switch add beta --device-auth
codex-switch add gamma
codex-switch add delta
codex-switch add epsilon
codex-switch use epsilon
codex-switch list
```

Labelled output:

```text
  beta -- plus -- 5h left: ? -- weekly left: ?
  delta -- 5h left: ? -- weekly left: ?
* epsilon -- 5h left: ? -- weekly left: ?
  gamma -- pro -- 5h left: ? -- weekly left: ?
```

Table mode config:

```json
{
  "list_format": "table"
}
```

Switch accounts when you hit limits:

```bash
codex-switch use alpha
codex-switch use beta
codex-switch status
```

Start automation daemon management:

```bash
codex-switch daemon install
codex-switch daemon start
codex-switch daemon status
```

Inspect automation telemetry and decisions:

```bash
codex-switch auto status
codex-switch auto source
codex-switch auto history --limit 10
```

Remove an alias you no longer need:

```bash
codex-switch remove epsilon
```

## Important behavior

`codex-switch` does not create isolated Codex homes. It only rotates `~/.codex/auth.json` so account login can change while the rest of the Codex directory stays shared.

For safety, mutating commands such as `add`, `use`, and `remove` refuse to run while Codex is active.

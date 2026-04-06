from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from codex_switch.accounts import AccountStore
from codex_switch.automation_db import AliasRecord, AutomationStore, RateLimitRecord, SwitchEventRecord
from codex_switch.automation_models import HandoffPhase, RateLimitSnapshot, RateLimitWindow
from codex_switch.automation_policy import choose_target_alias, should_trigger_soft_switch
from codex_switch.daemon_controller import DaemonController
from codex_switch.errors import (
    ActiveAliasRemovalError,
    AutomationDatabaseError,
    AutomationHandoffError,
    LoginCaptureError,
)
from codex_switch.fs import atomic_write_bytes, ensure_private_dir, file_digest
from codex_switch.models import (
    AliasListEntry,
    AliasTelemetryObservation,
    AppPaths,
    AppState,
    AutoSourceResult,
    AutoStatusResult,
    DaemonStatusResult,
    LoginMode,
    StatusResult,
)
from codex_switch.state import StateStore


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_codex_resume(thread_id: str) -> None:
    subprocess.run(["codex", "resume", thread_id], check=True)


class CodexSwitchManager:
    def __init__(
        self,
        paths: AppPaths,
        accounts: AccountStore,
        state: StateStore,
        ensure_safe_to_mutate: Callable[[], None],
        login_runner: Callable[[LoginMode], None],
        automation: AutomationStore | None = None,
        daemon_controller: DaemonController | None = None,
        soft_switch_threshold: float = 95.0,
        resume_runner: Callable[[str], None] = run_codex_resume,
        alias_metadata_probe: Callable[[str], AliasTelemetryObservation | None] | None = None,
    ) -> None:
        self._paths = paths
        self._accounts = accounts
        self._state = state
        self._ensure_safe_to_mutate = ensure_safe_to_mutate
        self._login_runner = login_runner
        self._automation = automation if automation is not None else AutomationStore(paths.automation_db_file)
        self._daemon_controller = (
            daemon_controller if daemon_controller is not None else DaemonController(paths)
        )
        self._soft_switch_threshold = soft_switch_threshold
        self._resume_runner = resume_runner
        self._alias_metadata_probe = alias_metadata_probe

    def list_aliases(self) -> tuple[list[AliasListEntry], str | None]:
        current = self._state.load()
        aliases = self._accounts.list_aliases()
        metadata = self._metadata_by_alias()
        latest_rate_limits = self._latest_rate_limits_by_alias(aliases)
        entries = self._build_alias_entries(aliases, metadata, latest_rate_limits)

        unresolved_aliases = [
            entry.alias
            for entry in entries
            if entry.plan_type is None
            or entry.five_hour_left_percent is None
            or entry.weekly_left_percent is None
        ]
        if unresolved_aliases and self._alias_metadata_probe is not None:
            refreshed = self._refresh_missing_alias_metadata(
                unresolved_aliases=unresolved_aliases,
                previous_state=current,
                metadata=metadata,
            )
            if refreshed:
                metadata = self._metadata_by_alias()
                latest_rate_limits = self._latest_rate_limits_by_alias(aliases)
                entries = self._build_alias_entries(aliases, metadata, latest_rate_limits)

        return entries, current.active_alias

    def _metadata_by_alias(self) -> dict[str, AliasRecord]:
        if not self._paths.automation_db_file.exists():
            return {}
        try:
            return {row.alias: row for row in self._automation.list_aliases()}
        except AutomationDatabaseError:
            return {}

    def _latest_rate_limits_by_alias(self, aliases: list[str]) -> dict[str, RateLimitRecord]:
        if not self._paths.automation_db_file.exists():
            return {}

        rows: dict[str, RateLimitRecord] = {}
        for alias in aliases:
            try:
                latest = self._automation.latest_rate_limit_for_alias(alias)
            except AutomationDatabaseError:
                return {}
            if latest is not None:
                rows[alias] = latest
        return rows

    def _build_alias_entries(
        self,
        aliases: list[str],
        metadata: dict[str, AliasRecord],
        latest_rate_limits: dict[str, RateLimitRecord],
    ) -> list[AliasListEntry]:
        entries: list[AliasListEntry] = []
        for alias in aliases:
            alias_metadata = metadata.get(alias)
            rate_limit = latest_rate_limits.get(alias)
            entries.append(
                AliasListEntry(
                    alias=alias,
                    plan_type=_normalize_plan_type(
                        None if alias_metadata is None else alias_metadata.account_plan_type
                    ),
                    five_hour_left_percent=_remaining_percent(
                        None if rate_limit is None else rate_limit.primary_used_percent
                    ),
                    weekly_left_percent=_remaining_percent(
                        None if rate_limit is None else rate_limit.secondary_used_percent
                    ),
                ),
            )
        return entries

    def _refresh_missing_alias_metadata(
        self,
        *,
        unresolved_aliases: list[str],
        previous_state: AppState,
        metadata: dict[str, AliasRecord],
    ) -> bool:
        refreshed = False
        for alias in unresolved_aliases:
            observation = self._probe_alias_metadata(alias=alias, previous_state=previous_state)
            if observation is None:
                continue

            plan_type = _normalize_plan_type(observation.account_plan_type)
            if plan_type is not None:
                existing = metadata.get(alias)
                try:
                    self._automation.record_alias_observation(
                        alias=alias,
                        account_email=(
                            observation.account_email
                            if observation.account_email is not None
                            else None if existing is None else existing.account_email
                        ),
                        account_plan_type=plan_type,
                        account_fingerprint=(
                            observation.account_fingerprint
                            if observation.account_fingerprint is not None
                            else None if existing is None else existing.account_fingerprint
                        ),
                        observed_at=observation.observed_at,
                    )
                except AutomationDatabaseError:
                    continue
                refreshed = True
            for snapshot in observation.rate_limits:
                try:
                    self._automation.upsert_rate_limit(snapshot)
                except AutomationDatabaseError:
                    continue
                refreshed = True
        return refreshed

    def _probe_alias_metadata(
        self,
        *,
        alias: str,
        previous_state: AppState,
    ) -> AliasTelemetryObservation | None:
        if self._alias_metadata_probe is None:
            return None

        if alias == previous_state.active_alias:
            try:
                return self._alias_metadata_probe(alias)
            except Exception:
                return None

        try:
            self._ensure_safe_to_mutate()
        except Exception:
            return None

        backup_path: Path | None = None
        clear_unmanaged_live_auth = False
        observation: AliasTelemetryObservation | None = None
        probe_error: Exception | None = None
        try:
            backup_path = self._backup_live_auth()
            clear_unmanaged_live_auth = True
            atomic_write_bytes(
                self._paths.live_auth_file,
                self._accounts.read_snapshot(alias),
                mode=0o600,
                root=self._paths.codex_root,
            )
            observation = self._alias_metadata_probe(alias)
        except Exception as exc:
            probe_error = exc

        cleanup_errors: list[Exception] = []
        try:
            self._restore_previous_live_auth(
                previous_state,
                backup_path,
                clear_unmanaged_live_auth,
                prefer_live_backup=True,
            )
        except Exception as exc:
            cleanup_errors.append(exc)
        try:
            self._state.save(previous_state)
        except Exception as exc:
            cleanup_errors.append(exc)

        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise ExceptionGroup("Multiple cleanup failures", cleanup_errors)

        if probe_error is not None:
            return None
        return observation

    def status(self) -> StatusResult:
        current = self._state.load()
        active_alias = current.active_alias
        live_auth_exists = self._paths.live_auth_file.exists()
        snapshot_exists = False
        in_sync: bool | None = None

        if active_alias is not None:
            snapshot_exists = self._accounts.exists(active_alias)
            if snapshot_exists and live_auth_exists:
                in_sync = (
                    file_digest(self._accounts.snapshot_path(active_alias))
                    == file_digest(self._paths.live_auth_file)
                )

        return StatusResult(
            active_alias=active_alias,
            snapshot_exists=snapshot_exists,
            live_auth_exists=live_auth_exists,
            in_sync=in_sync,
        )

    def daemon_install(self) -> None:
        self._automation.initialize()
        self._daemon_controller.install()

    def daemon_start(self) -> DaemonStatusResult:
        self._automation.initialize()
        return self._daemon_controller.start()

    def daemon_stop(self) -> DaemonStatusResult:
        return self._daemon_controller.stop()

    def daemon_status(self) -> DaemonStatusResult:
        return self._daemon_controller.status()

    def auto_status(self) -> AutoStatusResult:
        self._automation.initialize()
        current = self._state.load()
        active_alias = current.active_alias
        if active_alias is None:
            return AutoStatusResult(
                active_alias=None,
                active_observed_via=None,
                active_observed_at=None,
                soft_switch_triggered=False,
                target_alias=None,
            )

        active_record = self._automation.latest_rate_limit_for_alias(active_alias)
        if active_record is None:
            return AutoStatusResult(
                active_alias=active_alias,
                active_observed_via=None,
                active_observed_at=None,
                soft_switch_triggered=False,
                target_alias=None,
            )

        active_snapshot = _rate_limit_record_to_snapshot(active_record)
        soft_switch_triggered = should_trigger_soft_switch(active_snapshot, self._soft_switch_threshold)

        target_alias: str | None = None
        if soft_switch_triggered:
            candidate_snapshots: list[RateLimitSnapshot] = []
            for alias in self._accounts.list_aliases():
                snapshot_record = self._automation.latest_rate_limit_for_alias(alias)
                if snapshot_record is None:
                    continue
                candidate_snapshots.append(_rate_limit_record_to_snapshot(snapshot_record))
            target_alias = choose_target_alias(
                active_alias=active_alias,
                candidates=candidate_snapshots,
                threshold=self._soft_switch_threshold,
            )

        return AutoStatusResult(
            active_alias=active_alias,
            active_observed_via=active_record.observed_via.value,
            active_observed_at=active_record.observed_at,
            soft_switch_triggered=soft_switch_triggered,
            target_alias=target_alias,
        )

    def auto_source(self) -> list[AutoSourceResult]:
        self._automation.initialize()
        source_rows: list[AutoSourceResult] = []
        for alias in self._accounts.list_aliases():
            latest = self._automation.latest_rate_limit_for_alias(alias)
            source_rows.append(
                AutoSourceResult(
                    alias=alias,
                    observed_via=latest.observed_via.value if latest is not None else None,
                    observed_at=latest.observed_at if latest is not None else None,
                )
            )
        return source_rows

    def auto_history(self, limit: int = 20) -> list[SwitchEventRecord]:
        self._automation.initialize()
        return self._automation.list_switch_events(limit=limit)

    def auto_retry_resume(self) -> str:
        self._automation.initialize()
        handoff = self._automation.get_handoff_state()
        if handoff is None:
            raise AutomationHandoffError("No handoff state is available for resume retry")
        if handoff.phase != HandoffPhase.failed_resume:
            raise AutomationHandoffError("Resume retry is only valid for failed_resume handoff state")
        self._automation.set_handoff_state(
            thread_id=handoff.thread_id,
            source_alias=handoff.source_alias,
            target_alias=handoff.target_alias,
            phase=HandoffPhase.pending_resume,
            reason=handoff.reason,
            updated_at=utc_now(),
        )
        try:
            self._resume_runner(handoff.thread_id)
        except Exception:
            self._automation.set_handoff_state(
                thread_id=handoff.thread_id,
                source_alias=handoff.source_alias,
                target_alias=handoff.target_alias,
                phase=HandoffPhase.failed_resume,
                reason=handoff.reason,
                updated_at=utc_now(),
            )
            raise
        self._automation.clear_handoff_state()
        return handoff.thread_id

    def _sync_active_snapshot_from_live_auth(self, state: AppState) -> None:
        if (
            state.active_alias is not None
            and self._paths.live_auth_file.exists()
            and self._accounts.exists(state.active_alias)
        ):
            atomic_write_bytes(
                self._accounts.snapshot_path(state.active_alias),
                self._paths.live_auth_file.read_bytes(),
                mode=0o600,
                root=self._paths.switch_root,
            )

    def _backup_live_auth(self) -> Path | None:
        if not self._paths.live_auth_file.exists():
            return None

        ensure_private_dir(self._paths.live_auth_file.parent, root=self._paths.codex_root)
        fd, raw_path = tempfile.mkstemp(
            prefix="auth-backup-",
            suffix=".json",
            dir=self._paths.live_auth_file.parent,
        )
        backup_path = Path(raw_path)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._paths.live_auth_file.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(backup_path, 0o600)
            self._paths.live_auth_file.unlink()
            return backup_path
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise

    def _restore_previous_live_auth(
        self,
        previous_state: AppState,
        backup_path: Path | None,
        clear_unmanaged_live_auth: bool,
        *,
        prefer_live_backup: bool = False,
    ) -> None:
        if prefer_live_backup:
            if backup_path is not None and backup_path.exists():
                atomic_write_bytes(
                    self._paths.live_auth_file,
                    backup_path.read_bytes(),
                    mode=0o600,
                    root=self._paths.codex_root,
                )
                backup_path.unlink(missing_ok=True)
                return

            if clear_unmanaged_live_auth and self._paths.live_auth_file.exists():
                self._paths.live_auth_file.unlink()
            return

        if (
            previous_state.active_alias is not None
            and self._accounts.exists(previous_state.active_alias)
        ):
            atomic_write_bytes(
                self._paths.live_auth_file,
                self._accounts.read_snapshot(previous_state.active_alias),
                mode=0o600,
                root=self._paths.codex_root,
            )
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
            return

        if backup_path is not None and backup_path.exists():
            atomic_write_bytes(
                self._paths.live_auth_file,
                backup_path.read_bytes(),
                mode=0o600,
                root=self._paths.codex_root,
            )
            backup_path.unlink(missing_ok=True)
            return

        if clear_unmanaged_live_auth and self._paths.live_auth_file.exists():
            self._paths.live_auth_file.unlink()

    def add(self, alias: str, *, login_mode: LoginMode = LoginMode.BROWSER) -> None:
        self._ensure_safe_to_mutate()
        self._accounts.assert_missing(alias)
        previous_state = self._state.load()
        backup_path: Path | None = None
        primary_error: Exception | None = None
        alias_captured = False
        clear_unmanaged_live_auth = False

        try:
            self._sync_active_snapshot_from_live_auth(previous_state)
            backup_path = self._backup_live_auth()
            clear_unmanaged_live_auth = True
            self._login_runner(login_mode)
            if not self._paths.live_auth_file.exists():
                raise LoginCaptureError("codex login did not leave ~/.codex/auth.json behind")
            self._accounts.write_snapshot_from_file(alias, self._paths.live_auth_file)
            alias_captured = True
        except Exception as exc:
            primary_error = exc

        cleanup_errors: list[Exception] = []
        try:
            self._restore_previous_live_auth(
                previous_state,
                backup_path,
                clear_unmanaged_live_auth,
            )
        except Exception as exc:
            cleanup_errors.append(exc)
        try:
            self._state.save(previous_state)
        except Exception as exc:
            cleanup_errors.append(exc)
        alias_needs_rollback = alias_captured
        if not alias_needs_rollback and primary_error is not None:
            alias_needs_rollback = self._accounts.exists(alias)
        if (primary_error is not None or cleanup_errors) and alias_needs_rollback and self._accounts.exists(alias):
            try:
                self._accounts.delete(alias)
            except Exception as exc:
                cleanup_errors.append(exc)

        if primary_error is not None:
            for cleanup_error in cleanup_errors:
                primary_error.add_note(f"Cleanup failed: {cleanup_error}")
            raise primary_error

        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise ExceptionGroup("Multiple cleanup failures", cleanup_errors)

    def use(self, alias: str) -> None:
        self._ensure_safe_to_mutate()
        current = self._state.load()
        target_snapshot = self._accounts.read_snapshot(alias)

        if current.active_alias is not None:
            self._sync_active_snapshot_from_live_auth(current)

        atomic_write_bytes(
            self._paths.live_auth_file,
            target_snapshot,
            mode=0o600,
            root=self._paths.codex_root,
        )
        self._state.save(replace(current, active_alias=alias, updated_at=utc_now()))

    def remove(self, alias: str) -> None:
        self._ensure_safe_to_mutate()
        current = self._state.load()
        if current.active_alias == alias:
            raise ActiveAliasRemovalError(f"Cannot remove active alias '{alias}'")
        self._accounts.delete(alias)


def _rate_limit_record_to_snapshot(record: RateLimitRecord) -> RateLimitSnapshot:
    return RateLimitSnapshot(
        alias=record.alias,
        limit_id=record.limit_id,
        limit_name=record.limit_name,
        observed_via=record.observed_via,
        plan_type=record.plan_type,
        primary_window=RateLimitWindow(
            used_percent=record.primary_used_percent,
            resets_at=record.primary_resets_at,
            window_duration_mins=record.primary_window_duration_mins,
        ),
        secondary_window=RateLimitWindow(
            used_percent=record.secondary_used_percent,
            resets_at=record.secondary_resets_at,
            window_duration_mins=record.secondary_window_duration_mins,
        ),
        credits_has_credits=record.credits_has_credits,
        credits_unlimited=record.credits_unlimited,
        credits_balance=record.credits_balance,
        observed_at=record.observed_at,
    )


def _normalize_plan_type(plan_type: str | None) -> str | None:
    if plan_type is None:
        return None
    normalized = plan_type.strip()
    return normalized or None


def _remaining_percent(used_percent: float | None) -> int | None:
    if used_percent is None:
        return None
    remaining = int(round(100 - used_percent))
    return max(0, min(100, remaining))

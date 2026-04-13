from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import signal
import subprocess
import threading
from typing import Protocol

from codex_switch.accounts import AccountStore
from codex_switch.automation_db import AutomationStore, HandoffStateRecord, RateLimitRecord
from codex_switch.automation_models import (
    AccountIdentitySnapshot,
    HandoffPhase,
    RateLimitSnapshot,
    RateLimitWindow,
    ThreadRuntimeSnapshot,
    ThreadTurnUsageSnapshot,
    UsageSource,
)
from codex_switch.automation_pty import parse_status_output
from codex_switch.automation_policy import choose_target_alias, should_trigger_soft_switch
from codex_switch.automation_rpc import (
    CodexRpcClient,
    parse_account_read_result,
    parse_rate_limits_result,
    parse_thread_runtime_notification,
    parse_thread_turn_usage_notification,
)
from codex_switch.errors import AutomationSourceUnavailableError
from codex_switch.manager import CodexSwitchManager
from codex_switch.paths import resolve_paths
from codex_switch.process_guard import ensure_codex_not_running
from codex_switch.state import StateStore

_SOFT_SWITCH_THRESHOLD = 95.0
_FRESH_TELEMETRY_SECONDS = 15 * 60


@dataclass(slots=True, frozen=True)
class RpcPollResult:
    account_identity: AccountIdentitySnapshot | None
    rate_limits: list[RateLimitSnapshot]
    thread_runtime: ThreadRuntimeSnapshot | None
    token_usage: list[ThreadTurnUsageSnapshot]
    hard_limit_exceeded: bool


class RpcSource(Protocol):
    def poll(self, *, active_alias: str | None) -> RpcPollResult:
        ...


class PtySource(Protocol):
    def probe(self, *, alias: str, observed_at: str) -> RateLimitSnapshot | None:
        ...


class CodexController(Protocol):
    def stop(self) -> None:
        ...

    def resume(self, thread_id: str) -> None:
        ...


class NullRpcSource:
    def poll(self, *, active_alias: str | None) -> RpcPollResult:
        raise AutomationSourceUnavailableError("Codex RPC source is not configured")


class NullPtySource:
    def probe(self, *, alias: str, observed_at: str) -> RateLimitSnapshot | None:
        return None


class SubprocessCodexController:
    def stop(self) -> None:
        subprocess.run(["pkill", "-TERM", "-x", "codex"], check=False)

    def resume(self, thread_id: str) -> None:
        subprocess.run(["codex", "resume", thread_id], check=True)


class AppServerRpcSource:
    def __init__(self, client_factory=None) -> None:
        self._client_factory = client_factory if client_factory is not None else CodexRpcClient.launch_default
        self._client: CodexRpcClient | None = None
        self._initialized = False
        self._next_request_id = 1

    def poll(self, *, active_alias: str | None) -> RpcPollResult:
        if active_alias is None:
            raise AutomationSourceUnavailableError("Active alias is required for Codex RPC polling")

        client = self._ensure_client()
        if not self._initialized:
            client.send_request(
                self._allocate_request_id(),
                "initialize",
                {"clientInfo": {"name": "codex-switchd", "version": "0.1.0"}},
            )
            self._initialized = True

        observed_at = _utc_now()
        account_response = client.send_request(self._allocate_request_id(), "account/read", {})
        rate_limit_response = client.send_request(
            self._allocate_request_id(),
            "account/rateLimits/read",
            {},
        )
        thread_runtime: ThreadRuntimeSnapshot | None = None
        token_usage: list[ThreadTurnUsageSnapshot] = []
        hard_limit_exceeded = False
        for message in client.drain_messages_nonblocking():
            method = message.payload.get("method")
            if method == "thread/runtime/updated":
                try:
                    thread_runtime = parse_thread_runtime_notification(
                        message.payload,
                        current_alias=active_alias,
                        observed_at=observed_at,
                    )
                except ValueError:
                    continue
                hard_limit_exceeded = (
                    thread_runtime.last_known_status in {"usage_limit_exceeded", "limit_exceeded"}
                )
            elif method == "thread/tokenUsage/updated":
                try:
                    token_usage.append(
                        parse_thread_turn_usage_notification(
                            message.payload,
                            observed_at=observed_at,
                        )
                    )
                except ValueError:
                    continue

        account_identity = None
        if "error" not in account_response.payload:
            account_identity = parse_account_read_result(
                account_response.payload,
                observed_at=observed_at,
            )

        rate_limits: list[RateLimitSnapshot] = []
        if "error" not in rate_limit_response.payload:
            rate_limits = parse_rate_limits_result(
                alias=active_alias,
                response=rate_limit_response.payload,
                observed_via=UsageSource.RPC,
                observed_at=observed_at,
            )

        return RpcPollResult(
            account_identity=account_identity,
            rate_limits=rate_limits,
            thread_runtime=thread_runtime,
            token_usage=token_usage,
            hard_limit_exceeded=hard_limit_exceeded,
        )

    def _ensure_client(self) -> CodexRpcClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _allocate_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id


class CodexCliPtySource:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._env = None if env is None else dict(env)

    def probe(self, *, alias: str, observed_at: str) -> RateLimitSnapshot | None:
        try:
            completed = subprocess.run(
                ["codex"],
                input="/status\n/exit\n",
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                env=self._env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        text = "\n".join(chunk for chunk in (completed.stdout, completed.stderr) if chunk)
        try:
            parsed = parse_status_output(text)
        except ValueError:
            return None

        return RateLimitSnapshot(
            alias=alias,
            limit_id="cli-status",
            limit_name="CLI /status",
            observed_via=UsageSource.PTY,
            plan_type=None,
            primary_window=RateLimitWindow(
                used_percent=parsed.primary_used_percent,
                resets_at=None,
                window_duration_mins=300,
            ),
            secondary_window=RateLimitWindow(
                used_percent=parsed.secondary_used_percent,
                resets_at=None,
                window_duration_mins=10080,
            ),
            credits_has_credits=None,
            credits_unlimited=None,
            credits_balance=parsed.credits_balance,
            observed_at=observed_at,
        )


class DaemonRuntime:
    def __init__(
        self,
        store: AutomationStore,
        manager: CodexSwitchManager,
        *,
        rpc_source: RpcSource | None = None,
        pty_source: PtySource | None = None,
        codex_controller: CodexController | None = None,
        can_mutate_auth=None,
        poll_interval_seconds: float,
        soft_switch_threshold: float = _SOFT_SWITCH_THRESHOLD,
        fresh_telemetry_seconds: int = _FRESH_TELEMETRY_SECONDS,
    ) -> None:
        self._store = store
        self._manager = manager
        self._rpc_source = rpc_source if rpc_source is not None else NullRpcSource()
        self._pty_source = pty_source if pty_source is not None else NullPtySource()
        self._codex_controller = (
            codex_controller if codex_controller is not None else SubprocessCodexController()
        )
        self._can_mutate_auth = can_mutate_auth if can_mutate_auth is not None else _can_mutate_auth
        self._poll_interval_seconds = poll_interval_seconds
        self._soft_switch_threshold = soft_switch_threshold
        self._fresh_telemetry_seconds = fresh_telemetry_seconds
        self._stop_event = threading.Event()
        self._initialized = False

    def request_stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> None:
        self._initialize_once()
        alias_entries, active_alias = self._manager.list_aliases()
        aliases = [entry.alias for entry in alias_entries]
        self._store.reconcile_aliases(aliases)

        pending = self._store.get_handoff_state()
        if pending is not None and pending.phase in {HandoffPhase.pending_switch, HandoffPhase.pending_resume}:
            self._recover_handoff(pending)
            return

        if active_alias is None:
            return

        self._refresh_backup_aliases_if_safe(aliases=aliases, active_alias=active_alias)

        try:
            rpc_result = self._rpc_source.poll(active_alias=active_alias)
        except AutomationSourceUnavailableError:
            observed_at = _utc_now()
            snapshot = self._pty_source.probe(alias=active_alias, observed_at=observed_at)
            if snapshot is not None:
                self._store.upsert_rate_limit(snapshot)
            return

        self._persist_rpc_poll(active_alias=active_alias, poll=rpc_result)

        if rpc_result.thread_runtime is None:
            return

        pending = self._store.get_handoff_state()
        if pending is not None and pending.phase in {
            HandoffPhase.pending_idle_checkpoint,
            HandoffPhase.pending_stop,
        }:
            if rpc_result.thread_runtime.safe_to_switch:
                self._execute_handoff(
                    thread_id=pending.thread_id,
                    source_alias=pending.source_alias,
                    target_alias=pending.target_alias,
                    trigger_type=pending.reason,
                )
            return

        active_snapshot = _latest_snapshot_for_alias(active_alias, rpc_result.rate_limits)
        if active_snapshot is None:
            return

        target_alias = self._choose_fresh_target_alias(active_alias=active_alias, aliases=aliases)
        if target_alias is None:
            return

        trigger_type: str | None = None
        next_phase: HandoffPhase | None = None
        if rpc_result.hard_limit_exceeded:
            trigger_type = "hard_trigger"
            next_phase = HandoffPhase.pending_stop
        elif should_trigger_soft_switch(active_snapshot, self._soft_switch_threshold):
            trigger_type = "soft_trigger"
            next_phase = HandoffPhase.pending_idle_checkpoint

        if trigger_type is None or next_phase is None:
            return

        if rpc_result.thread_runtime.safe_to_switch:
            self._execute_handoff(
                thread_id=rpc_result.thread_runtime.thread_id,
                source_alias=active_alias,
                target_alias=target_alias,
                trigger_type=trigger_type,
            )
            return

        self._store.set_handoff_state(
            thread_id=rpc_result.thread_runtime.thread_id,
            source_alias=active_alias,
            target_alias=target_alias,
            phase=next_phase,
            reason=trigger_type,
            updated_at=_utc_now(),
        )

    def run_forever(self) -> None:
        self._initialize_once()
        while not self._stop_event.wait(self._poll_interval_seconds):
            self.run_once()

    def _initialize_once(self) -> None:
        if self._initialized:
            return
        self._store.initialize()
        self._initialized = True

    def _persist_rpc_poll(self, *, active_alias: str, poll: RpcPollResult) -> None:
        if poll.account_identity is not None:
            self._store.record_alias_observation(
                alias=active_alias,
                account_email=poll.account_identity.email,
                account_plan_type=poll.account_identity.plan_type,
                account_fingerprint=poll.account_identity.fingerprint,
                observed_at=poll.account_identity.observed_at,
            )

        for snapshot in poll.rate_limits:
            self._store.upsert_rate_limit(snapshot)

        if poll.thread_runtime is not None:
            self._store.upsert_thread_runtime(
                thread_id=poll.thread_runtime.thread_id,
                cwd=poll.thread_runtime.cwd,
                model=poll.thread_runtime.model,
                current_alias=poll.thread_runtime.current_alias,
                last_turn_id=poll.thread_runtime.last_turn_id,
                last_known_status=poll.thread_runtime.last_known_status,
                safe_to_switch=poll.thread_runtime.safe_to_switch,
                last_total_tokens=poll.thread_runtime.last_total_tokens,
                last_seen_at=poll.thread_runtime.last_seen_at,
            )

        for usage in poll.token_usage:
            self._store.append_thread_turn_usage(
                thread_id=usage.thread_id,
                turn_id=usage.turn_id,
                last_input_tokens=usage.last_input_tokens,
                last_cached_input_tokens=usage.last_cached_input_tokens,
                last_output_tokens=usage.last_output_tokens,
                last_reasoning_output_tokens=usage.last_reasoning_output_tokens,
                last_total_tokens=usage.last_total_tokens,
                total_input_tokens=usage.total_input_tokens,
                total_cached_input_tokens=usage.total_cached_input_tokens,
                total_output_tokens=usage.total_output_tokens,
                total_reasoning_output_tokens=usage.total_reasoning_output_tokens,
                total_tokens=usage.total_tokens,
                observed_at=usage.observed_at,
            )

    def _choose_fresh_target_alias(self, *, active_alias: str, aliases: list[str]) -> str | None:
        candidate_snapshots: list[RateLimitSnapshot] = []
        for alias in aliases:
            if alias == active_alias:
                continue
            latest = self._store.latest_rate_limit_for_alias(alias)
            if latest is None or not _is_fresh_record(latest, max_age_seconds=self._fresh_telemetry_seconds):
                continue
            candidate_snapshots.append(_rate_limit_record_to_snapshot(latest))
        return choose_target_alias(
            active_alias=active_alias,
            candidates=candidate_snapshots,
            threshold=self._soft_switch_threshold,
        )

    def _refresh_backup_aliases_if_safe(self, *, aliases: list[str], active_alias: str) -> None:
        if not self._can_mutate_auth():
            return

        current_alias = active_alias
        try:
            for alias in aliases:
                if alias == active_alias:
                    continue
                latest = self._store.latest_rate_limit_for_alias(alias)
                if latest is not None and _is_fresh_record(
                    latest,
                    max_age_seconds=self._fresh_telemetry_seconds,
                ):
                    continue

                if current_alias != alias:
                    self._manager.use(alias)
                    current_alias = alias

                try:
                    poll = self._rpc_source.poll(active_alias=alias)
                except AutomationSourceUnavailableError:
                    observed_at = _utc_now()
                    snapshot = self._pty_source.probe(alias=alias, observed_at=observed_at)
                    if snapshot is not None:
                        self._store.upsert_rate_limit(snapshot)
                else:
                    self._persist_rpc_poll(active_alias=alias, poll=poll)
        finally:
            if current_alias != active_alias:
                self._manager.use(active_alias)

    def _recover_handoff(self, handoff: HandoffStateRecord) -> None:
        if handoff.phase not in {HandoffPhase.pending_switch, HandoffPhase.pending_resume}:
            return
        self._execute_handoff(
            thread_id=handoff.thread_id,
            source_alias=handoff.source_alias,
            target_alias=handoff.target_alias,
            trigger_type=handoff.reason,
        )

    def _execute_handoff(
        self,
        *,
        thread_id: str,
        source_alias: str | None,
        target_alias: str | None,
        trigger_type: str | None,
    ) -> None:
        if target_alias is None:
            return

        requested_at = _utc_now()
        self._store.set_handoff_state(
            thread_id=thread_id,
            source_alias=source_alias,
            target_alias=target_alias,
            phase=HandoffPhase.pending_switch,
            reason=trigger_type,
            updated_at=requested_at,
        )

        switched_at: str | None = None
        switched_alias = False
        try:
            self._codex_controller.stop()
            self._manager.use(target_alias)
            switched_alias = True
            switched_at = _utc_now()
            self._store.set_handoff_state(
                thread_id=thread_id,
                source_alias=source_alias,
                target_alias=target_alias,
                phase=HandoffPhase.pending_resume,
                reason=trigger_type,
                updated_at=switched_at,
            )
            self._codex_controller.resume(thread_id)
        except Exception as exc:
            if switched_alias:
                self._store.set_handoff_state(
                    thread_id=thread_id,
                    source_alias=source_alias,
                    target_alias=target_alias,
                    phase=HandoffPhase.failed_resume,
                    reason=trigger_type,
                    updated_at=_utc_now(),
                )
                self._store.append_switch_event(
                    thread_id=thread_id,
                    from_alias=source_alias,
                    to_alias=target_alias,
                    trigger_type=trigger_type,
                    trigger_limit_id=None,
                    trigger_used_percent=None,
                    requested_at=requested_at,
                    switched_at=switched_at,
                    resumed_at=None,
                    result="failed_resume",
                    failure_message=str(exc),
                )
            else:
                self._store.append_switch_event(
                    thread_id=thread_id,
                    from_alias=source_alias,
                    to_alias=target_alias,
                    trigger_type=trigger_type,
                    trigger_limit_id=None,
                    trigger_used_percent=None,
                    requested_at=requested_at,
                    switched_at=None,
                    resumed_at=None,
                    result="failed_switch",
                    failure_message=str(exc),
                )
            return

        resumed_at = _utc_now()
        self._store.clear_handoff_state()
        self._store.append_switch_event(
            thread_id=thread_id,
            from_alias=source_alias,
            to_alias=target_alias,
            trigger_type=trigger_type,
            trigger_limit_id=None,
            trigger_used_percent=None,
            requested_at=requested_at,
            switched_at=switched_at,
            resumed_at=resumed_at,
            result="success",
            failure_message=None,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-switchd")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--home", default=None)
    run_parser.add_argument("--poll-interval", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command != "run":
        parser.error(f"unknown command: {args.command}")

    home = Path(args.home) if args.home is not None else None
    paths = resolve_paths(home=home)
    accounts = AccountStore(paths.accounts_dir)
    state = StateStore(paths.state_file)
    store = AutomationStore(paths.automation_db_file)
    manager = CodexSwitchManager(
        paths=paths,
        accounts=accounts,
        state=state,
        ensure_safe_to_mutate=ensure_codex_not_running,
        login_runner=lambda _mode: None,
        automation=store,
    )
    runtime = DaemonRuntime(
        store=store,
        manager=manager,
        rpc_source=AppServerRpcSource(),
        pty_source=CodexCliPtySource(),
        can_mutate_auth=_can_mutate_auth,
        poll_interval_seconds=args.poll_interval,
    )

    def _handle_signal(_signum: int, _frame) -> None:
        runtime.request_stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    runtime.run_forever()
    return 0


def _latest_snapshot_for_alias(alias: str, snapshots: list[RateLimitSnapshot]) -> RateLimitSnapshot | None:
    latest: RateLimitSnapshot | None = None
    for snapshot in snapshots:
        if snapshot.alias != alias:
            continue
        if latest is None or snapshot.observed_at > latest.observed_at:
            latest = snapshot
    return latest


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


def _is_fresh_record(record: RateLimitRecord, *, max_age_seconds: int) -> bool:
    try:
        observed_at = _parse_iso(record.observed_at)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - observed_at).total_seconds() <= max_age_seconds


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _can_mutate_auth() -> bool:
    try:
        ensure_codex_not_running()
    except Exception:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())

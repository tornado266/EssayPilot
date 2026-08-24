"""Reliable seven-day browser recovery for Supabase passwordless sessions."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable, MutableMapping

import streamlit as st

from src.cloud_store import (
    CloudSessionExpiredError,
    CloudStoreError,
    CloudUser,
    SupabaseStore,
)


AUTH_STORAGE_KEY = "essaypilot_auth_refresh_v1"
AUTH_BROWSER_COMMAND_KEY = "auth_browser_command"
AUTH_BROWSER_VERSION_KEY = "auth_browser_version"
AUTH_USER_VERSION_KEY = "auth_user_version"
AUTH_LOGOUT_PENDING_KEY = "auth_logout_pending"
AUTH_RECOVERY_STATE_KEY = "auth_recovery_state"
AUTH_RETENTION_SECONDS = 7 * 24 * 60 * 60
ACCESS_REFRESH_SKEW_SECONDS = 5 * 60
_MAX_REFRESH_TOKEN_LENGTH = 8192

_AUTH_COMPONENT = st.components.v2.component(
    "essaypilot_auth_session",
    html='<span hidden aria-hidden="true"></span>',
    js=f"""
    export default function({{ data, setStateValue }}) {{
      const key = {AUTH_STORAGE_KEY!r};
      const retentionSeconds = {AUTH_RETENTION_SECONDS};
      const maxTokenLength = {_MAX_REFRESH_TOKEN_LENGTH};
      const action = data?.action || "read";
      const commandId = String(data?.command_id || "");

      const parseRecord = (raw) => {{
        if (!raw) return null;
        const stored = JSON.parse(raw);
        const refreshToken = String(stored?.refresh_token || "");
        const savedAt = Number(stored?.saved_at || 0);
        const version = Number(stored?.version || 0);
        if (
          !refreshToken || refreshToken.length > maxTokenLength ||
          !Number.isFinite(savedAt) || savedAt <= 0 ||
          !Number.isFinite(version) || version < 0
        ) return null;
        return {{ refresh_token: refreshToken, saved_at: savedAt, version }};
      }};

      const currentRecord = () => parseRecord(window.localStorage.getItem(key));
      const emitRecord = (record, status, extra = {{}}) => setStateValue(
        "auth_session",
        {{ status, ...record, ...extra }}
      );

      try {{
        if (action === "write") {{
          const requested = {{
            refresh_token: String(data?.refresh_token || ""),
            saved_at: Number(data?.saved_at || 0),
            version: Number(data?.version || 0),
          }};
          const now = Date.now() / 1000;
          const existing = currentRecord();
          const expectedVersion = Number(data?.expected_version || 0);
          if (
            !requested.refresh_token || requested.refresh_token.length > maxTokenLength ||
            !Number.isFinite(requested.saved_at) || requested.saved_at <= 0 ||
            requested.saved_at > now + 300 || now - requested.saved_at >= retentionSeconds ||
            !Number.isFinite(requested.version) || requested.version <= 0
          ) {{
            emitRecord(existing || {{}}, "rejected", {{ command_id: commandId }});
            return;
          }}
          if (
            existing && existing.version === requested.version &&
            existing.refresh_token === requested.refresh_token
          ) {{
            emitRecord(existing, "written", {{ command_id: commandId }});
            return;
          }}
          if (!existing && expectedVersion > 0) {{
            setStateValue("auth_session", {{
              status: "skipped_cleared",
              command_id: commandId,
              version: expectedVersion,
            }});
            return;
          }}
          if (existing && expectedVersion > 0 && existing.version !== expectedVersion) {{
            emitRecord(existing, "skipped_newer", {{ command_id: commandId }});
            return;
          }}
          if (
            existing && (
              existing.version > requested.version ||
              (
                existing.version === requested.version &&
                existing.refresh_token !== requested.refresh_token &&
                existing.saved_at >= requested.saved_at
              )
            )
          ) {{
            emitRecord(existing, "skipped_newer", {{ command_id: commandId }});
            return;
          }}
          window.localStorage.setItem(key, JSON.stringify(requested));
          emitRecord(requested, "written", {{ command_id: commandId }});
          return;
        }}

        if (action === "clear") {{
          const existing = currentRecord();
          const expectedVersion = Number(data?.expected_version || 0);
          if (existing && existing.version !== expectedVersion) {{
            emitRecord(existing, "skipped_newer", {{ command_id: commandId }});
            return;
          }}
          window.localStorage.removeItem(key);
          setStateValue("auth_session", {{
            status: "cleared",
            command_id: commandId,
            version: expectedVersion,
          }});
          return;
        }}

        const raw = window.localStorage.getItem(key);
        if (!raw) {{
          setStateValue("auth_session", {{ status: "empty" }});
        }} else {{
          const stored = currentRecord();
          const now = Date.now() / 1000;
          if (!stored || stored.saved_at > now + 300 || now - stored.saved_at >= retentionSeconds) {{
            const expiredVersion = Number(stored?.version || 0);
            window.localStorage.removeItem(key);
            setStateValue("auth_session", {{ status: "expired", version: expiredVersion }});
          }} else {{
            emitRecord(stored, "loaded", {{ source: "read" }});
          }}
        }}

        const wake = () => setStateValue("auth_wake", Date.now());
        const wakeWhenVisible = () => {{
          if (document.visibilityState === "visible") wake();
        }};
        const storageChanged = (event) => {{
          if (event.key !== key) return;
          try {{
            const newer = parseRecord(event.newValue);
            if (newer) {{
              emitRecord(newer, "loaded", {{ source: "storage" }});
            }} else {{
              const previous = parseRecord(event.oldValue);
              setStateValue("auth_session", {{
                status: "storage_cleared",
                source: "storage",
                version: Number(previous?.version || 0),
              }});
            }}
          }} catch (_) {{
            setStateValue("auth_session", {{ status: "unavailable" }});
          }}
        }};
        window.addEventListener("online", wake);
        window.addEventListener("storage", storageChanged);
        document.addEventListener("visibilitychange", wakeWhenVisible);
        return () => {{
          window.removeEventListener("online", wake);
          window.removeEventListener("storage", storageChanged);
          document.removeEventListener("visibilitychange", wakeWhenVisible);
        }};
      }} catch (_) {{
        setStateValue("auth_session", {{ status: "unavailable" }});
      }}
    }}
    """,
)


@dataclass(frozen=True)
class PersistedRefreshSession:
    refresh_token: str
    saved_at: float
    version: int = 0


@dataclass(frozen=True)
class BrowserAck:
    status: str
    command: dict[str, Any]
    record: PersistedRefreshSession | None = None


@dataclass(frozen=True)
class AuthResolution:
    user: CloudUser | None
    persist_refresh: bool = False
    clear_persisted: bool = False
    clear_expected_version: int = 0
    state_changed: bool = False
    recovery_pending: bool = False
    browser_version: int = 0


def cloud_user_to_state(user: CloudUser) -> dict[str, Any]:
    return asdict(user)


def cloud_user_from_state(value: object) -> CloudUser | None:
    if not isinstance(value, dict) or not value.get("access_token") or not value.get("id"):
        return None
    try:
        expires_at = max(0, int(float(value.get("expires_at") or 0)))
        return CloudUser(
            id=str(value.get("id", "")),
            email=str(value.get("email", "")),
            access_token=str(value.get("access_token", "")),
            refresh_token=str(value.get("refresh_token", "")),
            expires_at=expires_at,
            expires_in=max(0, int(float(value.get("expires_in") or 0))),
            expiry_source=str(
                value.get("expiry_source") or ("expires_at" if expires_at else "unknown")
            ),
        )
    except (TypeError, ValueError):
        return None


def _next_version(state: MutableMapping[str, Any], now: float) -> int:
    known = max(
        int(state.get(AUTH_BROWSER_VERSION_KEY) or 0),
        int(state.get(AUTH_USER_VERSION_KEY) or 0),
    )
    return max(known + 1, int(now * 1_000_000))


def queue_refresh_token_write(
    state: MutableMapping[str, Any],
    refresh_token: str,
    *,
    now: float | None = None,
) -> int:
    if not refresh_token:
        queue_refresh_token_clear(state)
        return 0
    saved_at = float(time.time() if now is None else now)
    version = _next_version(state, saved_at)
    state[AUTH_BROWSER_COMMAND_KEY] = {
        "action": "write",
        "command_id": str(uuid.uuid4()),
        "refresh_token": refresh_token,
        "saved_at": saved_at,
        "version": version,
        "expected_version": int(state.get(AUTH_BROWSER_VERSION_KEY) or 0),
    }
    state[AUTH_USER_VERSION_KEY] = version
    return version


def queue_refresh_token_clear(
    state: MutableMapping[str, Any],
    *,
    expected_version: int | None = None,
) -> str:
    version = int(
        state.get(AUTH_BROWSER_VERSION_KEY, 0)
        if expected_version is None
        else expected_version
    )
    command_id = str(uuid.uuid4())
    state[AUTH_BROWSER_COMMAND_KEY] = {
        "action": "clear",
        "command_id": command_id,
        "expected_version": version,
    }
    return command_id


def take_browser_command(state: MutableMapping[str, Any]) -> dict[str, Any]:
    """Return the pending command without deleting it until a matching ACK arrives."""
    command = state.get(AUTH_BROWSER_COMMAND_KEY)
    if isinstance(command, dict) and command.get("action") in {"write", "clear"}:
        return dict(command)
    return {"action": "read"}


def browser_refresh_session(command: dict[str, Any]) -> dict[str, Any]:
    result = _AUTH_COMPONENT(
        data=command,
        key="essaypilot_auth_session",
        default={"auth_session": {"status": "loading"}, "auth_wake": 0},
        on_auth_session_change=lambda: None,
        on_auth_wake_change=lambda: None,
    )
    value = getattr(result, "auth_session", {})
    return value if isinstance(value, dict) else {}


def parse_persisted_refresh_session(
    value: object,
    *,
    now: float | None = None,
) -> tuple[PersistedRefreshSession | None, bool]:
    if not isinstance(value, dict):
        return None, False
    status = str(value.get("status", ""))
    if status in {"", "loading", "empty", "cleared", "storage_cleared", "unavailable"}:
        return None, False
    if status == "expired":
        return None, True
    if status not in {"loaded", "written", "skipped_newer"}:
        return None, False
    token = str(value.get("refresh_token", ""))
    try:
        saved_at = float(value.get("saved_at") or 0)
        version = max(0, int(float(value.get("version") or 0)))
    except (TypeError, ValueError):
        return None, True
    current_time = time.time() if now is None else now
    invalid = (
        not token
        or len(token) > _MAX_REFRESH_TOKEN_LENGTH
        or saved_at <= 0
        or saved_at > current_time + ACCESS_REFRESH_SKEW_SECONDS
        or current_time - saved_at >= AUTH_RETENTION_SECONDS
    )
    if invalid:
        return None, True
    return PersistedRefreshSession(token, saved_at, version), False


def acknowledge_browser_command(
    state: MutableMapping[str, Any],
    value: object,
    *,
    now: float | None = None,
) -> BrowserAck | None:
    pending = state.get(AUTH_BROWSER_COMMAND_KEY)
    if not isinstance(pending, dict) or not isinstance(value, dict):
        return None
    if str(value.get("command_id") or "") != str(pending.get("command_id") or ""):
        return None
    status = str(value.get("status") or "")
    allowed = {
        "write": {"written", "skipped_newer", "skipped_cleared"},
        "clear": {"cleared", "skipped_newer"},
    }
    if status not in allowed.get(str(pending.get("action")), set()):
        return None
    record, _ = parse_persisted_refresh_session(value, now=now)
    state.pop(AUTH_BROWSER_COMMAND_KEY, None)
    if record is not None:
        state[AUTH_BROWSER_VERSION_KEY] = max(
            int(state.get(AUTH_BROWSER_VERSION_KEY) or 0), record.version
        )
    return BrowserAck(status, dict(pending), record)


def browser_signaled_logout(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and (
            (
                value.get("status") == "storage_cleared"
                and value.get("source") == "storage"
            )
            or value.get("status") == "skipped_cleared"
        )
    )


def begin_logout(
    state: MutableMapping[str, Any],
    *,
    reason: str,
    expected_version: int | None = None,
) -> None:
    if isinstance(state.get(AUTH_LOGOUT_PENDING_KEY), dict):
        return
    state[AUTH_LOGOUT_PENDING_KEY] = {"reason": reason, "clear_retries": 0}
    queue_refresh_token_clear(state, expected_version=expected_version)


def start_logout_with_remote_best_effort(
    state: MutableMapping[str, Any],
    user: CloudUser | None,
    remote_logout: Callable[[CloudUser], None],
    *,
    expected_version: int,
) -> None:
    """Remote revocation may fail, but the ACK-backed local logout always starts."""
    if user is not None:
        try:
            remote_logout(user)
        except CloudStoreError:
            pass
    begin_logout(
        state,
        reason="user",
        expected_version=expected_version,
    )


def apply_browser_command_to_record(
    record: PersistedRefreshSession | None,
    command: dict[str, Any],
) -> tuple[PersistedRefreshSession | None, dict[str, Any]]:
    """Pure mirror of the component's idempotent write/clear conflict rules."""
    action = str(command.get("action") or "")
    command_id = str(command.get("command_id") or "")
    if action == "write":
        requested = PersistedRefreshSession(
            str(command.get("refresh_token") or ""),
            float(command.get("saved_at") or 0),
            int(command.get("version") or 0),
        )
        expected = int(command.get("expected_version") or 0)
        if (
            record is not None
            and record.version == requested.version
            and record.refresh_token == requested.refresh_token
        ):
            return record, {"status": "written", "command_id": command_id, **asdict(record)}
        if record is None and expected > 0:
            return None, {
                "status": "skipped_cleared",
                "command_id": command_id,
                "version": expected,
            }
        if record is not None and expected > 0 and record.version != expected:
            return record, {"status": "skipped_newer", "command_id": command_id, **asdict(record)}
        if record is not None and (
            record.version > requested.version
            or (
                record.version == requested.version
                and record.refresh_token != requested.refresh_token
                and record.saved_at >= requested.saved_at
            )
        ):
            return record, {"status": "skipped_newer", "command_id": command_id, **asdict(record)}
        return requested, {"status": "written", "command_id": command_id, **asdict(requested)}
    if action == "clear":
        expected = int(command.get("expected_version") or 0)
        if record is not None and record.version != expected:
            return record, {"status": "skipped_newer", "command_id": command_id, **asdict(record)}
        return None, {"status": "cleared", "command_id": command_id, "version": expected}
    return record, {"status": "loaded", **(asdict(record) if record else {})}


def access_token_needs_refresh(user: CloudUser, *, now: float | None = None) -> bool:
    if not user.expires_at or user.expiry_source == "unknown":
        return False
    current_time = time.time() if now is None else now
    return user.expires_at <= current_time + ACCESS_REFRESH_SKEW_SECONDS


def access_token_is_expired(user: CloudUser, *, now: float | None = None) -> bool:
    if not user.expires_at or user.expiry_source == "unknown":
        return False
    current_time = time.time() if now is None else now
    return user.expires_at <= current_time


def _refresh_candidate(store: SupabaseStore, token: str) -> tuple[CloudUser | None, str]:
    try:
        return store.refresh(token), "ok"
    except CloudSessionExpiredError:
        return None, "invalid"
    except CloudStoreError:
        return None, "temporary"


def resolve_auth_session(
    store: SupabaseStore,
    current_user: CloudUser | None,
    browser_value: object,
    *,
    current_version: int = 0,
    now: float | None = None,
) -> AuthResolution:
    """Resolve with at most one primary refresh and one newer-token fallback."""
    persisted, should_clear = parse_persisted_refresh_session(browser_value, now=now)
    if should_clear:
        return AuthResolution(
            current_user,
            clear_persisted=True,
            clear_expected_version=current_version,
            state_changed=current_user is None,
        )

    if current_user is not None:
        browser_is_newer = bool(
            persisted
            and persisted.refresh_token != current_user.refresh_token
            and (persisted.version > current_version or (persisted.version == current_version == 0))
        )
        if browser_is_newer and persisted is not None:
            refreshed, outcome = _refresh_candidate(store, persisted.refresh_token)
            if outcome == "ok" and refreshed is not None:
                return AuthResolution(
                    refreshed, persist_refresh=True, state_changed=True,
                    browser_version=persisted.version,
                )
            if outcome == "temporary":
                return AuthResolution(
                    current_user,
                    recovery_pending=access_token_is_expired(current_user, now=now),
                    browser_version=persisted.version,
                )
            return AuthResolution(
                None,
                clear_persisted=True,
                clear_expected_version=persisted.version,
                state_changed=True,
                browser_version=persisted.version,
            )

        if not access_token_needs_refresh(current_user, now=now):
            return AuthResolution(current_user, browser_version=current_version)
        if not current_user.refresh_token:
            if access_token_is_expired(current_user, now=now):
                return AuthResolution(None, state_changed=True)
            return AuthResolution(current_user)

        refreshed, outcome = _refresh_candidate(store, current_user.refresh_token)
        if outcome == "ok" and refreshed is not None:
            return AuthResolution(refreshed, persist_refresh=True, state_changed=True)
        if outcome == "temporary":
            return AuthResolution(
                current_user,
                recovery_pending=access_token_is_expired(current_user, now=now),
            )
        if persisted is not None and persisted.version > current_version:
            fallback, fallback_outcome = _refresh_candidate(store, persisted.refresh_token)
            if fallback_outcome == "ok" and fallback is not None:
                return AuthResolution(
                    fallback, persist_refresh=True, state_changed=True,
                    browser_version=persisted.version,
                )
            if fallback_outcome == "temporary":
                return AuthResolution(current_user, recovery_pending=True)
        return AuthResolution(
            None,
            clear_persisted=True,
            clear_expected_version=current_version,
            state_changed=True,
        )

    if persisted is None:
        return AuthResolution(None)
    restored, outcome = _refresh_candidate(store, persisted.refresh_token)
    if outcome == "ok" and restored is not None:
        return AuthResolution(
            restored, persist_refresh=True, state_changed=True,
            browser_version=persisted.version,
        )
    if outcome == "temporary":
        return AuthResolution(None, recovery_pending=True, browser_version=persisted.version)
    return AuthResolution(
        None,
        clear_persisted=True,
        clear_expected_version=persisted.version,
        state_changed=True,
        browser_version=persisted.version,
    )

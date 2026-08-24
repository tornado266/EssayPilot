"""Reliable seven-day browser recovery for Supabase passwordless sessions."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
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
AUTH_BROWSER_RECOVERY_KEY = "auth_browser_recovery"
AUTH_LISTENER_RERUN_KEY = "auth_listener_rerun"
AUTH_REQUEST_RERUN_KEY = "auth_request_rerun"
AUTH_BROWSER_READ_EPOCH_KEY = "auth_browser_read_epoch"
AUTH_PERSIST_WARNING_KEY = "auth_persist_warning"
AUTH_BROWSER_TOMBSTONES_KEY = "auth_browser_tombstones"
_MAX_BROWSER_TOMBSTONES = 32
AUTH_RETENTION_SECONDS = 7 * 24 * 60 * 60
ACCESS_REFRESH_SKEW_SECONDS = 5 * 60
_MAX_REFRESH_TOKEN_LENGTH = 8192

_AUTH_COMPONENT = st.components.v2.component(
    "essaypilot_auth_session",
    html='<span hidden aria-hidden="true"></span>',
    js=f"""
    export default function({{ data, setStateValue }}) {{
      const key = {AUTH_STORAGE_KEY!r};
      const tombstoneKey = key + "_write_tombstones";
      const maxTombstones = {_MAX_BROWSER_TOMBSTONES};
      const retentionSeconds = {AUTH_RETENTION_SECONDS};
      const maxTokenLength = {_MAX_REFRESH_TOKEN_LENGTH};
      const action = data?.action || "read";
      const commandId = String(data?.command_id || "");
      const readEpoch = String(data?.read_epoch || "");

      const parseRecord = (raw) => {{
        if (!raw) return null;
        try {{
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
        }} catch (_) {{
          return null;
        }}
      }};

      const currentRecord = () => parseRecord(window.localStorage.getItem(key));
      const parseTombstones = (raw) => {{
        const now = Date.now() / 1000;
        let stored = [];
        try {{
          stored = JSON.parse(raw || "[]");
        }} catch (_) {{
          stored = [];
        }}
        if (!Array.isArray(stored)) stored = [];
        const byVersion = new Map();
        for (const item of stored) {{
          const version = Number(item?.version || 0);
          const expiresAt = Number(item?.expires_at || 0);
          if (
            !Number.isSafeInteger(version) || version <= 0 ||
            !Number.isFinite(expiresAt) || expiresAt <= now
          ) continue;
          const boundedExpiry = Math.min(expiresAt, now + retentionSeconds);
          byVersion.set(
            version, Math.max(byVersion.get(version) || 0, boundedExpiry)
          );
        }}
        return Array.from(byVersion, ([version, expires_at]) => ({{
          version, expires_at
        }}))
          .sort((left, right) => left.expires_at - right.expires_at)
          .slice(-maxTombstones);
      }};
      const currentTombstones = () => {{
        const raw = window.localStorage.getItem(tombstoneKey);
        const active = parseTombstones(raw);
        const serialized = JSON.stringify(active);
        if (active.length > 0 && raw !== serialized) {{
          window.localStorage.setItem(tombstoneKey, serialized);
        }} else if (active.length === 0 && raw) {{
          window.localStorage.removeItem(tombstoneKey);
        }}
        return active;
      }};
      const addTombstones = (versions) => {{
        const now = Date.now() / 1000;
        const byVersion = new Map(
          currentTombstones().map((item) => [item.version, item.expires_at])
        );
        for (const rawVersion of versions) {{
          const version = Number(rawVersion);
          if (!Number.isSafeInteger(version) || version <= 0) continue;
          byVersion.set(version, now + retentionSeconds);
        }}
        const active = Array.from(byVersion, ([version, expires_at]) => ({{
          version, expires_at
        }}))
          .sort((left, right) => left.expires_at - right.expires_at)
          .slice(-maxTombstones);
        window.localStorage.setItem(tombstoneKey, JSON.stringify(active));
        return active.map((item) => item.version);
      }};
      const currentTombstoneVersions = () =>
        currentTombstones().map((item) => item.version);
      const emitRecord = (record, status, extra = {{}}) => setStateValue(
        "auth_session",
        {{ status, ...record, ...extra }}
      );
      const supersededWriteVersions = new Set(
        (Array.isArray(data?.superseded_write_versions)
          ? data.superseded_write_versions : [])
          .map((version) => Number(version))
          .filter((version) => Number.isFinite(version) && version > 0)
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
          const tombstoneVersions = currentTombstoneVersions();
          const tombstoneExtra = {{ tombstone_versions: tombstoneVersions }};
          if (
            !requested.refresh_token || requested.refresh_token.length > maxTokenLength ||
            !Number.isFinite(requested.saved_at) || requested.saved_at <= 0 ||
            requested.saved_at > now + 300 || now - requested.saved_at >= retentionSeconds ||
            !Number.isFinite(requested.version) || requested.version <= 0
          ) {{
            emitRecord(existing || {{}}, "rejected", {{
              command_id: commandId, ...tombstoneExtra
            }});
            return;
          }}
          if (tombstoneVersions.includes(requested.version)) {{
            if (existing) {{
              emitRecord(existing, "skipped_newer", {{
                command_id: commandId,
                ...tombstoneExtra,
              }});
            }} else {{
              setStateValue("auth_session", {{
                status: "skipped_cleared",
                command_id: commandId,
                version: requested.version,
                ...tombstoneExtra,
              }});
            }}
            return;
          }}
          if (
            existing && existing.version === requested.version &&
            existing.refresh_token === requested.refresh_token
          ) {{
            emitRecord(existing, "written", {{
              command_id: commandId, ...tombstoneExtra
            }});
            return;
          }}
          if (
            existing && expectedVersion > 0 &&
            existing.version !== expectedVersion &&
            !supersededWriteVersions.has(existing.version)
          ) {{
            emitRecord(existing, "skipped_newer", {{
              command_id: commandId, ...tombstoneExtra
            }});
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
            emitRecord(existing, "skipped_newer", {{
              command_id: commandId, ...tombstoneExtra
            }});
            return;
          }}
          window.localStorage.setItem(key, JSON.stringify(requested));
          emitRecord(requested, "written", {{
            command_id: commandId, ...tombstoneExtra
          }});
          return;
        }}

        if (action === "clear") {{
          const existing = currentRecord();
          const expectedVersion = Number(data?.expected_version || 0);
          const requestedTombstones = Array.isArray(data?.tombstone_versions)
            ? data.tombstone_versions : [];
          const tombstoneVersions = addTombstones([
            expectedVersion,
            ...supersededWriteVersions,
            ...requestedTombstones,
          ]);
          if (
            existing && existing.version !== expectedVersion &&
            !supersededWriteVersions.has(existing.version)
          ) {{
            emitRecord(existing, "skipped_newer", {{
              command_id: commandId,
              tombstone_versions: tombstoneVersions,
            }});
            return;
          }}
          window.localStorage.removeItem(key);
          setStateValue("auth_session", {{
            status: "cleared",
            command_id: commandId,
            version: expectedVersion,
            tombstone_versions: tombstoneVersions,
          }});
          return;
        }}

        const tombstoneVersions = currentTombstoneVersions();
        const raw = window.localStorage.getItem(key);
        if (!raw) {{
          setStateValue("auth_session", {{
            status: "empty", source: "read", read_epoch: readEpoch,
            tombstone_versions: tombstoneVersions,
          }});
        }} else {{
          const stored = currentRecord();
          const now = Date.now() / 1000;
          if (!stored) {{
            window.localStorage.removeItem(key);
            setStateValue("auth_session", {{
              status: "invalid", source: "read", read_epoch: readEpoch, version: 0,
              tombstone_versions: tombstoneVersions,
            }});
          }} else if (stored.saved_at > now + 300 || now - stored.saved_at >= retentionSeconds) {{
            const expiredVersion = Number(stored?.version || 0);
            window.localStorage.removeItem(key);
            setStateValue("auth_session", {{
              status: "expired", source: "read",
              read_epoch: readEpoch, version: expiredVersion,
              tombstone_versions: tombstoneVersions,
            }});
          }} else {{
            emitRecord(stored, "loaded", {{
              source: "read", read_epoch: readEpoch,
              tombstone_versions: tombstoneVersions,
            }});
          }}
        }}

        const wake = () => setStateValue("auth_wake", Date.now());
        const wakeWhenVisible = () => {{
          if (document.visibilityState === "visible") wake();
        }};
        const storageChanged = (event) => {{
          if (event.key === tombstoneKey) {{ wake(); return; }}
          if (event.key !== key) return;
          try {{
            const newer = parseRecord(event.newValue);
            if (newer) {{
              emitRecord(newer, "loaded", {{
                source: "storage", read_epoch: readEpoch,
                tombstone_versions: currentTombstoneVersions(),
              }});
            }} else {{
              const previous = parseRecord(event.oldValue);
              if (event.newValue) window.localStorage.removeItem(key);
              setStateValue("auth_session", {{
                status: "storage_cleared",
                source: "storage",
                read_epoch: readEpoch,
                version: Number(previous?.version || 0),
                tombstone_versions: currentTombstoneVersions(),
              }});
            }}
          }} catch (_) {{
            setStateValue("auth_session", {{
              status: "unavailable", read_epoch: readEpoch
            }});
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
        setStateValue("auth_session", {{
          status: "unavailable", read_epoch: readEpoch
        }});
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


def _superseded_write_versions(
    command: object, *, include_current: bool = False
) -> list[int]:
    if not isinstance(command, dict):
        return []
    raw_versions = command.get("superseded_write_versions")
    values = (
        list(raw_versions)
        if isinstance(raw_versions, (list, tuple))
        else []
    )
    if include_current and command.get("action") == "write":
        values.append(command.get("version"))
    versions: list[int] = []
    for value in values:
        try:
            version = int(value)
        except (TypeError, ValueError):
            continue
        if version > 0 and version not in versions:
            versions.append(version)
    return versions[-8:]


def _normalize_tombstone_versions(values: object) -> list[int]:
    if not isinstance(values, (list, tuple, set)):
        return []
    versions: list[int] = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if (
            not numeric.is_integer()
            or numeric <= 0
            or numeric > 9_007_199_254_740_991
        ):
            continue
        version = int(numeric)
        if version not in versions:
            versions.append(version)
    return versions[-_MAX_BROWSER_TOMBSTONES:]


def _observe_browser_tombstones(
    state: MutableMapping[str, Any], value: object
) -> None:
    if not isinstance(value, dict) or "tombstone_versions" not in value:
        return
    versions = _normalize_tombstone_versions(value.get("tombstone_versions"))
    if versions:
        state[AUTH_BROWSER_TOMBSTONES_KEY] = versions
    else:
        state.pop(AUTH_BROWSER_TOMBSTONES_KEY, None)


def _next_version(state: MutableMapping[str, Any], now: float) -> int:
    tombstone_versions = _normalize_tombstone_versions(
        state.get(AUTH_BROWSER_TOMBSTONES_KEY)
    )
    known = max(
        int(state.get(AUTH_BROWSER_VERSION_KEY) or 0),
        int(state.get(AUTH_USER_VERSION_KEY) or 0),
        *tombstone_versions,
    )
    return max(known + 1, int(now * 1_000_000))


def queue_refresh_token_write(
    state: MutableMapping[str, Any],
    refresh_token: str,
    *,
    now: float | None = None,
    request_rerun: bool = False,
) -> int:
    pending = state.get(AUTH_BROWSER_COMMAND_KEY)
    if isinstance(state.get(AUTH_LOGOUT_PENDING_KEY), dict) or (
        isinstance(pending, dict) and pending.get("action") == "clear"
    ):
        return int(state.get(AUTH_USER_VERSION_KEY) or 0)
    if not refresh_token:
        queue_refresh_token_clear(state)
        return 0
    state.pop(AUTH_BROWSER_READ_EPOCH_KEY, None)
    saved_at = float(time.time() if now is None else now)
    version = _next_version(state, saved_at)
    superseded = _superseded_write_versions(pending, include_current=True)
    command = {
        "action": "write",
        "command_id": str(uuid.uuid4()),
        "refresh_token": refresh_token,
        "saved_at": saved_at,
        "version": version,
        "expected_version": int(state.get(AUTH_BROWSER_VERSION_KEY) or 0),
    }
    if superseded:
        command["superseded_write_versions"] = superseded
    state[AUTH_BROWSER_COMMAND_KEY] = command
    state[AUTH_USER_VERSION_KEY] = version
    if request_rerun:
        state[AUTH_REQUEST_RERUN_KEY] = True
    return version


def queue_refresh_token_clear(
    state: MutableMapping[str, Any],
    *,
    expected_version: int | None = None,
) -> str:
    pending = state.get(AUTH_BROWSER_COMMAND_KEY)
    version = int(
        state.get(AUTH_BROWSER_VERSION_KEY, 0)
        if expected_version is None
        else expected_version
    )
    state.pop(AUTH_BROWSER_READ_EPOCH_KEY, None)
    command_id = str(uuid.uuid4())
    superseded = _superseded_write_versions(pending, include_current=True)
    tombstone_versions = _normalize_tombstone_versions([
        version,
        int(state.get(AUTH_USER_VERSION_KEY) or 0),
        *superseded,
    ])
    command: dict[str, Any] = {
        "action": "clear",
        "command_id": command_id,
        "expected_version": version,
    }
    if tombstone_versions:
        command["tombstone_versions"] = tombstone_versions
    if superseded:
        command["superseded_write_versions"] = superseded
    state[AUTH_BROWSER_COMMAND_KEY] = command
    return command_id


def take_browser_command(state: MutableMapping[str, Any]) -> dict[str, Any]:
    """Return the pending command without deleting it until a matching ACK arrives."""
    command = state.get(AUTH_BROWSER_COMMAND_KEY)
    if isinstance(command, dict) and command.get("action") in {"write", "clear"}:
        return dict(command)
    read_epoch = str(state.get(AUTH_BROWSER_READ_EPOCH_KEY) or "")
    if not read_epoch:
        read_epoch = str(uuid.uuid4())
        state[AUTH_BROWSER_READ_EPOCH_KEY] = read_epoch
    return {"action": "read", "read_epoch": read_epoch}


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
    if status in {"expired", "invalid"}:
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
    _observe_browser_tombstones(state, value)
    pending = state.get(AUTH_BROWSER_COMMAND_KEY)
    if not isinstance(pending, dict) or not isinstance(value, dict):
        return None
    if str(value.get("command_id") or "") != str(pending.get("command_id") or ""):
        return None
    status = str(value.get("status") or "")
    allowed = {
        "write": {"written", "skipped_newer", "skipped_cleared", "rejected"},
        "clear": {"cleared", "skipped_newer"},
    }
    if status not in allowed.get(str(pending.get("action")), set()):
        return None
    record, _ = parse_persisted_refresh_session(value, now=now)
    state.pop(AUTH_BROWSER_COMMAND_KEY, None)
    if status == "rejected":
        state[AUTH_PERSIST_WARNING_KEY] = True
    elif status in {"written", "skipped_newer"}:
        state.pop(AUTH_PERSIST_WARNING_KEY, None)
    if record is not None:
        state[AUTH_BROWSER_VERSION_KEY] = max(
            int(state.get(AUTH_BROWSER_VERSION_KEY) or 0), record.version
        )
    return BrowserAck(status, dict(pending), record)


def browser_bootstrap_transition(
    state: MutableMapping[str, Any], value: object
) -> str:
    """Return a bounded browser bootstrap action: wait, retry, degraded, or ready."""
    status = str(value.get("status") or "") if isinstance(value, dict) else ""
    if status in {"", "loading"}:
        return "wait"
    if status == "unavailable":
        recovery = state.setdefault(AUTH_BROWSER_RECOVERY_KEY, {"attempts": 0})
        attempts = int(recovery.get("attempts") or 0)
        if attempts < 1:
            recovery["attempts"] = attempts + 1
            return "retry"
        recovery["degraded"] = True
        return "degraded"
    state.pop(AUTH_BROWSER_RECOVERY_KEY, None)
    return "ready"


def browser_ack_needs_listener_rerun(
    state: MutableMapping[str, Any], ack: BrowserAck | None
) -> bool:
    """Request exactly one rerun after an ACK whose component did not mount listeners."""
    if ack is None or ack.status not in {"written", "skipped_newer", "rejected"}:
        return False
    command_id = str(ack.command.get("command_id") or "")
    if not command_id or state.get(AUTH_LISTENER_RERUN_KEY) == command_id:
        return False
    state[AUTH_LISTENER_RERUN_KEY] = command_id
    state.pop(AUTH_REQUEST_RERUN_KEY, None)
    return True


def mark_browser_listener_stable(
    state: MutableMapping[str, Any], command: object, value: object
) -> bool:
    """Mark the read-mode component stable after it has produced a browser result."""
    if not isinstance(command, dict) or command.get("action") != "read":
        return False
    status = str(value.get("status") or "") if isinstance(value, dict) else ""
    if status in {"", "loading", "unavailable"}:
        return False
    read_epoch = str(command.get("read_epoch") or "")
    value_epoch = str(value.get("read_epoch") or "") if isinstance(value, dict) else ""
    if not read_epoch or value_epoch != read_epoch:
        return False
    state.pop(AUTH_LISTENER_RERUN_KEY, None)
    _observe_browser_tombstones(state, value)
    return True


def consume_auth_request_rerun(state: MutableMapping[str, Any]) -> bool:
    """Consume the one-shot rerun queued after a successful 401 recovery."""
    return bool(state.pop(AUTH_REQUEST_RERUN_KEY, False))


def browser_signaled_logout(
    value: object,
    *,
    has_current_user: bool = False,
    command: object = None,
    listener_stable: bool = False,
    has_pending_command: bool = False,
    persistence_failed: bool = False,
    ack: BrowserAck | None = None,
    current_version: int = 0,
) -> bool:
    if not isinstance(value, dict):
        return False
    status = str(value.get("status") or "")
    if status == "skipped_cleared":
        return bool(
            has_current_user
            and ack is not None
            and ack.status == "skipped_cleared"
            and ack.command.get("action") == "write"
            and str(value.get("command_id") or "")
            == str(ack.command.get("command_id") or "")
        )
    if (
        not has_current_user
        or status not in {"empty", "storage_cleared"}
        or has_pending_command
        or persistence_failed
        or not listener_stable
        or not isinstance(command, dict)
        or command.get("action") != "read"
    ):
        return False
    read_epoch = str(command.get("read_epoch") or "")
    if not read_epoch or read_epoch != str(value.get("read_epoch") or ""):
        return False
    if status == "empty":
        return value.get("source") == "read"
    if value.get("source") != "storage":
        return False
    try:
        cleared_version = int(value.get("version") or 0)
        user_version = max(0, int(current_version or 0))
    except (TypeError, ValueError):
        return False
    return cleared_version >= user_version


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
    remote_runner: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Remote revocation may fail, but the ACK-backed local logout always starts."""
    begin_logout(
        state,
        reason="user",
        expected_version=expected_version,
    )
    if user is not None:

        def revoke_current_session() -> None:
            try:
                remote_logout(user)
            except CloudStoreError:
                pass

        runner = remote_runner or (
            lambda task: threading.Thread(target=task, daemon=True).start()
        )
        try:
            runner(revoke_current_session)
        except (RuntimeError, OSError):
            pass


def apply_browser_command_to_record(
    record: PersistedRefreshSession | None,
    command: dict[str, Any],
    *,
    tombstone_versions: object = (),
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
        superseded = set(_superseded_write_versions(command))
        active_tombstones = _normalize_tombstone_versions(tombstone_versions)
        if requested.version in active_tombstones:
            if record is not None:
                return record, {
                    "status": "skipped_newer",
                    "command_id": command_id,
                    **asdict(record),
                    "tombstone_versions": active_tombstones,
                }
            return None, {
                "status": "skipped_cleared",
                "command_id": command_id,
                "version": requested.version,
                "tombstone_versions": active_tombstones,
            }
        if (
            record is not None
            and record.version == requested.version
            and record.refresh_token == requested.refresh_token
        ):
            return record, {"status": "written", "command_id": command_id, **asdict(record)}
        if (
            record is not None
            and expected > 0
            and record.version != expected
            and record.version not in superseded
        ):
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
        superseded_versions = _superseded_write_versions(command)
        superseded = set(superseded_versions)
        active_tombstones = _normalize_tombstone_versions([
            *_normalize_tombstone_versions(tombstone_versions),
            expected,
            *superseded_versions,
            *_normalize_tombstone_versions(command.get("tombstone_versions")),
        ])
        if (
            record is not None
            and record.version != expected
            and record.version not in superseded
        ):
            return record, {
                "status": "skipped_newer",
                "command_id": command_id,
                **asdict(record),
                "tombstone_versions": active_tombstones,
            }
        return None, {
            "status": "cleared",
            "command_id": command_id,
            "version": expected,
            "tombstone_versions": active_tombstones,
        }
    return record, {
        "status": "loaded",
        **(asdict(record) if record else {}),
        "tombstone_versions": _normalize_tombstone_versions(tombstone_versions),
    }


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
    force_browser_refresh: bool = False,
) -> AuthResolution:
    """Resolve with at most one primary refresh and one newer-token fallback."""
    persisted, should_clear = parse_persisted_refresh_session(browser_value, now=now)
    if force_browser_refresh and current_user is not None:
        candidate_token = (
            persisted.refresh_token if persisted is not None
            else current_user.refresh_token
        )
        candidate_version = (
            persisted.version if persisted is not None else current_version
        )
        if not candidate_token:
            return AuthResolution(
                None,
                clear_persisted=True,
                clear_expected_version=candidate_version,
                state_changed=True,
                browser_version=candidate_version,
            )
        synced_user = replace(current_user, refresh_token=candidate_token)
        refreshed, outcome = _refresh_candidate(store, candidate_token)
        if outcome == "ok" and refreshed is not None:
            return AuthResolution(
                refreshed,
                persist_refresh=True,
                state_changed=True,
                browser_version=candidate_version,
            )
        if outcome == "temporary":
            return AuthResolution(
                synced_user,
                state_changed=True,
                recovery_pending=True,
                browser_version=candidate_version,
            )
        return AuthResolution(
            None,
            clear_persisted=True,
            clear_expected_version=candidate_version,
            state_changed=True,
            browser_version=candidate_version,
        )

    if should_clear and current_user is not None and current_user.refresh_token:
        return AuthResolution(
            current_user,
            persist_refresh=True,
            state_changed=True,
            browser_version=current_version,
        )
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
            synced_user = replace(current_user, refresh_token=persisted.refresh_token)
            if not access_token_needs_refresh(synced_user, now=now):
                return AuthResolution(
                    synced_user,
                    state_changed=True,
                    browser_version=persisted.version,
                )
            refreshed, outcome = _refresh_candidate(store, persisted.refresh_token)
            if outcome == "ok" and refreshed is not None:
                return AuthResolution(
                    refreshed, persist_refresh=True, state_changed=True,
                    browser_version=persisted.version,
                )
            if outcome == "temporary":
                return AuthResolution(
                    synced_user,
                    state_changed=True,
                    recovery_pending=access_token_is_expired(synced_user, now=now),
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

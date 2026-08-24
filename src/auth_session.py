"""Seven-day browser recovery for Supabase passwordless sessions."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, MutableMapping

import streamlit as st

from src.cloud_store import (
    CloudSessionExpiredError,
    CloudStoreError,
    CloudUser,
    SupabaseStore,
)


AUTH_STORAGE_KEY = "essaypilot_auth_refresh_v1"
AUTH_BROWSER_COMMAND_KEY = "auth_browser_command"
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
      const action = data?.action || "read";
      try {{
        if (action === "clear") {{
          window.localStorage.removeItem(key);
          setStateValue("auth_session", {{ status: "cleared" }});
          return;
        }}
        if (action === "write") {{
          const refreshToken = String(data?.refresh_token || "");
          const savedAt = Number(data?.saved_at || 0);
          const now = Date.now() / 1000;
          if (
            !refreshToken || refreshToken.length > {_MAX_REFRESH_TOKEN_LENGTH} ||
            !Number.isFinite(savedAt) || savedAt <= 0 || savedAt > now + 300 ||
            now - savedAt >= retentionSeconds
          ) {{
            window.localStorage.removeItem(key);
            setStateValue("auth_session", {{ status: "cleared" }});
            return;
          }}
          window.localStorage.setItem(
            key,
            JSON.stringify({{ refresh_token: refreshToken, saved_at: savedAt }})
          );
          setStateValue("auth_session", {{ status: "written" }});
          return;
        }}
        const raw = window.localStorage.getItem(key);
        if (!raw) {{
          setStateValue("auth_session", {{ status: "empty" }});
          return;
        }}
        const stored = JSON.parse(raw);
        const refreshToken = String(stored?.refresh_token || "");
        const savedAt = Number(stored?.saved_at || 0);
        const now = Date.now() / 1000;
        if (
          !refreshToken || !Number.isFinite(savedAt) || savedAt <= 0 ||
          savedAt > now + 300 || now - savedAt >= retentionSeconds
        ) {{
          window.localStorage.removeItem(key);
          setStateValue("auth_session", {{ status: "expired" }});
          return;
        }}
        setStateValue("auth_session", {{
          status: "loaded",
          refresh_token: refreshToken,
          saved_at: savedAt,
        }});
        const wake = () => setStateValue("auth_wake", Date.now());
        const wakeWhenVisible = () => {{
          if (document.visibilityState === "visible") wake();
        }};
        window.addEventListener("online", wake);
        document.addEventListener("visibilitychange", wakeWhenVisible);
        return () => {{
          window.removeEventListener("online", wake);
          document.removeEventListener("visibilitychange", wakeWhenVisible);
        }};
      }} catch (_) {{
        try {{ window.localStorage.removeItem(key); }} catch (_) {{}}
        setStateValue("auth_session", {{ status: "unavailable" }});
      }}
    }}
    """,
)


@dataclass(frozen=True)
class PersistedRefreshSession:
    refresh_token: str
    saved_at: float


@dataclass(frozen=True)
class AuthResolution:
    user: CloudUser | None
    persist_refresh: bool = False
    clear_persisted: bool = False
    state_changed: bool = False


def cloud_user_to_state(user: CloudUser) -> dict[str, Any]:
    """Serialize the server-side Streamlit session, including token expiry."""
    return asdict(user)


def cloud_user_from_state(value: object) -> CloudUser | None:
    """Read a CloudUser from Streamlit state without accepting partial sessions."""
    if not isinstance(value, dict) or not value.get("access_token") or not value.get("id"):
        return None
    try:
        return CloudUser(
            id=str(value.get("id", "")),
            email=str(value.get("email", "")),
            access_token=str(value.get("access_token", "")),
            refresh_token=str(value.get("refresh_token", "")),
            expires_at=max(0, int(float(value.get("expires_at") or 0))),
            expires_in=max(0, int(float(value.get("expires_in") or 0))),
        )
    except (TypeError, ValueError):
        return None


def queue_refresh_token_write(
    state: MutableMapping[str, Any],
    refresh_token: str,
    *,
    now: float | None = None,
) -> None:
    """Schedule a browser write containing only the recovery token and timestamp."""
    if not refresh_token:
        queue_refresh_token_clear(state)
        return
    state[AUTH_BROWSER_COMMAND_KEY] = {
        "action": "write",
        "refresh_token": refresh_token,
        "saved_at": float(time.time() if now is None else now),
    }


def queue_refresh_token_clear(state: MutableMapping[str, Any]) -> None:
    """Ensure logout or invalidation wins over any pending token write."""
    state[AUTH_BROWSER_COMMAND_KEY] = {"action": "clear"}


def take_browser_command(state: MutableMapping[str, Any]) -> dict[str, Any]:
    """Consume one pending write/clear command; normal runs only read."""
    command = state.pop(AUTH_BROWSER_COMMAND_KEY, None)
    if isinstance(command, dict) and command.get("action") == "clear":
        return {"action": "clear"}
    if isinstance(command, dict) and command.get("action") == "write":
        return {
            "action": "write",
            "refresh_token": str(command.get("refresh_token", "")),
            "saved_at": command.get("saved_at", 0),
        }
    return {"action": "read"}


def browser_refresh_session(command: dict[str, Any]) -> dict[str, Any]:
    """Read or update the localStorage record through a hidden v2 component."""
    result = _AUTH_COMPONENT(
        data=command,
        key="essaypilot_auth_session",
        default={"auth_session": {"status": "loading"}, "auth_wake": 0},
        on_auth_session_change=lambda: None,
        on_auth_wake_change=lambda: None,
    )
    if command.get("action") != "read":
        return {}
    value = getattr(result, "auth_session", {})
    return value if isinstance(value, dict) else {}


def parse_persisted_refresh_session(
    value: object,
    *,
    now: float | None = None,
) -> tuple[PersistedRefreshSession | None, bool]:
    """Validate a browser recovery record and report whether it must be cleared."""
    if not isinstance(value, dict):
        return None, False
    status = str(value.get("status", ""))
    if status in {"", "loading", "empty", "cleared", "unavailable"}:
        return None, False
    if status == "expired":
        return None, True
    token = str(value.get("refresh_token", ""))
    try:
        saved_at = float(value.get("saved_at") or 0)
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
    return PersistedRefreshSession(token, saved_at), False


def access_token_needs_refresh(
    user: CloudUser,
    *,
    now: float | None = None,
) -> bool:
    """Refresh shortly before expiry; legacy sessions without expiry remain usable."""
    if not user.expires_at:
        return bool(user.refresh_token)
    current_time = time.time() if now is None else now
    return user.expires_at <= current_time + ACCESS_REFRESH_SKEW_SECONDS


def resolve_auth_session(
    store: SupabaseStore,
    current_user: CloudUser | None,
    browser_value: object,
    *,
    now: float | None = None,
) -> AuthResolution:
    """Refresh at most once and distinguish invalid tokens from transient outages."""
    if current_user is not None:
        if not access_token_needs_refresh(current_user, now=now):
            return AuthResolution(current_user)
        if not current_user.refresh_token:
            return AuthResolution(current_user)
        try:
            refreshed = store.refresh(current_user.refresh_token)
        except CloudSessionExpiredError:
            return AuthResolution(None, clear_persisted=True, state_changed=True)
        except CloudStoreError:
            return AuthResolution(current_user)
        return AuthResolution(refreshed, persist_refresh=True, state_changed=True)

    persisted, should_clear = parse_persisted_refresh_session(browser_value, now=now)
    if should_clear:
        return AuthResolution(None, clear_persisted=True, state_changed=True)
    if persisted is None:
        return AuthResolution(None)
    try:
        restored = store.refresh(persisted.refresh_token)
    except CloudSessionExpiredError:
        return AuthResolution(None, clear_persisted=True, state_changed=True)
    except CloudStoreError:
        return AuthResolution(None)
    return AuthResolution(restored, persist_refresh=True, state_changed=True)

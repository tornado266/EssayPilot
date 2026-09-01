"""Supabase passwordless authentication and row-level-secured learning records."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests
import streamlit as st


def _setting(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except (FileNotFoundError, KeyError):
        value = ""
    return str(value or os.getenv(name, "")).strip()


def _positive_int(value: object) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _jwt_exp(access_token: str) -> int:
    """Decode exp only to schedule refresh; this deliberately does not verify identity."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return _positive_int(decoded.get("exp")) if isinstance(decoded, dict) else 0
    except (IndexError, ValueError, TypeError, UnicodeError, binascii.Error):
        return 0


def _auth_expiry(
    access_token: str,
    raw_expires_at: object,
    raw_expires_in: object,
    *,
    now: float | None = None,
) -> tuple[int, int, str]:
    expires_at = _positive_int(raw_expires_at)
    expires_in = _positive_int(raw_expires_in)
    if expires_at:
        return expires_at, expires_in, "expires_at"
    if expires_in:
        current_time = time.time() if now is None else now
        return int(current_time) + expires_in, expires_in, "expires_in"
    jwt_exp = _jwt_exp(access_token)
    if jwt_exp:
        return jwt_exp, 0, "jwt"
    return 0, 0, "unknown"


class CloudStoreError(RuntimeError):
    """Safe cloud/authentication error for display in the app."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CloudSessionExpiredError(CloudStoreError):
    """The stored refresh token is no longer accepted by the auth service."""


def _missing_column_error(exc: CloudStoreError, *column_names: str) -> bool:
    message = str(exc).lower()
    return exc.status_code in {400, 404} and any(
        column.lower() in message for column in column_names
    )


@dataclass(frozen=True)
class CloudUser:
    id: str
    email: str
    access_token: str
    refresh_token: str = ""
    expires_at: int = 0
    expires_in: int = 0
    expiry_source: str = "unknown"


class SupabaseStore:
    def __init__(self) -> None:
        self.url = _setting("SUPABASE_URL").rstrip("/")
        self.anon_key = _setting("SUPABASE_ANON_KEY")
        self.service_role_key = _setting("SUPABASE_SERVICE_ROLE_KEY")
        self.secret_key = _setting("SUPABASE_SECRET_KEY")
        self.beta_start_at = _setting("BETA_START_AT")
        self._auth_user_getter: Callable[[], CloudUser | None] | None = None
        self._auth_user_updated: Callable[[CloudUser], None] | None = None
        self._auth_user_invalidated: Callable[[CloudUser], None] | None = None
        self._runtime_user: CloudUser | None = None
        self._auth_refresh_attempted_user_ids: set[str] = set()
        self._auth_refresh_temporarily_failed_user_ids: set[str] = set()
        self._auth_blocked_user_ids: set[str] = set()

    def bind_auth_session(
        self,
        user_getter: Callable[[], CloudUser | None],
        user_updated: Callable[[CloudUser], None],
        user_invalidated: Callable[[CloudUser], None],
    ) -> None:
        """Bind main-thread session callbacks used by the one-shot 401 recovery path."""
        self._auth_user_getter = user_getter
        self._auth_user_updated = user_updated
        self._auth_user_invalidated = user_invalidated

    @property
    def server_key(self) -> str:
        """Prefer Supabase's current secret-key format, with legacy fallback."""
        return self.secret_key or self.service_role_key

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.anon_key)

    @property
    def funnel_enabled(self) -> bool:
        return bool(self.enabled and self.server_key and self.beta_start_at)

    @property
    def analytics_enabled(self) -> bool:
        return bool(self.enabled and self.server_key)

    def _headers(
        self,
        access_token: str = "",
        *,
        prefer: str = "",
        api_key: str = "",
    ) -> dict[str, str]:
        request_key = api_key or self.anon_key
        headers = {
            "apikey": request_key,
            "Content-Type": "application/json",
        }
        bearer = access_token or (
            "" if request_key.startswith(("sb_secret_", "sb_publishable_")) else request_key
        )
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str = "",
        payload: dict[str, Any] | list[dict[str, Any]] | None = None,
        params: dict[str, str] | None = None,
        prefer: str = "",
        api_key: str = "",
        timeout: float = 20,
    ) -> Any:
        if not self.enabled:
            raise CloudStoreError("Supabase is not configured.")
        try:
            response = requests.request(
                method,
                f"{self.url}{path}",
                headers=self._headers(access_token, prefer=prefer, api_key=api_key),
                json=payload,
                params=params,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise CloudStoreError("Unable to reach the learning-record service.") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("msg") or response.json().get("message")
            except (ValueError, AttributeError):
                detail = ""
            raise CloudStoreError(
                detail or f"Cloud request failed ({response.status_code}).",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def _authenticated_request(
        self,
        user: CloudUser,
        method: str,
        path: str,
        *,
        allow_refresh: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Retry exactly one explicit 401 after rotating the user's session."""
        if user.id in self._auth_blocked_user_ids:
            raise CloudSessionExpiredError(
                "The saved login session has expired.", status_code=401
            )
        active_user = user
        if allow_refresh:
            if self._auth_user_getter is not None:
                latest = self._auth_user_getter()
                if latest is not None and latest.id == user.id:
                    active_user = latest
            elif self._runtime_user is not None and self._runtime_user.id == user.id:
                active_user = self._runtime_user
        try:
            return self._request(
                method, path, access_token=active_user.access_token, **kwargs
            )
        except CloudStoreError as exc:
            if exc.status_code != 401 or not allow_refresh:
                raise
        if not active_user.refresh_token:
            self._invalidate_runtime_user(active_user)
            raise CloudSessionExpiredError(
                "The saved login session has expired.", status_code=401
            )
        if active_user.id in self._auth_refresh_attempted_user_ids:
            if active_user.id in self._auth_refresh_temporarily_failed_user_ids:
                raise CloudStoreError(
                    "The saved login session could not be refreshed temporarily."
                )
            self._invalidate_runtime_user(active_user)
            raise CloudSessionExpiredError(
                "The saved login session has expired.", status_code=401
            )
        self._auth_refresh_attempted_user_ids.add(active_user.id)
        try:
            refreshed = self.refresh(active_user.refresh_token)
        except CloudSessionExpiredError:
            self._invalidate_runtime_user(active_user)
            raise
        except CloudStoreError:
            self._auth_refresh_temporarily_failed_user_ids.add(active_user.id)
            raise
        self._runtime_user = refreshed
        if self._auth_user_updated is not None:
            self._auth_user_updated(refreshed)
        try:
            result = self._request(
                method, path, access_token=refreshed.access_token, **kwargs
            )
        except CloudStoreError as exc:
            if exc.status_code == 401:
                self._invalidate_runtime_user(refreshed)
                raise CloudSessionExpiredError(
                    "The saved login session has expired.", status_code=401
                ) from exc
            raise
        return result

    def _invalidate_runtime_user(self, user: CloudUser) -> None:
        self._runtime_user = None
        self._auth_blocked_user_ids.add(user.id)
        if self._auth_user_invalidated is not None:
            self._auth_user_invalidated(user)

    def get_beta_funnel(self) -> dict[str, Any]:
        """Return anonymous aggregate counts using a server-only credential."""
        if not self.funnel_enabled:
            raise CloudStoreError(
                "Public-beta analytics require SUPABASE_SECRET_KEY (or the legacy "
                "SUPABASE_SERVICE_ROLE_KEY) and BETA_START_AT."
            )
        result = self._request(
            "POST",
            "/rest/v1/rpc/get_beta_funnel",
            access_token="" if self.server_key.startswith("sb_secret_") else self.server_key,
            api_key=self.server_key,
            payload={"p_since": self.beta_start_at},
        )
        return result if isinstance(result, dict) else {}

    def get_product_funnel(self) -> dict[str, Any]:
        """Return privacy-safe lifecycle conversion counts."""
        if not self.funnel_enabled:
            raise CloudStoreError(
                "Product analytics require SUPABASE_SECRET_KEY (or the legacy "
                "SUPABASE_SERVICE_ROLE_KEY) and BETA_START_AT."
            )
        result = self._request(
            "POST",
            "/rest/v1/rpc/get_product_funnel",
            access_token="" if self.server_key.startswith("sb_secret_") else self.server_key,
            api_key=self.server_key,
            payload={"p_since": self.beta_start_at},
        )
        return result if isinstance(result, dict) else {}

    def reserve_guest_trial(self, visitor_hash: str, flow_id: str) -> bool:
        result = self._request(
            "POST",
            "/rest/v1/rpc/reserve_guest_trial",
            payload={"p_visitor_hash": visitor_hash, "p_flow_id": flow_id},
        )
        return bool(isinstance(result, dict) and result.get("allowed"))

    def complete_guest_trial(self, visitor_hash: str, flow_id: str) -> bool:
        return bool(self._request(
            "POST",
            "/rest/v1/rpc/complete_guest_trial",
            payload={"p_visitor_hash": visitor_hash, "p_flow_id": flow_id},
        ))

    def release_guest_trial(self, visitor_hash: str, flow_id: str) -> bool:
        return bool(self._request(
            "POST",
            "/rest/v1/rpc/release_guest_trial",
            payload={"p_visitor_hash": visitor_hash, "p_flow_id": flow_id},
        ))

    def get_membership_entitlement(self, user: CloudUser) -> dict[str, Any]:
        """Return the authenticated user's current founder-pass snapshot."""
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/get_my_membership_entitlement",
            payload={},
        )
        return result if isinstance(result, dict) else {}

    def get_my_membership_request(self, user: CloudUser) -> dict[str, Any]:
        """Return the user's latest manual-payment review request."""
        result = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/membership_requests",
            params={
                "select": (
                    "id,application_code:request_code,status,payment_reference,"
                    "submitted_at:created_at,reviewed_at"
                ),
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        return result[0] if isinstance(result, list) and result else {}

    def create_membership_request(
        self,
        user: CloudUser,
        payment_reference: str,
        *,
        paid_at: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Submit payment details for manual review without accepting screenshots."""
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/create_membership_request",
            payload={
                "p_payment_reference": payment_reference.strip(),
                "p_paid_at": paid_at.strip(),
                "p_note": note.strip(),
            },
        )
        return result if isinstance(result, dict) else {}

    def reserve_membership_run(
        self,
        user: CloudUser,
        flow_id: str,
        content_hash: str,
        *,
        grading_run_id: str = "",
    ) -> dict[str, Any]:
        """Atomically reserve one of the member's three essay-cycle slots."""
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/reserve_membership_run",
            payload={
                "p_flow_id": flow_id,
                "p_content_hash": content_hash,
                "p_grading_run_id": grading_run_id or None,
            },
        )
        return result if isinstance(result, dict) else {}

    def complete_membership_run(
        self,
        user: CloudUser,
        flow_id: str,
        grading_run_id: str,
    ) -> dict[str, Any]:
        """Bind a successful reservation to its persisted first-draft run."""
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/complete_membership_run",
            payload={"p_flow_id": flow_id, "p_grading_run_id": grading_run_id},
        )
        return result if isinstance(result, dict) else {}

    def release_membership_run(
        self, user: CloudUser, flow_id: str
    ) -> dict[str, Any]:
        """Release a failed essay-cycle reservation without consuming quota."""
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/release_membership_run",
            payload={"p_flow_id": flow_id},
        )
        return result if isinstance(result, dict) else {}

    def get_membership_run_access(
        self, user: CloudUser, grading_run_id: str
    ) -> dict[str, Any]:
        """Return per-run training and second-draft allowances."""
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/get_membership_run_access",
            payload={"p_grading_run_id": grading_run_id},
        )
        return result if isinstance(result, dict) else {}

    def reserve_training_action(
        self,
        user: CloudUser,
        grading_run_id: str,
        flow_id: str,
        task_kind: str,
        task_key: str,
    ) -> dict[str, Any]:
        """Reserve one sentence/logic review; store only an opaque task key."""
        task_key_hash = self._practice_task_key_hash(task_kind, task_key)
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/reserve_training_action",
            payload={
                "p_grading_run_id": grading_run_id,
                "p_flow_id": flow_id,
                "p_task_kind": task_kind,
                "p_task_key_hash": task_key_hash,
            },
        )
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _practice_task_key_hash(task_kind: str, task_key: str) -> str:
        """Return the opaque task identity shared by reservations and attempts."""
        return hashlib.sha256(f"{task_kind}\0{task_key}".encode("utf-8")).hexdigest()

    def complete_training_action(
        self, user: CloudUser, flow_id: str
    ) -> dict[str, Any]:
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/complete_training_action",
            payload={"p_flow_id": flow_id},
        )
        return result if isinstance(result, dict) else {}

    def release_training_action(
        self, user: CloudUser, flow_id: str
    ) -> dict[str, Any]:
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/release_training_action",
            payload={"p_flow_id": flow_id},
        )
        return result if isinstance(result, dict) else {}

    def reserve_second_draft_action(
        self,
        user: CloudUser,
        grading_run_id: str,
        flow_id: str,
        content_hash: str,
    ) -> dict[str, Any]:
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/reserve_second_draft_action",
            payload={
                "p_grading_run_id": grading_run_id,
                "p_flow_id": flow_id,
                "p_content_hash": content_hash,
            },
        )
        return result if isinstance(result, dict) else {}

    def complete_second_draft_action(
        self,
        user: CloudUser,
        flow_id: str,
        revised_grading_run_id: str,
    ) -> dict[str, Any]:
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/complete_second_draft_action",
            payload={
                "p_flow_id": flow_id,
                "p_revised_grading_run_id": revised_grading_run_id,
            },
        )
        return result if isinstance(result, dict) else {}

    def release_second_draft_action(
        self, user: CloudUser, flow_id: str
    ) -> dict[str, Any]:
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/release_second_draft_action",
            payload={"p_flow_id": flow_id},
        )
        return result if isinstance(result, dict) else {}

    def list_pending_membership_requests(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List manual-review requests using only the server credential."""
        if not self.enabled or not self.server_key:
            raise CloudStoreError("Membership review requires a server-only key.")
        safe_limit = max(1, min(200, int(limit)))
        result = self._request(
            "GET",
            "/rest/v1/membership_requests",
            access_token="" if self.server_key.startswith("sb_secret_") else self.server_key,
            api_key=self.server_key,
            params={
                "select": (
                    "id,request_code,user_id,status,amount_cny,currency,"
                    "payment_reference,paid_at,note,created_at,reviewed_at,reviewed_by"
                ),
                "status": "eq.pending",
                "order": "created_at.asc",
                "limit": str(safe_limit),
            },
        )
        return result if isinstance(result, list) else []

    def approve_membership_request(self, request_id: str) -> dict[str, Any]:
        """Idempotently approve one request using only the server credential."""
        if not self.enabled or not self.server_key:
            raise CloudStoreError("Membership approval requires a server-only key.")
        result = self._request(
            "POST",
            "/rest/v1/rpc/approve_membership_request",
            access_token="" if self.server_key.startswith("sb_secret_") else self.server_key,
            api_key=self.server_key,
            payload={"p_request_id": request_id},
        )
        return result if isinstance(result, dict) else {}

    def record_product_event(
        self,
        event_name: str,
        visitor_hash: str,
        flow_id: str,
        *,
        user: CloudUser | None = None,
    ) -> bool:
        """Record a deduplicated event without essay, report, email, or raw device id."""
        if user is None:
            return bool(self._request(
                "POST",
                "/rest/v1/rpc/record_product_event",
                payload={
                    "p_event_name": event_name,
                    "p_visitor_hash": visitor_hash,
                    "p_flow_id": flow_id,
                },
            ))
        return bool(self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/record_product_event",
            allow_refresh=False,
            payload={
                "p_event_name": event_name,
                "p_visitor_hash": visitor_hash,
                "p_flow_id": flow_id,
            },
        ))

    def record_analytics_event(
        self,
        event_name: str,
        session_id: str,
        dedupe_key: str,
        *,
        anonymous_user_id: str = "",
        run_id: str = "",
        attempt_id: str = "",
        metadata: dict[str, object] | None = None,
        user: CloudUser | None = None,
        event_id: str = "",
    ) -> bool:
        """Record one deduplicated V2 event through the narrow analytics RPC."""
        payload = {
            "p_event_id": event_id or str(uuid.uuid4()),
            "p_session_id": session_id,
            "p_attempt_id": attempt_id or None,
            "p_run_id": run_id or None,
            "p_event_name": event_name,
            "p_metadata_json": metadata or {},
            "p_dedupe_key": dedupe_key,
            "p_anonymous_user_id": anonymous_user_id or None,
        }
        if user is None:
            return bool(self._request(
                "POST", "/rest/v1/rpc/record_analytics_event_v2",
                payload=payload, timeout=2,
            ))
        return bool(self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/record_analytics_event_v2",
            allow_refresh=False,
            payload=payload,
            timeout=2,
        ))

    def record_product_feedback(
        self,
        touchpoint: str,
        session_id: str,
        helpful: bool,
        reason_codes: list[str],
        dedupe_key: str,
        *,
        anonymous_user_id: str = "",
        run_id: str = "",
        attempt_id: str = "",
        user: CloudUser | None = None,
        feedback_id: str = "",
    ) -> bool:
        """Persist structured feedback without essay text, email, or free text."""
        payload = {
            "p_feedback_id": feedback_id or str(uuid.uuid4()),
            "p_session_id": session_id,
            "p_attempt_id": attempt_id or None,
            "p_run_id": run_id or None,
            "p_touchpoint": touchpoint,
            "p_helpful": helpful,
            "p_reason_codes": reason_codes,
            "p_dedupe_key": dedupe_key,
            "p_anonymous_user_id": anonymous_user_id or None,
        }
        if user is None:
            return bool(self._request(
                "POST", "/rest/v1/rpc/record_product_feedback",
                payload=payload, timeout=2,
            ))
        return bool(self._authenticated_request(
            user,
            "POST", "/rest/v1/rpc/record_product_feedback",
            allow_refresh=False, payload=payload, timeout=2,
        ))

    def get_analytics_dashboard(self, since: str | None = None) -> dict[str, Any]:
        """Return aggregate product metrics using a server-only credential."""
        if not self.analytics_enabled:
            raise CloudStoreError(
                "Product analytics require SUPABASE_SECRET_KEY (or the legacy "
                "SUPABASE_SERVICE_ROLE_KEY)."
            )
        result = self._request(
            "POST",
            "/rest/v1/rpc/get_analytics_dashboard",
            access_token="" if self.server_key.startswith("sb_secret_") else self.server_key,
            api_key=self.server_key,
            payload={"p_since": since},
        )
        return result if isinstance(result, dict) else {}

    def get_analytics_dashboard_v2(
        self,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Return the decision dashboard contract using a server-only credential."""
        if not self.analytics_enabled:
            raise CloudStoreError(
                "Product analytics require SUPABASE_SECRET_KEY (or the legacy "
                "SUPABASE_SERVICE_ROLE_KEY)."
            )
        result = self._request(
            "POST",
            "/rest/v1/rpc/get_analytics_dashboard_v2",
            access_token="" if self.server_key.startswith("sb_secret_") else self.server_key,
            api_key=self.server_key,
            payload={"p_since": since, "p_until": until},
        )
        return result if isinstance(result, dict) else {}

    def send_email_code(self, email: str) -> None:
        self._request("POST", "/auth/v1/otp", payload={"email": email, "create_user": True})

    def verify_email_code(self, email: str, code: str) -> CloudUser:
        result = self._request(
            "POST",
            "/auth/v1/verify",
            payload={"email": email, "token": code, "type": "email"},
        )
        return self._auth_user(result, fallback_email=email)

    def refresh(self, refresh_token: str) -> CloudUser:
        try:
            result = self._request(
                "POST",
                "/auth/v1/token",
                params={"grant_type": "refresh_token"},
                payload={"refresh_token": refresh_token},
            )
        except CloudStoreError as exc:
            if exc.status_code in {400, 401, 403}:
                raise CloudSessionExpiredError(
                    "The saved login session has expired.",
                    status_code=exc.status_code,
                ) from exc
            raise
        return self._auth_user(result, fallback_refresh_token=refresh_token)

    def sign_out(self, user: CloudUser) -> None:
        """Best-effort current-session logout; callers still clear local state on failure."""
        self._request(
            "POST",
            "/auth/v1/logout",
            access_token=user.access_token,
            params={"scope": "local"},
            timeout=5,
        )

    @staticmethod
    def _auth_user(
        result: Any,
        *,
        fallback_email: str = "",
        fallback_refresh_token: str = "",
    ) -> CloudUser:
        """Normalize Supabase auth responses without exposing their credentials."""
        if not isinstance(result, dict):
            raise CloudStoreError("Authentication service returned an incomplete session.")
        user = result.get("user") or {}
        access_token = str(result.get("access_token", ""))
        refresh_token = str(result.get("refresh_token") or fallback_refresh_token)
        user_id = str(user.get("id", ""))
        if not user_id or not access_token or not refresh_token:
            raise CloudStoreError("Authentication service returned an incomplete session.")
        expires_at, expires_in, expiry_source = _auth_expiry(
            access_token,
            result.get("expires_at"),
            result.get("expires_in"),
        )
        return CloudUser(
            id=user_id,
            email=str(user.get("email") or fallback_email),
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            expires_in=expires_in,
            expiry_source=expiry_source,
        )

    def save_grading_cycle(
        self,
        user: CloudUser,
        *,
        question: str,
        essay: str,
        word_count: int,
        package: dict[str, Any],
        content_hash: str,
    ) -> dict[str, Any]:
        structured = package["structured"]
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/save_grading_cycle",
            payload={
                "p_question": question,
                "p_essay": essay,
                "p_word_count": word_count,
                "p_content_hash": content_hash,
                "p_overall_band": structured["overall_band"],
                "p_criteria": structured["criteria"],
                "p_report_json": structured,
                "p_report_markdown": package["report"],
                "p_model": package["model"],
                "p_prompt_version": package["prompt_version"],
                "p_skill_version": package["skill_version"],
            },
        )
        if isinstance(result, dict) and result.get("reused") and result.get("grading_run_id"):
            existing = self._authenticated_request(
                user,
                "GET",
                "/rest/v1/grading_runs",
                params={
                    "select": "prompt_version",
                    "id": f"eq.{result['grading_run_id']}",
                    "limit": "1",
                },
            )
            existing_version = existing[0].get("prompt_version") if isinstance(existing, list) and existing else ""
            if existing_version != package["prompt_version"]:
                inserted = self._authenticated_request(
                    user,
                    "POST",
                    "/rest/v1/grading_runs",
                    prefer="return=representation",
                    payload={
                        "essay_id": result["essay_id"],
                        "user_id": user.id,
                        "overall_band": structured["overall_band"],
                        "criteria": structured["criteria"],
                        "report_json": structured,
                        "report_markdown": package["report"],
                        "model": package["model"],
                        "prompt_version": package["prompt_version"],
                        "skill_version": package["skill_version"],
                    },
                )
                if isinstance(inserted, list) and inserted:
                    return {
                        "essay_id": result["essay_id"],
                        "grading_run_id": inserted[0].get("id"),
                        "reused": False,
                    }
        return result if isinstance(result, dict) else {}

    def find_cached_grading(
        self,
        user: CloudUser,
        content_hash: str,
        prompt_version: str = "",
    ) -> dict[str, Any] | None:
        params = {
            "select": "id,essay_id,overall_band,criteria,report_json,report_markdown,model,prompt_version,skill_version,created_at,essays!inner(content_hash,question,content,word_count)",
            "essays.content_hash": f"eq.{content_hash}",
            "order": "created_at.desc",
            "limit": "1",
        }
        if prompt_version:
            params["prompt_version"] = f"eq.{prompt_version}"
        result = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/grading_runs",
            params=params,
        )
        if isinstance(result, list) and result:
            return result[0]
        return None

    def find_cached_scoring(
        self,
        user: CloudUser,
        content_hash: str,
        scoring_prompt_version: str,
    ) -> dict[str, Any] | None:
        """Find a locked score independently of the teaching/report version."""
        result = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/grading_runs",
            params={
                "select": "id,essay_id,report_json,model,skill_version,created_at,essays!inner(content_hash)",
                "essays.content_hash": f"eq.{content_hash}",
                "report_json->>scoring_prompt_version": f"eq.{scoring_prompt_version}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if isinstance(result, list) and result:
            report_json = result[0].get("report_json")
            if isinstance(report_json, dict) and isinstance(report_json.get("locked_scoring_decision"), dict):
                return result[0]
        return None

    def list_grading_runs(
        self, user: CloudUser, limit: int = 30, offset: int = 0
    ) -> list[dict[str, Any]]:
        params = {
            "select": "id,essay_id,overall_band,criteria,report_json,report_markdown,model,prompt_version,skill_version,draft_role,parent_run_id,created_at,essays(question,content,word_count)",
            "order": "created_at.desc",
            "limit": str(limit),
            "offset": str(offset),
        }
        try:
            result = self._authenticated_request(
                user, "GET", "/rest/v1/grading_runs", params=params
            )
        except CloudSessionExpiredError:
            raise
        except CloudStoreError as exc:
            if not _missing_column_error(exc, "draft_role", "parent_run_id"):
                raise
            params["select"] = "id,essay_id,overall_band,criteria,report_json,report_markdown,model,prompt_version,skill_version,created_at,essays(question,content,word_count)"
            result = self._authenticated_request(
                user, "GET", "/rest/v1/grading_runs", params=params
            )
        return result if isinstance(result, list) else []

    def get_grading_run(self, user: CloudUser, grading_run_id: str) -> dict[str, Any] | None:
        """Load one owner-scoped grading run for a direct report link."""
        result = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/grading_runs",
            params={
                "select": "id,essay_id,overall_band,criteria,report_json,report_markdown,model,prompt_version,skill_version,created_at,essays(question,content,word_count)",
                "id": f"eq.{grading_run_id}",
                "limit": "1",
            },
        )
        return result[0] if isinstance(result, list) and result else None

    def save_linked_grading_cycle(
        self,
        user: CloudUser,
        *,
        question: str,
        essay: str,
        word_count: int,
        package: dict[str, Any],
        content_hash: str,
        parent_run_id: str,
        draft_role: str = "second",
    ) -> dict[str, Any]:
        """Persist a linked draft as its own reusable run and report."""
        structured = package["structured"]
        result = self._authenticated_request(
            user, "POST", "/rest/v1/rpc/save_linked_grading_cycle",
            payload={
                "p_question": question, "p_essay": essay, "p_word_count": word_count,
                "p_content_hash": content_hash, "p_overall_band": structured["overall_band"],
                "p_criteria": structured["criteria"], "p_report_json": structured,
                "p_report_markdown": package["report"], "p_model": package["model"],
                "p_prompt_version": package["prompt_version"], "p_skill_version": package["skill_version"],
                "p_parent_run_id": parent_run_id, "p_draft_role": draft_role,
            },
        )
        return result if isinstance(result, dict) else {}

    def upsert_learning_items(self, user: CloudUser, rows: list[dict[str, Any]]) -> None:
        """Persist derived learning assets without creating duplicates."""
        if not rows:
            return
        self._authenticated_request(
            user,
            "POST",
            "/rest/v1/learning_items",
            params={"on_conflict": "user_id,item_key"},
            prefer="resolution=ignore-duplicates,return=minimal",
            payload=rows,
        )

    def upsert_learning_item(self, user: CloudUser, row: dict[str, Any]) -> dict[str, Any]:
        """Create a catalog-derived personal row on first use and return its id."""
        result = self._authenticated_request(
            user, "POST", "/rest/v1/learning_items",
            params={"on_conflict": "user_id,item_key"},
            prefer="resolution=merge-duplicates,return=representation", payload=row,
        )
        return result[0] if isinstance(result, list) and result else {}

    def list_learning_items(self, user: CloudUser, limit: int = 1000) -> list[dict[str, Any]]:
        result = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/learning_items",
            params={
                "select": "id,grading_run_id,item_key,item_type,category,source_text,target_text,explanation,origin,topic_category,function_category,usage_note,favorite,status,review_count,last_reviewed_at,created_at,updated_at,grading_runs(created_at,essays(question))",
                "order": "updated_at.desc",
                "limit": str(limit),
            },
        )
        return result if isinstance(result, list) else []

    def update_learning_item(
        self,
        user: CloudUser,
        item_id: str,
        *,
        status: str | None = None,
        review_count: int | None = None,
        favorite: bool | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = status
        if favorite is not None:
            payload["favorite"] = favorite
        if review_count is not None:
            payload["review_count"] = review_count
            payload["last_reviewed_at"] = datetime.now(timezone.utc).isoformat()
        self._authenticated_request(
            user,
            "PATCH",
            "/rest/v1/learning_items",
            params={"id": f"eq.{item_id}"},
            prefer="return=minimal",
            payload=payload,
        )

    def save_expression_attempt(
        self,
        user: CloudUser,
        *,
        learning_item_id: str,
        submitted_sentence: str,
        result: dict[str, Any],
        model: str,
        prompt_version: str,
    ) -> None:
        self._authenticated_request(
            user, "POST", "/rest/v1/expression_attempts",
            prefer="return=minimal",
            payload={
                "user_id": user.id,
                "learning_item_id": learning_item_id,
                "submitted_sentence": submitted_sentence,
                "feedback_zh": str(result.get("feedback_zh") or ""),
                "improved_sentence_en": str(result.get("improved_sentence_en") or ""),
                "appropriate": bool(result.get("appropriate")),
                "mastered": bool(result.get("mastered")),
                "model": model,
                "prompt_version": prompt_version,
            },
        )

    def list_expression_attempts(self, user: CloudUser, limit: int = 100) -> list[dict[str, Any]]:
        result = self._authenticated_request(
            user, "GET", "/rest/v1/expression_attempts",
            params={
                "select": "id,learning_item_id,submitted_sentence,feedback_zh,improved_sentence_en,appropriate,mastered,model,prompt_version,created_at",
                "order": "created_at.desc", "limit": str(limit),
            },
        )
        return result if isinstance(result, list) else []

    def update_learning_item_for_practice(
        self,
        user: CloudUser,
        *,
        grading_run_id: str,
        source_text: str,
        mastered: bool,
    ) -> None:
        """Synchronize a practice result with matching reusable error assets."""
        matches = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/learning_items",
            params={
                "select": "id,review_count",
                "grading_run_id": f"eq.{grading_run_id}",
                "source_text": f"eq.{source_text}",
                "limit": "10",
            },
        )
        for item in matches if isinstance(matches, list) else []:
            self.update_learning_item(
                user,
                str(item.get("id", "")),
                status="mastered" if mastered else "practicing",
                review_count=int(item.get("review_count") or 0) + 1,
            )

    def save_practice_attempt(
        self,
        user: CloudUser,
        *,
        grading_run_id: str,
        task_kind: str,
        task_key: str,
        task_index: int,
        original_text: str,
        submitted_text: str,
        feedback: str,
        training_action_id: str = "",
        training_flow_id: str = "",
        revision_text: str = "",
        mastered: bool = False,
        error_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        task_key_hash = self._practice_task_key_hash(task_kind, task_key)
        payload: dict[str, Any] = {
            "p_grading_run_id": grading_run_id,
            "p_action_id": training_action_id or None,
            "p_flow_id": training_flow_id or None,
            "p_task_kind": task_kind,
            "p_task_key_hash": task_key_hash,
            "p_task_index": task_index,
            "p_original_text": original_text,
            "p_submitted_text": submitted_text,
            "p_feedback": feedback,
            "p_revision_text": revision_text,
            "p_mastered": mastered,
            "p_error_tags": error_tags or [],
        }
        if training_action_id or training_flow_id:
            if not training_action_id or not training_flow_id:
                raise CloudStoreError(
                    "A practice proof requires both its action and flow identifiers."
                )
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/save_training_practice_attempt",
            payload=payload,
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise CloudStoreError("The saved practice feedback could not be confirmed.")
        return result

    def get_practice_attempt(
        self,
        user: CloudUser,
        *,
        grading_run_id: str,
        task_kind: str,
        task_key: str,
    ) -> dict[str, Any] | None:
        """Load one persisted sentence/logic attempt by its stable task identity."""
        result = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/practice_attempts",
            params={
                "select": (
                    "id,grading_run_id,task_kind,task_key_hash,task_index,original_text,"
                    "submitted_text,feedback,revision_text,status,error_tags,"
                    "training_action_id,training_flow_id,feedback_persisted_at,"
                    "settled_at,created_at,updated_at"
                ),
                "user_id": f"eq.{user.id}",
                "grading_run_id": f"eq.{grading_run_id}",
                "task_kind": f"eq.{task_kind}",
                "task_key_hash": f"eq.{self._practice_task_key_hash(task_kind, task_key)}",
                "limit": "1",
            },
        )
        if not isinstance(result, list) or not result:
            return None
        return result[0] if isinstance(result[0], dict) else None

    def list_practice_attempts_for_run(
        self,
        user: CloudUser,
        grading_run_id: str,
    ) -> list[dict[str, Any]]:
        """Load all persisted practice attempts for one owned grading run."""
        result = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/practice_attempts",
            params={
                "select": (
                    "id,grading_run_id,task_kind,task_key_hash,task_index,original_text,"
                    "submitted_text,feedback,revision_text,status,error_tags,"
                    "training_action_id,training_flow_id,feedback_persisted_at,"
                    "settled_at,created_at,updated_at"
                ),
                "user_id": f"eq.{user.id}",
                "grading_run_id": f"eq.{grading_run_id}",
                "order": "task_kind.asc,task_index.asc",
            },
        )
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    def list_pending_practice(self, user: CloudUser, limit: int = 10) -> list[dict[str, Any]]:
        result = self._authenticated_request(
            user,
            "GET",
            "/rest/v1/practice_attempts",
            params={
                "select": "id,grading_run_id,task_kind,task_key_hash,task_index,original_text,submitted_text,feedback,revision_text,status,error_tags,training_action_id,training_flow_id,feedback_persisted_at,settled_at,updated_at",
                "status": "eq.in_progress",
                "order": "updated_at.desc",
                "limit": str(limit),
            },
        )
        return result if isinstance(result, list) else []

    def list_draft_revisions(self, user: CloudUser, limit: int = 20) -> list[dict[str, Any]]:
        params = {
            "select": "id,essay_id,grading_run_id,revised_grading_run_id,draft_number,content,score_snapshot,report_json,report_markdown,progress_report,created_at,grading_runs!draft_revisions_grading_run_id_fkey(id,overall_band,report_json,report_markdown,essays(question,content)),revised_run:grading_runs!draft_revisions_revised_grading_run_id_fkey(id,overall_band,report_json,report_markdown,essays(question,content))",
            "order": "created_at.desc", "limit": str(limit),
        }
        try:
            result = self._authenticated_request(
                user, "GET", "/rest/v1/draft_revisions", params=params
            )
        except CloudSessionExpiredError:
            raise
        except CloudStoreError as exc:
            if not _missing_column_error(exc, "revised_grading_run_id"):
                raise
            params["select"] = "id,essay_id,grading_run_id,draft_number,content,score_snapshot,report_json,report_markdown,progress_report,created_at,grading_runs(overall_band,report_json,report_markdown,essays(question,content))"
            result = self._authenticated_request(
                user, "GET", "/rest/v1/draft_revisions", params=params
            )
        return result if isinstance(result, list) else []

    def get_draft_revision(
        self, user: CloudUser, grading_run_id: str
    ) -> dict[str, Any] | None:
        """Load the single persisted Draft 2 result for an original grading run."""
        params = {
            "select": "id,essay_id,grading_run_id,revised_grading_run_id,draft_number,content,score_snapshot,report_json,report_markdown,progress_report,created_at,revised_run:grading_runs!draft_revisions_revised_grading_run_id_fkey(id,overall_band,report_json,report_markdown,essays(question,content))",
            "grading_run_id": f"eq.{grading_run_id}",
            "draft_number": "eq.2",
            "order": "created_at.desc",
            "limit": "1",
        }
        try:
            result = self._authenticated_request(
                user, "GET", "/rest/v1/draft_revisions", params=params
            )
        except CloudSessionExpiredError:
            raise
        except CloudStoreError as exc:
            if not _missing_column_error(exc, "revised_grading_run_id"):
                raise
            params["select"] = (
                "id,essay_id,grading_run_id,draft_number,content,score_snapshot,"
                "report_json,report_markdown,progress_report,created_at"
            )
            result = self._authenticated_request(
                user, "GET", "/rest/v1/draft_revisions", params=params
            )
        return result[0] if isinstance(result, list) and result else None

    def save_second_draft_result(
        self,
        user: CloudUser,
        *,
        grading_run_id: str,
        flow_id: str,
        question: str,
        content: str,
        word_count: int,
        content_hash: str,
        package: dict[str, Any],
        scores: dict[str, float | None],
        progress_report: str,
    ) -> dict[str, Any]:
        """Atomically persist a linked grading run and its one Draft 2 revision."""
        structured = package["structured"]
        result = self._authenticated_request(
            user,
            "POST",
            "/rest/v1/rpc/save_second_draft_result",
            payload={
                "p_grading_run_id": grading_run_id,
                "p_flow_id": flow_id,
                "p_question": question,
                "p_content": content,
                "p_word_count": word_count,
                "p_content_hash": content_hash,
                "p_overall_band": structured["overall_band"],
                "p_criteria": structured["criteria"],
                "p_report_json": structured,
                "p_report_markdown": package["report"],
                "p_model": package["model"],
                "p_prompt_version": package["prompt_version"],
                "p_skill_version": package["skill_version"],
                "p_score_snapshot": scores,
                "p_progress_report": progress_report,
            },
        )
        return result if isinstance(result, dict) else {}

    def save_draft_revision(
        self,
        user: CloudUser,
        *,
        essay_id: str,
        grading_run_id: str,
        content: str,
        scores: dict[str, float | None],
        report_json: dict[str, Any],
        report_markdown: str,
        progress_report: str,
        revised_grading_run_id: str = "",
    ) -> None:
        payload = {
            "user_id": user.id,
            "essay_id": essay_id,
            "grading_run_id": grading_run_id,
            "draft_number": 2,
            "content": content,
            "score_snapshot": scores,
            "report_json": {} if revised_grading_run_id else report_json,
            "report_markdown": "" if revised_grading_run_id else report_markdown,
            "progress_report": progress_report,
            "idempotency_key": f"second-draft:{grading_run_id}",
        }
        if revised_grading_run_id:
            payload["revised_grading_run_id"] = revised_grading_run_id
        try:
            self._authenticated_request(
                user, "POST", "/rest/v1/draft_revisions",
                params={"on_conflict": "user_id,idempotency_key"},
                prefer="resolution=merge-duplicates,return=minimal", payload=payload,
            )
        except CloudSessionExpiredError:
            raise
        except CloudStoreError as exc:
            if not revised_grading_run_id:
                raise
            if not _missing_column_error(exc, "revised_grading_run_id"):
                raise
            payload.pop("revised_grading_run_id", None)
            payload.pop("idempotency_key", None)
            payload["report_json"] = report_json
            payload["report_markdown"] = report_markdown
            self._authenticated_request(
                user, "POST", "/rest/v1/draft_revisions",
                prefer="return=minimal", payload=payload,
            )

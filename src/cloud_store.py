"""Supabase passwordless authentication and row-level-secured learning records."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests
import streamlit as st


def _setting(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except (FileNotFoundError, KeyError):
        value = ""
    return str(value or os.getenv(name, "")).strip()


class CloudStoreError(RuntimeError):
    """Safe cloud/authentication error for display in the app."""


@dataclass(frozen=True)
class CloudUser:
    id: str
    email: str
    access_token: str
    refresh_token: str = ""


class SupabaseStore:
    def __init__(self) -> None:
        self.url = _setting("SUPABASE_URL").rstrip("/")
        self.anon_key = _setting("SUPABASE_ANON_KEY")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.anon_key)

    def _headers(self, access_token: str = "", *, prefer: str = "") -> dict[str, str]:
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {access_token or self.anon_key}",
            "Content-Type": "application/json",
        }
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
    ) -> Any:
        if not self.enabled:
            raise CloudStoreError("Supabase is not configured.")
        try:
            response = requests.request(
                method,
                f"{self.url}{path}",
                headers=self._headers(access_token, prefer=prefer),
                json=payload,
                params=params,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise CloudStoreError("Unable to reach the learning-record service.") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("msg") or response.json().get("message")
            except (ValueError, AttributeError):
                detail = ""
            raise CloudStoreError(detail or f"Cloud request failed ({response.status_code}).")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def send_email_code(self, email: str) -> None:
        self._request("POST", "/auth/v1/otp", payload={"email": email, "create_user": True})

    def verify_email_code(self, email: str, code: str) -> CloudUser:
        result = self._request(
            "POST",
            "/auth/v1/verify",
            payload={"email": email, "token": code, "type": "email"},
        )
        user = result.get("user") or {}
        return CloudUser(
            id=str(user.get("id", "")),
            email=str(user.get("email", email)),
            access_token=str(result.get("access_token", "")),
            refresh_token=str(result.get("refresh_token", "")),
        )

    def refresh(self, refresh_token: str) -> CloudUser:
        result = self._request(
            "POST",
            "/auth/v1/token",
            params={"grant_type": "refresh_token"},
            payload={"refresh_token": refresh_token},
        )
        user = result.get("user") or {}
        return CloudUser(
            id=str(user.get("id", "")),
            email=str(user.get("email", "")),
            access_token=str(result.get("access_token", "")),
            refresh_token=str(result.get("refresh_token", refresh_token)),
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
        result = self._request(
            "POST",
            "/rest/v1/rpc/save_grading_cycle",
            access_token=user.access_token,
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
        return result if isinstance(result, dict) else {}

    def find_cached_grading(self, user: CloudUser, content_hash: str) -> dict[str, Any] | None:
        result = self._request(
            "GET",
            "/rest/v1/grading_runs",
            access_token=user.access_token,
            params={
                "select": "id,essay_id,overall_band,criteria,report_json,report_markdown,model,prompt_version,skill_version,created_at,essays!inner(content_hash,question,content,word_count)",
                "essays.content_hash": f"eq.{content_hash}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if isinstance(result, list) and result:
            return result[0]
        return None

    def list_grading_runs(self, user: CloudUser, limit: int = 30) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            "/rest/v1/grading_runs",
            access_token=user.access_token,
            params={
                "select": "id,essay_id,overall_band,criteria,report_json,report_markdown,model,prompt_version,skill_version,created_at,essays(question,content,word_count)",
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        return result if isinstance(result, list) else []

    def save_practice_attempt(
        self,
        user: CloudUser,
        *,
        grading_run_id: str,
        task_kind: str,
        task_index: int,
        original_text: str,
        submitted_text: str,
        feedback: str,
        revision_text: str = "",
        mastered: bool = False,
        error_tags: list[str] | None = None,
    ) -> None:
        self._request(
            "POST",
            "/rest/v1/practice_attempts",
            access_token=user.access_token,
            params={"on_conflict": "user_id,grading_run_id,task_kind,task_index"},
            prefer="resolution=merge-duplicates,return=minimal",
            payload={
                "user_id": user.id,
                "grading_run_id": grading_run_id,
                "task_kind": task_kind,
                "task_index": task_index,
                "original_text": original_text,
                "submitted_text": submitted_text,
                "feedback": feedback,
                "revision_text": revision_text,
                "status": "mastered" if mastered else "in_progress",
                "error_tags": error_tags or [],
            },
        )

    def list_pending_practice(self, user: CloudUser, limit: int = 10) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            "/rest/v1/practice_attempts",
            access_token=user.access_token,
            params={
                "select": "id,grading_run_id,task_kind,task_index,original_text,submitted_text,feedback,revision_text,status,error_tags,updated_at",
                "status": "eq.in_progress",
                "order": "updated_at.desc",
                "limit": str(limit),
            },
        )
        return result if isinstance(result, list) else []

    def list_draft_revisions(self, user: CloudUser, limit: int = 20) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            "/rest/v1/draft_revisions",
            access_token=user.access_token,
            params={
                "select": "id,essay_id,grading_run_id,draft_number,score_snapshot,progress_report,created_at,grading_runs(overall_band)",
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        return result if isinstance(result, list) else []

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
    ) -> None:
        self._request(
            "POST",
            "/rest/v1/draft_revisions",
            access_token=user.access_token,
            prefer="return=minimal",
            payload={
                "user_id": user.id,
                "essay_id": essay_id,
                "grading_run_id": grading_run_id,
                "draft_number": 2,
                "content": content,
                "score_snapshot": scores,
                "report_json": report_json,
                "report_markdown": report_markdown,
                "progress_report": progress_report,
            },
        )

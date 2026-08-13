"""Supabase passwordless authentication and row-level-secured learning records."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
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
        self.service_role_key = _setting("SUPABASE_SERVICE_ROLE_KEY")
        self.beta_start_at = _setting("BETA_START_AT")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.anon_key)

    @property
    def funnel_enabled(self) -> bool:
        return bool(self.enabled and self.service_role_key and self.beta_start_at)

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
            "Authorization": f"Bearer {access_token or request_key}",
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
        api_key: str = "",
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

    def get_beta_funnel(self) -> dict[str, Any]:
        """Return anonymous aggregate counts using a server-only credential."""
        if not self.funnel_enabled:
            raise CloudStoreError(
                "Public-beta analytics require SUPABASE_SERVICE_ROLE_KEY and BETA_START_AT."
            )
        result = self._request(
            "POST",
            "/rest/v1/rpc/get_beta_funnel",
            access_token=self.service_role_key,
            api_key=self.service_role_key,
            payload={"p_since": self.beta_start_at},
        )
        return result if isinstance(result, dict) else {}

    def get_product_funnel(self) -> dict[str, Any]:
        """Return privacy-safe lifecycle conversion counts."""
        if not self.funnel_enabled:
            raise CloudStoreError(
                "Product analytics require SUPABASE_SERVICE_ROLE_KEY and BETA_START_AT."
            )
        result = self._request(
            "POST",
            "/rest/v1/rpc/get_product_funnel",
            access_token=self.service_role_key,
            api_key=self.service_role_key,
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

    def record_product_event(
        self,
        event_name: str,
        visitor_hash: str,
        flow_id: str,
        *,
        user: CloudUser | None = None,
    ) -> bool:
        """Record a deduplicated event without essay, report, email, or raw device id."""
        return bool(self._request(
            "POST",
            "/rest/v1/rpc/record_product_event",
            access_token=user.access_token if user else "",
            payload={
                "p_event_name": event_name,
                "p_visitor_hash": visitor_hash,
                "p_flow_id": flow_id,
            },
        ))

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
        if isinstance(result, dict) and result.get("reused") and result.get("grading_run_id"):
            existing = self._request(
                "GET",
                "/rest/v1/grading_runs",
                access_token=user.access_token,
                params={
                    "select": "prompt_version",
                    "id": f"eq.{result['grading_run_id']}",
                    "limit": "1",
                },
            )
            existing_version = existing[0].get("prompt_version") if isinstance(existing, list) and existing else ""
            if existing_version != package["prompt_version"]:
                inserted = self._request(
                    "POST",
                    "/rest/v1/grading_runs",
                    access_token=user.access_token,
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
        result = self._request(
            "GET",
            "/rest/v1/grading_runs",
            access_token=user.access_token,
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
        result = self._request(
            "GET",
            "/rest/v1/grading_runs",
            access_token=user.access_token,
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

    def get_grading_run(self, user: CloudUser, grading_run_id: str) -> dict[str, Any] | None:
        """Load one owner-scoped grading run for a direct report link."""
        result = self._request(
            "GET",
            "/rest/v1/grading_runs",
            access_token=user.access_token,
            params={
                "select": "id,essay_id,overall_band,criteria,report_json,report_markdown,model,prompt_version,skill_version,created_at,essays(question,content,word_count)",
                "id": f"eq.{grading_run_id}",
                "limit": "1",
            },
        )
        return result[0] if isinstance(result, list) and result else None

    def upsert_learning_items(self, user: CloudUser, rows: list[dict[str, Any]]) -> None:
        """Persist derived learning assets without creating duplicates."""
        if not rows:
            return
        self._request(
            "POST",
            "/rest/v1/learning_items",
            access_token=user.access_token,
            params={"on_conflict": "user_id,item_key"},
            prefer="resolution=ignore-duplicates,return=minimal",
            payload=rows,
        )

    def upsert_learning_item(self, user: CloudUser, row: dict[str, Any]) -> dict[str, Any]:
        """Create a catalog-derived personal row on first use and return its id."""
        result = self._request(
            "POST", "/rest/v1/learning_items", access_token=user.access_token,
            params={"on_conflict": "user_id,item_key"},
            prefer="resolution=merge-duplicates,return=representation", payload=row,
        )
        return result[0] if isinstance(result, list) and result else {}

    def list_learning_items(self, user: CloudUser, limit: int = 1000) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            "/rest/v1/learning_items",
            access_token=user.access_token,
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
        self._request(
            "PATCH",
            "/rest/v1/learning_items",
            access_token=user.access_token,
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
        self._request(
            "POST", "/rest/v1/expression_attempts", access_token=user.access_token,
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
        result = self._request(
            "GET", "/rest/v1/expression_attempts", access_token=user.access_token,
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
        matches = self._request(
            "GET",
            "/rest/v1/learning_items",
            access_token=user.access_token,
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

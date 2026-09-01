"""Manual membership product rules and display helpers.

The database remains the authority for access decisions.  This module only keeps
the public offer and read-only presentation rules in one testable place.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


@dataclass(frozen=True)
class FounderOffer:
    plan_code: str = "founder_pass_30d_3runs"
    price_cny: Decimal = Decimal("7.5")
    duration_days: int = 30
    run_quota: int = 3
    training_limit_per_run: int = 3
    second_draft_limit_per_run: int = 1
    auto_renews: bool = False
    expression_ai_included: bool = False


FOUNDER_OFFER = FounderOffer()
RENEWAL_OFFER = FounderOffer(
    plan_code="renewal_pass_30d_3runs",
    price_cny=Decimal("9.9"),
)


def offer_for_entitlement(payload: Mapping[str, object] | None) -> FounderOffer:
    """Choose display copy only; the server remains authoritative for pricing."""
    raw = dict(payload or {})
    next_plan_code = str(raw.get("next_plan_code") or "")
    try:
        purchase_count = max(0, int(raw.get("purchase_count") or 0))
    except (TypeError, ValueError):
        purchase_count = 0
    has_previous_purchase = bool(raw.get("has_previous_purchase")) or purchase_count > 0
    if next_plan_code == RENEWAL_OFFER.plan_code or has_previous_purchase:
        return RENEWAL_OFFER
    return FOUNDER_OFFER


def normalize_entitlement(payload: Mapping[str, object] | None) -> dict[str, object]:
    """Return a stable, conservative entitlement shape for UI rendering."""
    raw = dict(payload or {})
    quota = max(0, int(raw.get("run_quota") or FOUNDER_OFFER.run_quota))
    completed = max(0, int(raw.get("runs_completed") or 0))
    reserved = max(0, int(raw.get("runs_reserved") or 0))
    remaining = max(0, int(raw.get("runs_remaining") or 0))
    try:
        purchase_count = max(0, int(raw.get("purchase_count") or 0))
    except (TypeError, ValueError):
        purchase_count = 0
    has_previous_purchase = bool(raw.get("has_previous_purchase")) or purchase_count > 0
    next_offer = offer_for_entitlement(raw)
    next_amount_cny: Decimal | None = None
    try:
        if "next_amount_cny" in raw and raw.get("next_amount_cny") is not None:
            next_amount_cny = Decimal(str(raw.get("next_amount_cny")))
    except (ArithmeticError, TypeError, ValueError):
        next_amount_cny = None
    can_purchase = raw.get("can_purchase") is True
    server_offer_verified = (
        type(raw.get("can_purchase")) is bool
        and "next_plan_code" in raw
        and "next_amount_cny" in raw
        and str(raw.get("next_plan_code") or "") == next_offer.plan_code
        and next_amount_cny == next_offer.price_cny
    )
    return {
        "active": bool(raw.get("active")),
        "status": str(raw.get("status") or "none"),
        "plan_code": str(raw.get("plan_code") or ""),
        "expires_at": str(raw.get("expires_at") or ""),
        "run_quota": quota,
        "runs_completed": completed,
        "runs_reserved": reserved,
        "runs_remaining": min(quota, remaining),
        "purchase_count": purchase_count,
        "has_previous_purchase": has_previous_purchase,
        "next_plan_code": str(raw.get("next_plan_code") or next_offer.plan_code),
        "next_amount_cny": next_amount_cny,
        "can_purchase": can_purchase,
        "server_offer_verified": server_offer_verified,
    }


def entitlement_caption(payload: Mapping[str, object] | None) -> str:
    """Build a concise account status without implying automatic renewal."""
    entitlement = normalize_entitlement(payload)
    if entitlement["active"]:
        expiry = str(entitlement["expires_at"])[:10] or "未记录"
        current_plan = (
            "3 篇续包"
            if entitlement["plan_code"] == RENEWAL_OFFER.plan_code
            else "创始体验首包"
        )
        return (
            f"{current_plan} · "
            f"剩余 {entitlement['runs_remaining']}/{entitlement['run_quota']} 篇 · "
            f"有效至 {expiry} · 不自动续费"
        )
    if entitlement["status"] == "expired":
        return "当前训练包已到期 · 已生成内容仍可查看"
    if entitlement["status"] in {"exhausted", "used"}:
        return "当前训练包篇数已用完 · 已生成内容仍可查看"
    if entitlement["status"] == "pending":
        return "训练包尚未开始 · 暂时不能发起新的 AI 请求"
    if entitlement["status"] == "refunded":
        return "当前训练包已退款 · 已生成内容仍可查看"
    if entitlement["status"] == "revoked":
        return "当前训练包已停止 · 已生成内容仍可查看"
    return "免费用户 · 当前浏览器可免费生成 1 次首稿完整报告"


def action_reason_message(reason: object) -> str:
    """Translate server access decisions without leaking database details."""
    messages = {
        "membership_required": "本功能需要先开通 3 篇训练包。",
        "membership_inactive": "训练包当前不可用，请刷新权益或联系管理员。",
        "membership_expired": "训练包已到期；已有内容仍可查看。",
        "run_quota_exhausted": "3 篇完整训练额度已用完。",
        "training_limit_reached": "本篇 3 次专项 AI 点评已用完。",
        "already_completed": "这项 AI 任务已经完成，可直接查看已有结果。",
        "second_draft_completed": "本篇二稿评分与对比已经完成。",
        "run_access_required": "请先将这篇作文加入完整训练。",
        "reservation_conflict": "另一项请求正在处理中，请稍后刷新。",
        "free_report_used": "当前浏览器的 1 次免费首稿报告已经用完。",
        "existing_result": "这篇作文已经生成过报告，可直接打开已有结果。",
        "existing_run_access": "这篇作文已经加入过完整训练，可直接打开已有结果。",
        "grading_run_not_found": "没有找到可加入训练的首稿报告，请先保存报告。",
    }
    key = str(reason or "")
    return messages.get(key, "暂时无法确认本次权益，请稍后重试。")

"""Utilities for parsing and presenting structured IELTS examiner output."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from src.report_schema import format_practice_band_interval


CRITERIA_LABELS = {
    "task_response": "任务回应（TR）",
    "coherence_and_cohesion": "连贯与衔接（CC）",
    "lexical_resource": "词汇资源（LR）",
    "grammatical_range_and_accuracy": "语法多样性与准确性（GRA）",
}


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_json_object(text: str) -> str:
    cleaned = _strip_code_fence(text)
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]

    return cleaned


def parse_band(value: Any) -> float | None:
    """Return a valid IELTS band score, or None when parsing is not possible."""
    if value is None or isinstance(value, bool):
        return None

    try:
        score = float(value)
    except (TypeError, ValueError):
        match = re.search(r"\d(?:\.\d)?", str(value))
        if not match:
            return None
        score = float(match.group(0))

    if math.isfinite(score) and 0 <= score <= 9 and score * 2 == int(score * 2):
        return score
    return None


def parse_examiner_report(raw_text: str) -> dict[str, Any]:
    """Parse the AI response and return a safe structured wrapper."""
    try:
        data = json.loads(_extract_json_object(raw_text))
        if not isinstance(data, dict):
            raise ValueError("Top-level JSON value is not an object.")

        overall_band = parse_band(data.get("overall_band"))
        if overall_band is not None:
            data["overall_band"] = overall_band

        criteria = data.get("criteria_scores")
        if isinstance(criteria, dict):
            data["criteria_scores"] = {
                key: parse_band(value)
                for key, value in criteria.items()
                if parse_band(value) is not None
            }
        else:
            data["criteria_scores"] = {}

        return {
            "ok": True,
            "data": data,
            "raw": raw_text,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "data": {},
            "raw": raw_text,
            "error": str(exc),
        }


def structured_report_to_markdown(parsed: dict[str, Any]) -> str:
    """Convert a parsed report into Markdown for downloads and the error book."""
    if not parsed.get("ok"):
        return str(parsed.get("raw", ""))

    data = parsed.get("data", {})
    lines = ["# 雅思写作批改报告", ""]
    overall = data.get("overall_band")
    lines.extend(
        ["## 分数概览", "", f"预估分数区间：{format_practice_band_interval(overall)}", ""]
    )

    lines.extend(["## 四项评分", ""])
    criteria = data.get("criteria_scores", {})
    explanations = data.get("score_explanation", {})
    for key, label in CRITERIA_LABELS.items():
        score = criteria.get(key, "暂无") if isinstance(criteria, dict) else "暂无"
        reason = explanations.get(key, "") if isinstance(explanations, dict) else ""
        lines.append(f"- {label}: {score}. {reason}".strip())
    lines.append("")

    lines.extend(["## 主要问题", ""])
    for item in data.get("top_3_problems", []) or []:
        if isinstance(item, dict):
            lines.append(f"- 问题：{item.get('problem', '')}")
            lines.append(f"  原文：{item.get('original_sentence', '')}")
            lines.append(f"  建议：{item.get('suggestion', '')}")
    lines.append("")

    lines.extend(["## 逐句批改", ""])
    for item in data.get("sentence_level_corrections", []) or []:
        if isinstance(item, dict):
            lines.append(f"- 原文：{item.get('original', '')}")
            lines.append(f"  改写：{item.get('corrected', '')}")
            lines.append(f"  说明：{item.get('reason', '')}")
    lines.append("")

    lines.extend(["## Band 7.5 示范改写", "", str(data.get("band_75_rewrite", "")), ""])
    lines.extend(["## 表达积累", ""])
    for item in data.get("useful_expressions", []) or []:
        if isinstance(item, dict):
            lines.append(
                f"- {item.get('expression', '')}: {item.get('meaning', '')} "
                f"例句：{item.get('example', '')}"
            )
    lines.append("")

    lines.extend(["## 下一步训练", ""])
    for item in data.get("next_practice_plan", []) or []:
        lines.append(f"- {item}")

    return "\n".join(lines).strip()

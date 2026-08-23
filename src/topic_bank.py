"""Local, zero-token IELTS Task 2 topic bank utilities."""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

from src.expression_catalog import TOPIC_LABELS


DEFAULT_TOPIC_BANK_PATH = Path(__file__).resolve().parents[1] / "data" / "task2_topic_bank.json"

QUESTION_TYPE_LABELS = {
    "agree_disagree": "观点同意题",
    "discuss_both_views": "双边讨论题",
    "advantages_disadvantages": "利弊分析题",
    "problems_solutions": "问题解决题",
    "two_part": "两问类",
}


class TopicBankError(ValueError):
    """Raised when the local topic bank cannot be loaded safely."""


def validate_topic_bank(data: object) -> list[dict[str, str]]:
    """Validate and normalize topic-bank records without mutating the source."""
    if not isinstance(data, list):
        raise TopicBankError("题库数据应为题目列表。")
    if not data:
        raise TopicBankError("主题题库中暂时没有题目。")

    seen_ids: set[str] = set()
    validated: list[dict[str, str]] = []
    required_fields = ("id", "topic_category", "question_type", "question", "practice_focus")
    for index, raw_item in enumerate(data, start=1):
        if not isinstance(raw_item, Mapping):
            raise TopicBankError(f"第 {index} 条题目格式无效。")

        item: dict[str, str] = {}
        for field in required_fields:
            value = raw_item.get(field)
            if not isinstance(value, str):
                raise TopicBankError(f"第 {index} 条题目的 {field} 字段格式无效。")
            item[field] = value.strip()
        if not item["id"]:
            raise TopicBankError(f"第 {index} 条题目缺少 ID。")
        if item["id"] in seen_ids:
            raise TopicBankError(f"题目 ID 重复：{item['id']}。")
        if item["topic_category"] not in TOPIC_LABELS:
            raise TopicBankError(f"题目 {item['id']} 使用了非法题材。")
        if item["question_type"] not in QUESTION_TYPE_LABELS:
            raise TopicBankError(f"题目 {item['id']} 使用了非法题型。")
        if not item["question"]:
            raise TopicBankError(f"题目 {item['id']} 的英文题目为空。")
        if not item["practice_focus"]:
            raise TopicBankError(f"题目 {item['id']} 的练习重点为空。")

        seen_ids.add(item["id"])
        validated.append(item)
    return validated


def load_topic_bank(path: str | Path = DEFAULT_TOPIC_BANK_PATH) -> list[dict[str, str]]:
    """Load and validate the local JSON topic bank."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TopicBankError("主题题库暂时无法读取。") from exc
    return validate_topic_bank(data)


def filter_topics_by_category(
    topics: list[dict[str, str]], topic_category: str
) -> list[dict[str, str]]:
    """Return topics in one known category, preserving their JSON order."""
    if topic_category not in TOPIC_LABELS:
        raise TopicBankError("请选择有效的题材。")
    return [item for item in topics if item["topic_category"] == topic_category]


def apply_topic_selection(
    state: MutableMapping[str, Any],
    topic: Mapping[str, object],
    *,
    confirm_existing_essay: bool = False,
) -> str:
    """Select a topic while preserving every existing essay and learning artifact.

    Returns ``"confirmation_required"`` when an essay exists and the caller has
    not confirmed the topic switch; otherwise returns ``"selected"``.
    """
    normalized = validate_topic_bank([topic])[0]
    if str(state.get("essay_input") or "").strip() and not confirm_existing_essay:
        state["pending_topic_selection"] = normalized
        return "confirmation_required"

    state["topic_input"] = normalized["question"]
    state["selected_topic_id"] = normalized["id"]
    state["selected_topic_category"] = normalized["topic_category"]
    state["selected_topic_question"] = normalized["question"]
    state.pop("pending_topic_selection", None)
    return "selected"

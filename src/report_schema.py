"""Strict examiner result schema, validation, scoring, and Markdown rendering."""

from __future__ import annotations

import math
import re
import unicodedata
import hashlib
from typing import Any


SCHEMA_VERSION = "2.0"
PROMPT_VERSION = "task2-structured-zh-2026-08-09"
SKILL_VERSION = "ielts-writing-phase2-v1"
CRITERIA = (
    "Task Response",
    "Coherence and Cohesion",
    "Lexical Resource",
    "Grammatical Range and Accuracy",
)

CRITERION_DISPLAY_NAMES = {
    "Task Response": "任务回应（TR）",
    "Coherence and Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammatical Range and Accuracy": "语法多样性与准确性（GRA）",
}

SCORE_DISPLAY_NAMES = {
    "Overall Band": "总分",
    "Task Response": "任务回应（TR）",
    "Coherence & Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammar Range & Accuracy": "语法多样性与准确性（GRA）",
}


def submission_hash(question: str, essay: str) -> str:
    """Return a privacy-safe content fingerprint for per-user result reuse."""
    normalized = "\n".join(
        " ".join(value.split()).casefold() for value in (question.strip(), essay.strip())
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


EXAMINER_JSON_SCHEMA: dict[str, Any] = {
    "name": "essaypilot_examiner_report",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "criteria",
            "priorities",
            "problems",
            "sentence_corrections",
            "paragraph_feedback",
            "band_75_rewrite",
            "useful_expressions",
            "next_practice",
            "sentence_training",
            "logic_training",
            "error_tags",
        ],
        "properties": {
            "summary": {"type": "string"},
            "criteria": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion", "score", "reason", "evidence", "next_band_limit"],
                    "properties": {
                        "criterion": {"type": "string", "enum": list(CRITERIA)},
                        "score": {"type": "integer", "minimum": 0, "maximum": 9},
                        "reason": {"type": "string"},
                        "evidence": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "next_band_limit": {"type": "string"},
                    },
                },
            },
            "priorities": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"$ref": "#/$defs/coaching_item"}},
            "problems": {"type": "array", "minItems": 2, "maxItems": 5, "items": {"$ref": "#/$defs/coaching_item"}},
            "sentence_corrections": {
                "type": "array",
                "minItems": 3,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["original", "problem", "improved"],
                    "properties": {
                        "original": {"type": "string"},
                        "problem": {"type": "string"},
                        "improved": {"type": "string"},
                    },
                },
            },
            "paragraph_feedback": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["paragraph", "strength", "limitation", "improvement"],
                    "properties": {
                        "paragraph": {"type": "integer", "minimum": 1},
                        "strength": {"type": "string"},
                        "limitation": {"type": "string"},
                        "improvement": {"type": "string"},
                    },
                },
            },
            "band_75_rewrite": {"type": "string"},
            "useful_expressions": {
                "type": "array",
                "minItems": 3,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["expression", "meaning", "example"],
                    "properties": {
                        "expression": {"type": "string"},
                        "meaning": {"type": "string"},
                        "example": {"type": "string"},
                    },
                },
            },
            "next_practice": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task", "sentence_pattern", "warning"],
                "properties": {
                    "task": {"type": "string"},
                    "sentence_pattern": {"type": "string"},
                    "warning": {"type": "string"},
                },
            },
            "sentence_training": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["original", "goal", "reference"],
                    "properties": {
                        "original": {"type": "string"},
                        "goal": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                },
            },
            "logic_training": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["problem", "original", "task", "requirements"],
                    "properties": {
                        "problem": {"type": "string"},
                        "original": {"type": "string"},
                        "task": {"type": "string"},
                        "requirements": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    },
                },
            },
            "error_tags": {"type": "array", "items": {"type": "string"}},
        },
        "$defs": {
            "coaching_item": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "evidence", "why", "action"],
                "properties": {
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                    "why": {"type": "string"},
                    "action": {"type": "string"},
                },
            }
        },
    },
}


class ExaminerResultError(ValueError):
    """Raised when a structured examiner response is incomplete or inconsistent."""


def calculate_overall(criteria: list[dict[str, Any]]) -> float:
    """Calculate the IELTS half-band result from four whole-band criteria."""
    scores = [int(item["score"]) for item in criteria]
    if len(scores) != 4 or any(score < 0 or score > 9 for score in scores):
        raise ExaminerResultError("Exactly four whole-band criterion scores are required.")
    average = sum(scores) / 4
    return math.floor(average * 2 + 0.5) / 2


def _quote_is_present(quote: str, essay: str) -> bool:
    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        value = value.replace("’", "'").replace("‘", "'")
        return " ".join(value.strip().strip('“”\"').split())

    clean_quote = normalize(quote)
    clean_essay = normalize(essay)
    if len(clean_quote) >= 3 and clean_quote in clean_essay:
        return True
    fragments = [normalize(part) for part in re.split(r"\.{3,}|…+", quote)]
    if any(len(fragment.split()) >= 4 and fragment in clean_essay for fragment in fragments):
        return True
    quoted_terms = [normalize(part) for part in re.findall(r'["“]([^"”]+)["”]', quote)]
    if any(len(term) >= 3 and term in clean_essay for term in quoted_terms):
        return True
    list_terms = [normalize(part).strip(" .:()[]") for part in re.split(r"[,;/]", clean_quote)]
    if any(
        1 <= len(term.split()) <= 6
        and len(term) >= 4
        and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", clean_essay)
        for term in list_terms
    ):
        return True
    words = clean_quote.split()
    return any(" ".join(words[index : index + 6]) in clean_essay for index in range(max(0, len(words) - 5)))


def validate_examiner_result(data: dict[str, Any], essay: str) -> dict[str, Any]:
    """Validate key invariants and add program-owned score/version metadata."""
    if not isinstance(data, dict):
        raise ExaminerResultError("The examiner response is not a JSON object.")
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        raise ExaminerResultError("The examiner response is missing criterion scores.")
    labels = [item.get("criterion") for item in criteria if isinstance(item, dict)]
    if sorted(labels) != sorted(CRITERIA):
        raise ExaminerResultError("The examiner must return each IELTS criterion exactly once.")
    for item in criteria:
        score = item.get("score")
        if not isinstance(score, int) or isinstance(score, bool):
            raise ExaminerResultError("Criterion scores must be whole numbers.")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ExaminerResultError(f"{item['criterion']} has no essay evidence.")
        if not any(_quote_is_present(str(quote), essay) for quote in evidence):
            raise ExaminerResultError(f"{item['criterion']} evidence is not present in the essay.")
    for correction in data.get("sentence_corrections", []):
        if not _quote_is_present(str(correction.get("original", "")), essay):
            raise ExaminerResultError("A sentence correction does not quote the submitted essay.")
    for task in data.get("sentence_training", []):
        if not _quote_is_present(str(task.get("original", "")), essay):
            raise ExaminerResultError("A sentence training task does not quote the submitted essay.")
    normalized = dict(data)
    normalized["overall_band"] = calculate_overall(criteria)
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["prompt_version"] = PROMPT_VERSION
    normalized["skill_version"] = SKILL_VERSION
    return normalized


def score_snapshot(data: dict[str, Any]) -> dict[str, float | None]:
    mapping = {item["criterion"]: float(item["score"]) for item in data.get("criteria", [])}
    return {
        "Overall Band": float(data["overall_band"]),
        "Task Response": mapping.get("Task Response"),
        "Coherence & Cohesion": mapping.get("Coherence and Cohesion"),
        "Lexical Resource": mapping.get("Lexical Resource"),
        "Grammar Range & Accuracy": mapping.get("Grammatical Range and Accuracy"),
    }


def examiner_result_to_markdown(data: dict[str, Any]) -> str:
    """Render the strict result in the legacy report shape used by the UI and exports."""
    overall = float(data["overall_band"])
    lower = max(0.0, overall - 0.5)
    criteria_rows = []
    for item in data["criteria"]:
        evidence = "; ".join(f'“{quote.strip().strip(chr(34))}”' for quote in item["evidence"][:2])
        reason = f"{item['reason']} Evidence: {evidence} Next-band limit: {item['next_band_limit']}"
        criteria_rows.append(f"| {item['criterion']} | {item['score']} | {item['score']} | {reason} |")

    def coaching(items: list[dict[str, Any]]) -> str:
        blocks = []
        for index, item in enumerate(items, 1):
            blocks.append(
                f"{index}. **{item['title']}**\n"
                f"   - **Original evidence:** “{item['evidence']}”\n"
                f"   - **Why it matters:** {item['why']}\n"
                f"   - **Action:** {item['action']}"
            )
        return "\n\n".join(blocks)

    corrections = "\n".join(
        f"| {item['original'].replace('|', '/')} | {item['problem'].replace('|', '/')} | {item['improved'].replace('|', '/')} |"
        for item in data["sentence_corrections"]
    )
    paragraphs = "\n\n".join(
        f"### Paragraph {item['paragraph']}\n**What works:** {item['strength']}\n\n"
        f"**What weakens the band score:** {item['limitation']}\n\n"
        f"**One concrete improvement:** {item['improvement']}"
        for item in data["paragraph_feedback"]
    )
    expressions = "\n".join(
        f"| {item['expression'].replace('|', '/')} | {item['meaning'].replace('|', '/')} | {item['example'].replace('|', '/')} |"
        for item in data["useful_expressions"]
    )
    sentence_training = "\n".join(
        f'{index}. "{item["original"]}"\n   - 目标：{item["goal"]}\n   - 参考：{item["reference"]}'
        for index, item in enumerate(data["sentence_training"], 1)
    )
    logic_training = "\n\n".join(
        f"### 任务 {index}\n问题：{item['problem']}\n\n任务：{item['task']}\n\n"
        f'原文：“{item["original"]}”\n\n要求：\n' + "\n".join(f"- {rule}" for rule in item["requirements"])
        for index, item in enumerate(data["logic_training"], 1)
    )
    next_practice = data["next_practice"]
    return f"""# IELTS Writing Examiner Report

## 1. Overall Band Score

**Estimated band range: {lower:.1f}-{overall:.1f}**

**Likely score: {overall:.1f}**

{data['summary']}

## 2. Four Criteria Scores

| Criterion | Band Range | Likely Score | Why |
|---|---:|---:|---|
{chr(10).join(criteria_rows)}

## 3. Top 3 Score-Boosting Priorities

{coaching(data['priorities'])}

## 4. Main Problems

{coaching(data['problems'])}

## 5. Sentence-level Corrections

| Original | Problem | Improved version |
|---|---|---|
{corrections}

## 6. Paragraph-level Feedback

{paragraphs}

## 7. Band 7.5 Rewrite

{data['band_75_rewrite']}

## 8. Useful Expressions

| Expression | Meaning | Example |
|---|---|---|
{expressions}

## 9. Next Practice Task

**Task:** {next_practice['task']}

- **One sentence pattern to practise:** {next_practice['sentence_pattern']}
- **One warning about what to avoid next time:** {next_practice['warning']}

## 11. 单句提分训练

【练习任务】请先独立改写，再查看参考并提交点评。

{sentence_training}

## 12. 写作提升验证

【提升练习】围绕本轮最低评分项完成段落级重写。

{logic_training}
""".strip()

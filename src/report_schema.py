"""Strict examiner result schema, validation, scoring, and Markdown rendering."""

from __future__ import annotations

import math
import re
import unicodedata
import hashlib
from copy import deepcopy
from typing import Any


SCHEMA_VERSION = "2.9"
SCORING_PROMPT_VERSION = "task2-score-zh-official-claimed-audit-v10-2026-08-11"
FEEDBACK_PROMPT_VERSION = "task2-feedback-vocabulary-panel-v6-2026-08-20"
REPORT_PROMPT_VERSION = f"{SCORING_PROMPT_VERSION}+{FEEDBACK_PROMPT_VERSION}"
PROMPT_VERSION = SCORING_PROMPT_VERSION  # score-only compatibility API
SCORING_SKILL_VERSION = "ielts-writing-task2-official-v4"
FEEDBACK_SKILL_VERSION = "ielts-writing-feedback-closed-loop-v4"
SKILL_VERSION = SCORING_SKILL_VERSION  # score-only compatibility API
PRACTICE_BAND_INTERVAL_VERSION = "practice_band_interval_v1"
OVERALL_CALIBRATION_VERSION = "practice_overall_offset_v1"
OVERALL_CALIBRATION_OFFSET = 0.5
COACHING_CRITERIA = ("TR", "CC", "LR", "GRA")
COACHING_ACTION_TYPES = (
    "clarify", "develop", "support", "reorganize", "replace", "repair",
    "proofread_recurring",
)
COACHING_TO_SCORING_CRITERION = {
    "TR": "Task Response",
    "CC": "Coherence and Cohesion",
    "LR": "Lexical Resource",
    "GRA": "Grammatical Range and Accuracy",
}
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
            "essay_topic_category",
            "summary",
            "criteria",
            "priorities",
            "problems",
            "sentence_corrections",
            "vocabulary_recommendations",
            "paragraph_feedback",
            "band_75_rewrite",
            "useful_expressions",
            "next_practice",
            "sentence_training",
            "logic_training",
            "error_tags",
        ],
        "properties": {
            "essay_topic_category": {
                "type": "string",
                "enum": [
                    "education", "technology", "environment", "health",
                    "society_family", "work_economy", "government_policy",
                    "media_culture", "crime_law", "cities_transport"
                ],
            },
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
            "priorities": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"$ref": "#/$defs/coaching_item"}},
            "problems": {"type": "array", "minItems": 0, "maxItems": 5, "items": {"$ref": "#/$defs/coaching_item"}},
            "sentence_corrections": {
                "type": "array",
                "minItems": 0,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "criterion", "issue_type", "original", "problem", "improved",
                        "problem_spans", "learning_replacements",
                    ],
                    "properties": {
                        "criterion": {"type": "string", "enum": list(COACHING_CRITERIA)},
                        "issue_type": {"type": "string", "minLength": 1, "maxLength": 60},
                        "original": {"type": "string", "minLength": 1, "maxLength": 240},
                        "problem": {"type": "string"},
                        "improved": {"type": "string"},
                        "problem_spans": {
                            "type": "array",
                            "maxItems": 6,
                            "items": {"type": "string", "minLength": 1, "maxLength": 120},
                        },
                        "learning_replacements": {
                            "type": "array",
                            "maxItems": 2,
                            "items": {"$ref": "#/$defs/learning_replacement"},
                        },
                    },
                },
            },
            "vocabulary_recommendations": {
                "type": "array",
                "minItems": 4,
                "maxItems": 6,
                "items": {"$ref": "#/$defs/vocabulary_recommendation"},
            },
            "paragraph_feedback": {
                "type": "array",
                "minItems": 0,
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
                "minItems": 6,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["expression", "meaning", "usage_note", "example", "function_category"],
                    "properties": {
                        "expression": {"type": "string"},
                        "meaning": {"type": "string"},
                        "usage_note": {"type": "string"},
                        "example": {"type": "string"},
                        "function_category": {
                            "type": "string",
                            "enum": [
                                "core_collocation", "cause_effect", "contrast_concession",
                                "example_argument", "solution", "evaluation_stance"
                            ],
                        },
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
                "minItems": 0,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["original", "goal", "reference"],
                    "properties": {
                        "original": {"type": "string", "minLength": 1, "maxLength": 240},
                        "goal": {"type": "string"},
                        "reference": {"type": "string"},
                    },
                },
            },
            "logic_training": {
                "type": "array",
                "minItems": 0,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["problem", "original", "task", "requirements"],
                    "properties": {
                        "problem": {"type": "string"},
                        "original": {"type": "string", "minLength": 1, "maxLength": 240},
                        "task": {"type": "string"},
                        "requirements": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    },
                },
            },
            "error_tags": {"type": "array", "items": {"type": "string"}},
        },
        "$defs": {
            "vocabulary_recommendation": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind", "source", "target", "headword", "part_of_speech", "register",
                    "meaning_zh", "simple_definition", "pattern", "collocations",
                    "source_sentence", "reason_zh", "example_en", "example_zh",
                ],
                "properties": {
                    "kind": {"type": "string", "enum": ["recommended", "upgrade"]},
                    "source": {"type": "string", "minLength": 1, "maxLength": 100},
                    "target": {"type": "string", "minLength": 1, "maxLength": 100},
                    "headword": {"type": "string", "minLength": 1, "maxLength": 60},
                    "part_of_speech": {"type": "string", "minLength": 1, "maxLength": 40},
                    "register": {
                        "type": "string",
                        "enum": ["neutral", "formal", "academic", "informal", "technical"],
                    },
                    "meaning_zh": {"type": "string", "minLength": 1, "maxLength": 160},
                    "simple_definition": {"type": "string", "minLength": 1, "maxLength": 240},
                    "pattern": {"type": "string", "minLength": 1, "maxLength": 160},
                    "collocations": {
                        "type": "array", "minItems": 1, "maxItems": 4,
                        "items": {"type": "string", "minLength": 1, "maxLength": 100},
                    },
                    "source_sentence": {"type": "string", "minLength": 1, "maxLength": 300},
                    "reason_zh": {"type": "string", "minLength": 1, "maxLength": 280},
                    "example_en": {"type": "string", "minLength": 1, "maxLength": 300},
                    "example_zh": {"type": "string", "minLength": 1, "maxLength": 300},
                },
            },
            "learning_replacement": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source", "target", "headword", "part_of_speech", "meaning_zh",
                    "simple_definition", "pattern", "collocations", "usage_note_zh",
                ],
                "properties": {
                    "source": {"type": "string", "minLength": 1, "maxLength": 100},
                    "target": {"type": "string", "minLength": 1, "maxLength": 100},
                    "headword": {"type": "string", "minLength": 1, "maxLength": 60},
                    "part_of_speech": {"type": "string", "minLength": 1, "maxLength": 40},
                    "meaning_zh": {"type": "string", "minLength": 1, "maxLength": 160},
                    "simple_definition": {"type": "string", "minLength": 1, "maxLength": 240},
                    "pattern": {"type": "string", "minLength": 1, "maxLength": 160},
                    "collocations": {
                        "type": "array", "minItems": 1, "maxItems": 3,
                        "items": {"type": "string", "minLength": 1, "maxLength": 100},
                    },
                    "usage_note_zh": {"type": "string", "minLength": 1, "maxLength": 280},
                },
            },
            "coaching_item": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title", "evidence", "why", "action", "criterion",
                    "action_type", "success_check",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "evidence": {"type": "string", "minLength": 1, "maxLength": 240},
                    "why": {"type": "string"},
                    "action": {"type": "string"},
                    "criterion": {"type": "string", "enum": list(COACHING_CRITERIA)},
                    "action_type": {"type": "string", "enum": list(COACHING_ACTION_TYPES)},
                    "success_check": {"type": "string", "minLength": 1},
                },
            }
        },
    },
}


SCORING_DECISION_JSON_SCHEMA: dict[str, Any] = {
    "name": "essaypilot_task2_scoring_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["criteria", "uncertainty"],
        "properties": {
            "criteria": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "criterion", "score", "reason", "positive_evidence",
                        "limitation_evidence", "limitation_frequency",
                        "readability_impact", "why_not_lower_band",
                        "next_band_limit",
                    ],
                    "properties": {
                        "criterion": {"type": "string", "enum": list(CRITERIA)},
                        "score": {"type": "integer", "minimum": 0, "maximum": 9},
                        "reason": {"type": "string"},
                        "positive_evidence": {
                            "type": "array", "minItems": 1, "maxItems": 3,
                            "items": {"type": "string", "minLength": 1, "maxLength": 240},
                        },
                        "limitation_evidence": {
                            "type": "array", "minItems": 0, "maxItems": 3,
                            "items": {"type": "string", "minLength": 1, "maxLength": 240},
                        },
                        "limitation_frequency": {
                            "type": "string",
                            "enum": ["isolated", "occasional", "recurring", "pervasive"],
                        },
                        "readability_impact": {
                            "type": "string",
                            "enum": ["none", "minor", "intermittent", "severe"],
                        },
                        "why_not_lower_band": {"type": "string"},
                        "next_band_limit": {"type": "string"},
                    },
                },
            },
            "uncertainty": {
                "type": "object",
                "additionalProperties": False,
                "required": ["level", "adjacent_band_direction", "reason"],
                "properties": {
                    "level": {"type": "string", "enum": ["low", "material"]},
                    "adjacent_band_direction": {
                        "type": "string",
                        "enum": ["lower", "higher", "none"],
                    },
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


TEACHING_FEEDBACK_JSON_SCHEMA: dict[str, Any] = deepcopy(EXAMINER_JSON_SCHEMA)
TEACHING_FEEDBACK_JSON_SCHEMA["name"] = "essaypilot_task2_teaching_feedback"
TEACHING_FEEDBACK_JSON_SCHEMA["schema"]["required"].remove("criteria")
del TEACHING_FEEDBACK_JSON_SCHEMA["schema"]["properties"]["criteria"]


class ExaminerResultError(ValueError):
    """Raised when a structured examiner response is incomplete or inconsistent."""


class PriorityTrainingLinkError(ExaminerResultError):
    """Valid coaching evidence is missing its corresponding practice task."""


def calculate_descriptor_overall(criteria: list[dict[str, Any]]) -> float:
    """Calculate the uncalibrated IELTS half-band from four whole-band criteria."""
    raw_scores = [item.get("score") for item in criteria]
    if (
        len(raw_scores) != 4
        or any(not isinstance(score, int) or isinstance(score, bool) for score in raw_scores)
        or any(score < 0 or score > 9 for score in raw_scores)
    ):
        raise ExaminerResultError("Exactly four whole-band criterion scores are required.")
    scores = [int(score) for score in raw_scores]
    average = sum(scores) / 4
    return math.floor(average * 2 + 0.5) / 2


def calculate_overall(criteria: list[dict[str, Any]]) -> float:
    """Apply the versioned practice calibration to the descriptor-derived Overall."""
    raw = calculate_descriptor_overall(criteria)
    return min(9.0, raw + OVERALL_CALIBRATION_OFFSET)


def _normalize_evidence_text(value: str) -> str:
    """Normalize typography and whitespace without reordering or dropping words."""
    translation = str.maketrans(
        {
            "’": "'", "‘": "'", "“": '"', "”": '"',
            "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
        }
    )
    normalized = unicodedata.normalize("NFKC", value).translate(translation).casefold()
    return " ".join(normalized.strip().strip('“”\"\'').split())


def _quote_is_present(quote: str, essay: str) -> bool:
    """Require every teaching quotation to be one contiguous submitted substring."""
    clean_quote = _normalize_evidence_text(quote)
    return len(clean_quote) >= 3 and clean_quote in _normalize_evidence_text(essay)


def _exact_quote_is_present(quote: str, essay: str) -> bool:
    """Require the complete normalized quotation, not merely one matching fragment."""
    clean_quote = _normalize_evidence_text(quote)
    return len(clean_quote) >= 3 and clean_quote in _normalize_evidence_text(essay)


def _learning_replacement_is_valid(
    replacement: object, original: str, improved: str
) -> bool:
    """Validate an optional source-to-target learning item against its correction."""
    if not isinstance(replacement, dict):
        return False
    source = str(replacement.get("source") or "")
    target = str(replacement.get("target") or "")
    headword = _normalize_evidence_text(str(replacement.get("headword") or ""))
    collocations = replacement.get("collocations")
    required_text = (
        "part_of_speech", "meaning_zh", "simple_definition", "pattern", "usage_note_zh",
    )
    return (
        _exact_quote_is_present(source, original)
        and _exact_quote_is_present(target, improved)
        and len(headword) >= 2
        and headword in _normalize_evidence_text(target)
        and all(str(replacement.get(field) or "").strip() for field in required_text)
        and isinstance(collocations, list)
        and 1 <= len(collocations) <= 3
        and all(str(value).strip() for value in collocations)
    )


def _vocabulary_recommendation_is_valid(item: object, essay: str) -> bool:
    """Validate a whole-essay vocabulary item and its learner-dictionary links."""
    if not isinstance(item, dict):
        return False
    kind = str(item.get("kind") or "")
    source = str(item.get("source") or "")
    target = str(item.get("target") or "")
    source_sentence = str(item.get("source_sentence") or "")
    example_en = str(item.get("example_en") or "")
    headword = _normalize_evidence_text(str(item.get("headword") or ""))
    normal_source = _normalize_evidence_text(source)
    normal_target = _normalize_evidence_text(target)
    collocations = item.get("collocations")
    required_text = (
        "part_of_speech", "register", "meaning_zh", "simple_definition", "pattern",
        "reason_zh", "example_zh",
    )
    return (
        kind in {"recommended", "upgrade"}
        and _exact_quote_is_present(source_sentence, essay)
        and _exact_quote_is_present(source, source_sentence)
        and len(normal_target) >= 2
        and len(headword) >= 2
        and _exact_quote_is_present(target, example_en)
        and (kind != "recommended" or normal_source == normal_target)
        and (kind != "upgrade" or normal_source != normal_target)
        and all(str(item.get(field) or "").strip() for field in required_text)
        and isinstance(collocations, list)
        and 1 <= len(collocations) <= 4
        and all(str(value).strip() for value in collocations)
    )


def validate_scoring_decision(data: dict[str, Any], essay: str) -> dict[str, Any]:
    """Validate and freeze the score-only model response."""
    if not isinstance(data, dict):
        raise ExaminerResultError("The scoring response is not a JSON object.")
    if "overall_band" in data:
        raise ExaminerResultError("Overall Band is calculated by EssayPilot, not the model.")
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        raise ExaminerResultError("The scoring response is missing criterion scores.")
    labels = [item.get("criterion") for item in criteria if isinstance(item, dict)]
    if sorted(labels) != sorted(CRITERIA):
        raise ExaminerResultError("The examiner must return each IELTS criterion exactly once.")
    external_criteria: list[dict[str, Any]] = []
    invalid_evidence_items: list[tuple[str, str, str]] = []
    decision_errors: list[str] = []
    for item in criteria:
        score = item.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 9:
            raise ExaminerResultError("Criterion scores must be whole numbers from 0 to 9.")
        positive = item.get("positive_evidence")
        limitations = item.get("limitation_evidence")
        if not isinstance(positive, list) or not positive:
            raise ExaminerResultError(f"{item['criterion']} has no positive essay evidence.")
        if not isinstance(limitations, list) or (score < 9 and not limitations):
            raise ExaminerResultError(f"{item['criterion']} has no limitation essay evidence.")
        frequency = item.get("limitation_frequency")
        impact = item.get("readability_impact")
        if frequency not in {"isolated", "occasional", "recurring", "pervasive"}:
            raise ExaminerResultError(f"{item['criterion']} has an invalid limitation frequency.")
        if impact not in {"none", "minor", "intermittent", "severe"}:
            raise ExaminerResultError(f"{item['criterion']} has an invalid readability impact.")
        if frequency in {"recurring", "pervasive"} and len(limitations) < 2:
            decision_errors.append(
                f"{item['criterion']} claims recurring limitations without multiple exact examples."
            )
        if (
            item["criterion"] == "Grammatical Range and Accuracy"
            and score <= 6
            and frequency in {"isolated", "occasional"}
            and impact in {"none", "minor"}
        ):
            decision_errors.append(
                "A GRA score of 6 or below is inconsistent with only isolated/occasional "
                "minor limitations; reconsider the descriptor boundary without auto-adjusting."
            )
        evidence = [*positive, *limitations]
        invalid_evidence = [
            (field, str(quote))
            for field, values in (
                ("positive_evidence", positive),
                ("limitation_evidence", limitations),
            )
            for quote in values
            if not _exact_quote_is_present(str(quote), essay)
        ]
        if invalid_evidence:
            invalid_evidence_items.extend(
                (str(item["criterion"]), field, quote)
                for field, quote in invalid_evidence
            )
        if not str(item.get("why_not_lower_band", "")).strip():
            raise ExaminerResultError(
                f"{item['criterion']} does not explain why the demonstrated performance "
                "exceeds the adjacent lower band."
            )
        if not str(item.get("reason", "")).strip() or not str(item.get("next_band_limit", "")).strip():
            raise ExaminerResultError(f"{item['criterion']} lacks a complete descriptor explanation.")
        combined_evidence = list(dict.fromkeys(str(quote) for quote in evidence))
        external_criteria.append(
            {
                "criterion": item["criterion"],
                "score": score,
                "reason": item["reason"],
                "evidence": combined_evidence,
                # These remain internal score-lock metadata.  The established
                # evidence field is retained for every existing UI/storage caller.
                "positive_evidence": [str(quote) for quote in positive],
                "limitation_evidence": [str(quote) for quote in limitations],
                "limitation_frequency": frequency,
                "readability_impact": impact,
                "why_not_lower_band": item["why_not_lower_band"],
                "next_band_limit": item["next_band_limit"],
            }
        )
    if invalid_evidence_items:
        details = "; ".join(
            f"{criterion}.{field}={quote!r}"
            for criterion, field, quote in invalid_evidence_items
        )
        decision_errors.append(
            "Every evidence item must be present in the essay. Replace all invalid exact "
            "quotes by copying separate contiguous substrings verbatim from the submitted "
            f"essay: {details}"
        )
    if decision_errors:
        raise ExaminerResultError(" | ".join(decision_errors))
    uncertainty = data.get("uncertainty")
    if not isinstance(uncertainty, dict):
        raise ExaminerResultError("The scoring response is missing uncertainty metadata.")
    level = uncertainty.get("level")
    direction = uncertainty.get("adjacent_band_direction")
    if level not in {"low", "material"} or direction not in {"lower", "higher", "none"}:
        raise ExaminerResultError("The scoring uncertainty metadata is invalid.")
    if level == "low" and direction != "none":
        raise ExaminerResultError("Low uncertainty must not claim an adjacent-band direction.")
    if level == "material" and direction == "none":
        raise ExaminerResultError("Material uncertainty must identify an adjacent-band direction.")
    normalized = deepcopy(data)
    normalized["criteria"] = external_criteria
    normalized["raw_overall_band"] = calculate_descriptor_overall(external_criteria)
    normalized["overall_band"] = calculate_overall(external_criteria)
    normalized["overall_calibration_version"] = OVERALL_CALIBRATION_VERSION
    normalized["overall_calibration_offset"] = OVERALL_CALIBRATION_OFFSET
    return normalized


def restore_score_evidence_roles(scoring: dict[str, Any]) -> dict[str, Any]:
    """Enrich older v10 score locks whose exact evidence was stored as one combined list."""
    normalized = deepcopy(scoring)
    for item in normalized.get("criteria", []):
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            continue
        item.setdefault("positive_evidence", [str(evidence[0])])
        if "limitation_evidence" not in item:
            item["limitation_evidence"] = [str(value) for value in evidence[1:]]
    criteria = normalized.get("criteria")
    if isinstance(criteria, list):
        normalized["raw_overall_band"] = calculate_descriptor_overall(criteria)
        normalized["overall_band"] = calculate_overall(criteria)
        normalized["overall_calibration_version"] = OVERALL_CALIBRATION_VERSION
        normalized["overall_calibration_offset"] = OVERALL_CALIBRATION_OFFSET
    return normalized


def estimated_band_range(scoring: dict[str, Any]) -> tuple[float, float]:
    """Return the versioned conservative practice interval shown to learners."""
    value = scoring.get("overall_band")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExaminerResultError("A numeric internal Overall Band is required.")
    overall = float(value)
    if not 0.0 <= overall <= 9.0 or overall * 2 != round(overall * 2):
        raise ExaminerResultError("The internal Overall Band must use half-band steps from 0 to 9.")
    if overall <= 4.5:
        lower, upper = overall - 0.5, overall + 0.5
    elif overall <= 6.0:
        lower, upper = overall - 1.0, overall + 1.0
    elif overall == 6.5:
        lower, upper = overall - 0.5, overall + 1.0
    else:
        lower, upper = overall - 0.5, overall + 1.5
    return max(0.0, lower), min(9.0, upper)


def format_practice_band_interval(overall: float | int | None) -> str:
    """Format the learner-safe range without exposing the internal point estimate."""
    if overall is None:
        return "等待评分"
    lower, upper = estimated_band_range({"overall_band": overall})
    return f"{lower:.1f}–{upper:.1f}"


def format_overall_band(overall: float | int | None) -> str:
    """Format the program-calculated Overall Band for learner-facing surfaces."""
    if overall is None:
        return "等待评分"
    if isinstance(overall, bool) or not isinstance(overall, (int, float)):
        raise ExaminerResultError("A numeric Overall Band is required.")
    value = float(overall)
    if not 0.0 <= value <= 9.0 or value * 2 != round(value * 2):
        raise ExaminerResultError("Overall Band must use half-band steps from 0 to 9.")
    return f"{value:.1f}"


def learner_safe_report_markdown(markdown: str, overall: float | int | None) -> str:
    """Normalize legacy score lines to the current learner-facing point Overall."""
    if overall is None:
        return markdown
    score = format_overall_band(overall)
    score_line = re.compile(
        r"(?im)^\s*(?:\*\*)?(?:预估分数区间|Practice Band Interval|最可能分数|Likely Score|Overall Band Score|Overall Band|Overall|总分)"
        r"\s*[:：][^\r\n]*(?:\*\*)?\s*$"
    )
    replacement = f"**Overall：{score}**"
    if score_line.search(markdown):
        return score_line.sub(replacement, markdown)
    return markdown


_PSEUDO_SCORING_RULES = (
    re.compile(r"(?:IELTS|雅思).{0,18}(?:要求|规定|必须).{0,25}(?:字|词|段|模板|连接词|句式)", re.I),
    re.compile(r"(?:必须|务必|固定).{0,20}(?:\d+\s*(?:words?|字|词|段)|模板|连接词数量|指定句式)", re.I),
)


def pseudo_scoring_rule_hits(data: dict[str, Any]) -> list[str]:
    """Return feedback strings that falsely present coaching preferences as IELTS rules."""
    hits: list[str] = []
    fields: list[str] = [str(data.get("summary", ""))]
    for collection in ("priorities", "problems"):
        for item in data.get(collection, []):
            if isinstance(item, dict):
                fields.extend(str(item.get(key, "")) for key in ("why", "action", "success_check"))
    next_practice = data.get("next_practice")
    if isinstance(next_practice, dict):
        fields.extend(str(value) for value in next_practice.values())
    for value in fields:
        if any(pattern.search(value) for pattern in _PSEUDO_SCORING_RULES):
            hits.append(value)
    return hits


def feedback_quality_flags(
    data: dict[str, Any], essay: str, locked_scoring: dict[str, Any]
) -> dict[str, Any]:
    """Compute deterministic feedback-contract metrics without model judgement."""
    priorities = [item for item in data.get("priorities", []) if isinstance(item, dict)]
    evidence_items = [
        str(item.get(field, ""))
        for collection, field in (
            ("priorities", "evidence"), ("problems", "evidence"),
            ("sentence_corrections", "original"), ("sentence_training", "original"),
            ("logic_training", "original"),
            ("vocabulary_recommendations", "source_sentence"),
        )
        for item in data.get(collection, [])
        if isinstance(item, dict)
    ]
    evidence_valid = bool(evidence_items) and all(
        _exact_quote_is_present(value, essay) for value in evidence_items
    )
    scoring_by_criterion = {
        str(item.get("criterion")): item
        for item in locked_scoring.get("criteria", [])
        if isinstance(item, dict)
    }
    primary_aligned = False
    if priorities and priorities[0].get("criterion") in COACHING_TO_SCORING_CRITERION:
        criterion = COACHING_TO_SCORING_CRITERION[str(priorities[0]["criterion"])]
        limitations = scoring_by_criterion.get(criterion, {}).get("limitation_evidence") or []
        primary_aligned = _normalize_evidence_text(str(priorities[0].get("evidence", ""))) in {
            _normalize_evidence_text(str(value)) for value in limitations
        }
    training_originals = {
        _normalize_evidence_text(str(item.get("original", "")))
        for collection in ("sentence_training", "logic_training")
        for item in data.get(collection, [])
        if isinstance(item, dict)
    }
    closed_priorities = sum(
        _normalize_evidence_text(str(item.get("evidence", ""))) in training_originals
        for item in priorities
    )
    actionable_priorities = sum(
        item.get("criterion") in COACHING_CRITERIA
        and item.get("action_type") in COACHING_ACTION_TYPES
        and bool(str(item.get("action", "")).strip())
        and bool(str(item.get("success_check", "")).strip())
        for item in priorities
    )
    return {
        "structure_valid": len(priorities) == 2,
        "evidence_valid": evidence_valid,
        "primary_limitation_aligned": primary_aligned,
        "feedback_training_closed_loop": len(priorities) == 2 and closed_priorities == 2,
        "action_success_complete": len(priorities) == 2 and actionable_priorities == 2,
        "closed_priority_count": closed_priorities,
        "actionable_priority_count": actionable_priorities,
        "pseudo_scoring_rule_count": len(pseudo_scoring_rule_hits(data)),
    }


def drop_unverified_optional_teaching_items(
    data: dict[str, Any], essay: str
) -> tuple[dict[str, Any], list[str]]:
    """Drop unsupported optional coaching items after a strict retry, never inventing replacements."""
    normalized = deepcopy(data)
    removed: list[str] = []
    evidence_fields = {
        "priorities": "evidence",
        "problems": "evidence",
        "sentence_corrections": "original",
        "sentence_training": "original",
        "logic_training": "original",
    }
    for collection, field in evidence_fields.items():
        items = normalized.get(collection)
        if not isinstance(items, list):
            continue
        kept = [
            item
            for item in items
            if isinstance(item, dict)
            and _exact_quote_is_present(str(item.get(field, "")), essay)
        ]
        if len(kept) != len(items):
            removed.append(collection)
            normalized[collection] = kept
    for correction in normalized.get("sentence_corrections", []):
        if not isinstance(correction, dict):
            continue
        replacements = correction.get("learning_replacements")
        if not isinstance(replacements, list):
            continue
        original = str(correction.get("original") or "")
        improved = str(correction.get("improved") or "")
        kept_replacements = [
            item for item in replacements
            if _learning_replacement_is_valid(item, original, improved)
        ]
        if len(kept_replacements) != len(replacements):
            removed.append("sentence_corrections.learning_replacements")
            correction["learning_replacements"] = kept_replacements
    vocabulary_items = normalized.get("vocabulary_recommendations")
    if isinstance(vocabulary_items, list):
        kept_vocabulary = [
            item for item in vocabulary_items
            if _vocabulary_recommendation_is_valid(item, essay)
        ]
        if len(kept_vocabulary) != len(vocabulary_items):
            removed.append("vocabulary_recommendations")
            normalized["vocabulary_recommendations"] = kept_vocabulary
    return normalized, removed


def validate_examiner_result(data: dict[str, Any], essay: str) -> dict[str, Any]:
    """Validate key invariants and add program-owned score/version metadata."""
    if not isinstance(data, dict):
        raise ExaminerResultError("The examiner response is not a JSON object.")
    if data.get("essay_topic_category") not in {
        "education", "technology", "environment", "health", "society_family",
        "work_economy", "government_policy", "media_culture", "crime_law", "cities_transport",
    }:
        raise ExaminerResultError("The essay topic category is missing or invalid.")
    expressions = data.get("useful_expressions")
    if not isinstance(expressions, list) or not 6 <= len(expressions) <= 8:
        raise ExaminerResultError("The examiner must return 6-8 useful expressions.")
    vocabulary_items = data.get("vocabulary_recommendations")
    if not isinstance(vocabulary_items, list):
        raise ExaminerResultError("The vocabulary_recommendations field must be a list.")
    if not all(_vocabulary_recommendation_is_valid(item, essay) for item in vocabulary_items):
        raise ExaminerResultError(
            "Every vocabulary recommendation must link exact essay evidence to a complete "
            "learner-dictionary entry and bilingual example."
        )
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
        if not all(_exact_quote_is_present(str(quote), essay) for quote in evidence):
            raise ExaminerResultError(f"Every {item['criterion']} evidence item must be present in the essay.")
    priorities = data.get("priorities")
    if not isinstance(priorities, list) or len(priorities) != 2:
        raise ExaminerResultError("The feedback must contain exactly two core priorities.")
    for collection in ("priorities", "problems"):
        items = data.get(collection, [])
        if not isinstance(items, list):
            raise ExaminerResultError(f"The {collection} field must be a list.")
        for coaching_item in items:
            if not isinstance(coaching_item, dict):
                raise ExaminerResultError(f"A {collection} item is not an object.")
            if not _exact_quote_is_present(str(coaching_item.get("evidence", "")), essay):
                raise ExaminerResultError(f"A {collection} item does not quote the submitted essay.")
            if coaching_item.get("criterion") not in COACHING_CRITERIA:
                raise ExaminerResultError(f"A {collection} item has an invalid criterion.")
            if coaching_item.get("action_type") not in COACHING_ACTION_TYPES:
                raise ExaminerResultError(f"A {collection} item has an invalid action type.")
            if not str(coaching_item.get("action", "")).strip():
                raise ExaminerResultError(f"A {collection} item has no concrete action.")
            if not str(coaching_item.get("success_check", "")).strip():
                raise ExaminerResultError(f"A {collection} item has no success check.")
    corrections = data.get("sentence_corrections")
    if not isinstance(corrections, list):
        raise ExaminerResultError("The sentence_corrections field must be a list.")
    for correction in corrections:
        if not isinstance(correction, dict):
            raise ExaminerResultError("A sentence correction is not an object.")
        if not _exact_quote_is_present(str(correction.get("original", "")), essay):
            raise ExaminerResultError("A sentence correction does not quote the submitted essay.")
        if correction.get("criterion") not in COACHING_CRITERIA:
            raise ExaminerResultError("A sentence correction has an invalid criterion.")
        if not str(correction.get("issue_type") or "").strip():
            raise ExaminerResultError("A sentence correction has no issue type.")
        replacements = correction.get("learning_replacements")
        if not isinstance(replacements, list):
            raise ExaminerResultError("A sentence correction has no learning replacement list.")
        original = str(correction.get("original") or "")
        improved = str(correction.get("improved") or "")
        if not all(
            _learning_replacement_is_valid(item, original, improved)
            for item in replacements
        ):
            raise ExaminerResultError(
                "Every learning replacement must link exact source and target text and include "
                "a complete learner-dictionary explanation."
            )
    for task in data.get("sentence_training", []):
        if not _exact_quote_is_present(str(task.get("original", "")), essay):
            raise ExaminerResultError("A sentence training task does not quote the submitted essay.")
    for task in data.get("logic_training", []):
        if not _exact_quote_is_present(str(task.get("original", "")), essay):
            raise ExaminerResultError("A logic training task does not quote the submitted essay.")
    scoring_by_criterion = {
        str(item.get("criterion")): item for item in criteria if isinstance(item, dict)
    }
    primary = priorities[0]
    scoring_criterion = COACHING_TO_SCORING_CRITERION[str(primary["criterion"])]
    scoring_item = scoring_by_criterion.get(scoring_criterion, {})
    limitations = scoring_item.get("limitation_evidence")
    if not isinstance(limitations, list) or not limitations:
        raise ExaminerResultError(
            "The locked scoring decision has no limitation evidence for the first priority."
        )
    primary_evidence = _normalize_evidence_text(str(primary["evidence"]))
    if primary_evidence not in {
        _normalize_evidence_text(str(evidence)) for evidence in limitations
    }:
        allowed = ", ".join(repr(str(evidence)) for evidence in limitations)
        raise ExaminerResultError(
            "The first priority evidence must equal limitation evidence from its "
            f"locked {primary['criterion']} criterion. Copy exactly one of these allowed "
            f"strings without editing it: {allowed}"
        )
    pseudo_rules = pseudo_scoring_rule_hits(data)
    if pseudo_rules:
        raise ExaminerResultError(
            "Feedback must not present word counts, paragraph counts, templates, linking-word "
            "counts, or prescribed constructions as IELTS requirements."
        )
    training_originals = {
        _normalize_evidence_text(str(task.get("original", "")))
        for collection in ("sentence_training", "logic_training")
        for task in data.get(collection, [])
        if isinstance(task, dict)
    }
    for priority in priorities:
        evidence = _normalize_evidence_text(str(priority["evidence"]))
        if evidence not in training_originals:
            raise PriorityTrainingLinkError(
                "Every priority must link to sentence or logic training using the same original evidence. "
                f"Missing training original: {priority['evidence']!r}."
            )
    normalized = deepcopy(data)
    for correction in normalized.get("sentence_corrections", []):
        if not isinstance(correction, dict):
            continue
        original = str(correction.get("original") or "")
        spans = correction.get("problem_spans")
        correction["problem_spans"] = list(dict.fromkeys(
            span for span in spans or []
            if isinstance(span, str) and span and span in original
        )) if isinstance(spans, list) else []
    normalized["raw_overall_band"] = calculate_descriptor_overall(criteria)
    normalized["overall_band"] = calculate_overall(criteria)
    normalized["overall_calibration_version"] = OVERALL_CALIBRATION_VERSION
    normalized["overall_calibration_offset"] = OVERALL_CALIBRATION_OFFSET
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["prompt_version"] = REPORT_PROMPT_VERSION
    normalized["scoring_prompt_version"] = SCORING_PROMPT_VERSION
    normalized["feedback_prompt_version"] = FEEDBACK_PROMPT_VERSION
    normalized["skill_version"] = SCORING_SKILL_VERSION
    normalized["feedback_skill_version"] = FEEDBACK_SKILL_VERSION
    normalized["practice_band_interval_version"] = PRACTICE_BAND_INTERVAL_VERSION
    return normalized


def validate_teaching_training_links(
    data: dict[str, Any], essay: str
) -> tuple[dict[str, Any], list[str]]:
    """Complete missing tasks from validated priorities, then recheck every invariant.

    Scores, evidence, actions and success checks stay unchanged. This is not a
    fuzzy quote match or a generated model answer; the priority itself supplies
    the exercise. Other validation failures still require the normal retry.
    """
    try:
        return validate_examiner_result(data, essay), []
    except PriorityTrainingLinkError:
        normalized = deepcopy(data)

    collections = ("sentence_training", "logic_training")
    priorities = normalized["priorities"]
    priority_quotes = {
        _normalize_evidence_text(item["evidence"]) for item in priorities
    }
    repaired: list[str] = []
    for priority in priorities:
        originals = [
            _normalize_evidence_text(task["original"])
            for collection in collections
            for task in normalized.get(collection, [])
        ]
        if _normalize_evidence_text(priority["evidence"]) in originals:
            continue
        if priority["action_type"] in {"replace", "repair", "proofread_recurring"}:
            collection = "sentence_training"
            task = {
                "original": priority["evidence"],
                "goal": f"{priority['action']}\n完成标准：{priority['success_check']}",
                # An absent reference is preferable to inventing an English answer.
                "reference": "",
            }
        else:
            collection = "logic_training"
            task = {
                "problem": priority["title"],
                "original": priority["evidence"],
                "task": priority["action"],
                "requirements": [priority["success_check"]],
            }
        tasks = normalized.setdefault(collection, [])
        limit = EXAMINER_JSON_SCHEMA["schema"]["properties"][collection]["maxItems"]
        if len(tasks) > limit:
            raise ExaminerResultError(f"Too many {collection} tasks.")
        if len(tasks) == limit:
            # Keep at least one task for each already-linked priority, including
            # when multiple tasks quote the same evidence.
            removable = next((
                index for index, existing in enumerate(tasks)
                if (quote := _normalize_evidence_text(existing["original"])) not in priority_quotes
                or originals.count(quote) > 1
            ), None)
            if removable is None:
                raise PriorityTrainingLinkError("No room for the missing priority training task.")
            del tasks[removable]
        tasks.append(task)
        repaired.append(priority["evidence"])
    return validate_examiner_result(normalized, essay), repaired


def score_snapshot(data: dict[str, Any]) -> dict[str, float | None]:
    mapping = {item["criterion"]: float(item["score"]) for item in data.get("criteria", [])}
    return {
        "Overall Band": float(data["overall_band"]),
        "Task Response": mapping.get("Task Response"),
        "Coherence & Cohesion": mapping.get("Coherence and Cohesion"),
        "Lexical Resource": mapping.get("Lexical Resource"),
        "Grammar Range & Accuracy": mapping.get("Grammatical Range and Accuracy"),
    }

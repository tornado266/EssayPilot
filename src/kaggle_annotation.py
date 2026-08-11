"""Cost-controlled model annotation for pre-cleaned Kaggle training candidates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai_grader import build_client
from src.training_case_library import load_taxonomy


ANNOTATION_PROMPT_VERSION = "kaggle-training-annotation-v1"


def annotation_schema() -> dict[str, Any]:
    tags = list(load_taxonomy()["tags"])
    return {
        "name": "essaypilot_kaggle_training_annotation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["problem_tags", "strengths", "weaknesses", "training_targets"],
            "properties": {
                "problem_tags": {
                    "type": "array", "minItems": 1, "maxItems": 5,
                    "items": {"type": "string", "enum": tags},
                },
                "strengths": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
                "weaknesses": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string"}},
                "training_targets": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}},
            },
        },
    }


def validate_annotation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Annotation must be a JSON object.")
    taxonomy = load_taxonomy()["tags"]
    tags = value.get("problem_tags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 5 or any(tag not in taxonomy for tag in tags):
        raise ValueError("Annotation contains invalid problem tags.")
    for field, minimum, maximum in (
        ("strengths", 0, 3), ("weaknesses", 1, 5), ("training_targets", 1, 3)
    ):
        items = value.get(field)
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            raise ValueError(f"Annotation field {field} has an invalid length.")
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"Annotation field {field} contains an empty item.")
    return value


def annotate_case(case: dict[str, Any], *, model: str) -> tuple[dict[str, Any], dict[str, int | None]]:
    """Annotate weaknesses only; score metadata is deliberately excluded from messages."""
    client = build_client("OpenAI")
    taxonomy = load_taxonomy()
    prompt = (
        "Label this IELTS Task 2 learner response for training retrieval, not scoring. "
        "Use only tags from the supplied taxonomy. Do not estimate a Band. "
        "Keep strengths, weaknesses, and targets concise and evidence-based.\n\n"
        f"Taxonomy tags:\n{json.dumps(list(taxonomy['tags']), ensure_ascii=False)}\n\n"
        f"Question:\n{case.get('question', '')}\n\n"
        f"Essay:\n{case.get('essay_clean', '')}\n\n"
        f"Existing human feedback, if any (read-only):\n"
        f"{case.get('human_feedback_original') or case.get('feedback_extracted') or '(none)'}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You annotate learner-writing patterns for EssayPilot's training-only case library."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_schema", "json_schema": annotation_schema()},
        reasoning_effort="none",
        max_completion_tokens=1800,
    )
    content = response.choices[0].message.content
    annotation = validate_annotation(json.loads(content))
    usage = getattr(response, "usage", None)
    return annotation, {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def cache_record(
    case_id: str,
    *,
    model: str,
    status: str,
    annotation: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": status,
        "model": model,
        "prompt_version": ANNOTATION_PROMPT_VERSION,
        "annotated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "annotation": annotation,
        "usage": usage or {},
        "error": error,
    }


def load_annotation_cache(path: str | Path) -> dict[str, dict[str, Any]]:
    cache_path = Path(path)
    if not cache_path.exists():
        return {}
    output: dict[str, dict[str, Any]] = {}
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            output[str(record["case_id"])] = record
    return output

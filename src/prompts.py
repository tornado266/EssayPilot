"""Prompts for the isolated Task 2 scoring and teaching stages."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "ielts-writing"
IELTS_WRITING_SKILL_PATH = SKILL_DIR / "SKILL.md"
SCORING_REFERENCE_PATHS = (
    SKILL_DIR / "references" / "task2-band-descriptors.md",
    SKILL_DIR / "references" / "assessment-criteria.md",
    SKILL_DIR / "references" / "scoring-protocol.md",
)
OFFICIAL_DESCRIPTOR_VERSION = "updated May 2023"


def load_band_sample_anchors() -> str:
    """Legacy compatibility hook; unverified samples are never production anchors."""
    return ""


def load_skill_scoring_rules() -> str:
    """Load the concise skill plus every required official-reference layer."""
    paths = (IELTS_WRITING_SKILL_PATH, *SCORING_REFERENCE_PATHS)
    sections: list[str] = []
    try:
        for path in paths:
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                return ""
            if path == SCORING_REFERENCE_PATHS[0] and OFFICIAL_DESCRIPTOR_VERSION not in text:
                return ""
            sections.append(f"--- {path.name} ---\n{text}")
    except OSError:
        return ""
    return "\n\n".join(sections)


def _task2_only(task_type: str) -> None:
    if task_type != "Task 2":
        raise ValueError("EssayPilot V2 currently supports IELTS Writing Task 2 only.")


def _text_diagnostics(essay: str) -> str:
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", essay)
    paragraphs = [paragraph for paragraph in essay.splitlines() if paragraph.strip()]
    return f"word_count={len(words)}; non_empty_paragraphs={len(paragraphs)}"


def build_scoring_prompt(task_type: str, topic: str, essay: str) -> str:
    """Build the score-only prompt governed by official descriptor references."""
    _task2_only(task_type)
    rules = load_skill_scoring_rules()
    if not rules:
        raise RuntimeError("The IELTS scoring Skill or a required reference could not be loaded.")
    return f"""Assess this IELTS Writing Task 2 response as an estimated practice performance.

Authoritative scoring material:
{rules}

Decision contract:
- Return JSON matching the supplied score-only schema; never return Markdown.
- Judge Task Response, Coherence and Cohesion, Lexical Resource, and Grammatical Range and Accuracy independently.
- Return each criterion exactly once with a whole-number band from 0 to 9.
- In `reason`, describe Current performance in concise Chinese and identify the descriptor features demonstrated.
- In `next_band_limit`, explain Why not the next band in concise Chinese. If the score is 9, state that no higher public band exists.
- Every `evidence` value must be an exact, unedited substring of the submitted English essay. Include at least one item for every criterion.
- Do not return or infer an Overall Band. EssayPilot calculates it after validation.
- Set uncertainty to `material` only when the text genuinely supports an adjacent whole-band interpretation; identify `lower` or `higher`. Otherwise use `low` and `none`.
- Text diagnostics are audit context only. They cannot deduct points, cap a criterion, or override descriptor evidence.
- Do not treat a stylistic preference, a named sentence construction, apparent memorisation, or suspected authorship as an automatic scoring rule.

Audit diagnostics:
{_text_diagnostics(essay)}

Essay question:
{topic}

Student essay:
{essay}
""".strip()


def build_teaching_prompt(
    task_type: str,
    topic: str,
    essay: str,
    locked_scoring: dict[str, Any],
) -> str:
    """Build teaching output from a validated score that cannot be changed."""
    _task2_only(task_type)
    locked = json.dumps(locked_scoring, ensure_ascii=False, indent=2)
    return f"""Create teaching feedback for a Chinese IELTS learner from the locked scoring decision below.

Locked scoring decision (read-only):
{locked}

Teaching contract:
- Return JSON matching the supplied teaching-only schema; never return Markdown.
- Do not output `criteria`, criterion scores, or an Overall Band, and do not revise or reinterpret the locked decision.
- Organise coaching conceptually as: Current performance, Why not the next band, and Next training action. The first two are already locked; make priorities and actions directly address those gaps.
- Use exact unedited essay substrings in every coaching `evidence`, `sentence_corrections.original`, `sentence_training.original`, and `logic_training.original` field.
- Use Chinese for explanations and instructions. Keep submitted quotations, improved sentences, the model rewrite, reusable expressions, examples, the next IELTS question, sentence patterns, and sentence-training references in English.
- Create 2-3 priorities and 2-5 main problems. Make actions specific rather than prescribing a construction as a scoring requirement.
- Create 3-8 sentence corrections, paragraph feedback, a realistic Band 7.5 model rewrite close to the student's ideas, 6-8 transferable expressions, one next practice task, 2-4 sentence tasks, and 1-3 logic tasks.
- The learner should attempt training before using references; references are product-layer support, not scoring evidence.
- Classify the topic using the schema enum and use reusable error categories in `error_tags`.

Essay question:
{topic}

Student essay:
{essay}
""".strip()


def build_structured_grading_prompt(task_type: str, topic: str, essay: str) -> str:
    """Compatibility alias for callers that previously requested one structured prompt."""
    return build_scoring_prompt(task_type, topic, essay)

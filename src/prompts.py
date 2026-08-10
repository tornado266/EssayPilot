"""Prompts for the isolated Task 2 scoring and teaching stages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.text_utils import text_diagnostics


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
    diagnostics = text_diagnostics(essay)
    return "; ".join(f"{name}={value}" for name, value in diagnostics.items())


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
- For each criterion, first identify the higher-band features sustained across the response; only then determine which descriptor limitation prevents the adjacent higher band.
- In `reason`, describe the sustained current performance in concise Chinese, including the demonstrated descriptor features before limitations.
- Put only short, contiguous, exact, unedited essay substrings in `positive_evidence` and `limitation_evidence` (normally 3-25 words and never more than one sentence per item). Never join separate fragments, add separators such as `/` or `','`, add ellipses, correct text, or paraphrase. Provide at least one positive item for every criterion and at least one limitation item unless the criterion score is 9.
- Classify the observed limitation frequency as isolated, occasional, recurring, or pervasive and its readability impact as none, minor, intermittent, or severe. A recurring/pervasive claim requires at least two separate exact examples.
- In `next_band_limit`, explain the descriptor boundary to the next band in concise Chinese. If the score is 9, state that no higher public band exists.
- Do not return or infer an Overall Band. EssayPilot calculates it after validation.
- Set uncertainty to `material` only when the text genuinely supports an adjacent whole-band interpretation; identify `lower` or `higher`. Otherwise use `low` and `none`.
- Text diagnostics are audit context only. They cannot deduct points, cap a criterion, or override descriptor evidence.
- Do not treat a stylistic preference, a named sentence construction, apparent memorisation, or suspected authorship as an automatic scoring rule.
- Judge errors by frequency, range, effect on readability, and the proportion of error-free sentences. A single local error cannot determine a band.
- Never let the weakest criterion pull down another criterion. Do not count one spelling error in both LR and GRA, use Band 9 perfection as the threshold for Band 7, or let teaching advice decide a score.
- Sustained Band 7 or Band 8 features must receive their descriptor band even when isolated imperfections remain.
- Occasional omissions/lapses are compatible with Band 8 in TR, CC, and LR; a few non-impairing errors are compatible with Band 8 in GRA. Band 6 requires limitations characteristic of Band 6 across the response, not merely the existence of an imperfection.
- In GRA specifically, isolated or occasional minor errors that do not reduce clarity cannot support Band 6. Do not label the errors occasional/minor and then score as though they were recurring or reduced control across the response.

Audit diagnostics:
{_text_diagnostics(essay)}

Essay question (verbatim between markers):
<<<BEGIN_ESSAY_QUESTION>>>
{topic}
<<<END_ESSAY_QUESTION>>>

Student essay (verbatim between markers):
<<<BEGIN_STUDENT_ESSAY>>>
""" + essay + "\n<<<END_STUDENT_ESSAY>>>"


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
- Use one short, contiguous, exact, unedited essay substring in every coaching `evidence`, `sentence_corrections.original`, `sentence_training.original`, and `logic_training.original` field. Never join fragments, use `/`, insert line breaks between separate quotations, or use ellipses.
- Use Chinese for explanations and instructions. Keep submitted quotations, improved sentences, the model rewrite, reusable expressions, examples, the next IELTS question, sentence patterns, and sentence-training references in English.
- Return only genuine issues supported by the essay. Any of priorities, problems, sentence corrections, paragraph feedback, sentence tasks, or logic tasks may be an empty list; never invent a defect to fill a quota.
- Make actions specific rather than prescribing a construction as a scoring requirement. Also create a realistic Band 7.5 model rewrite close to the student's ideas, 6-8 transferable expressions, and one next practice task.
- The learner should attempt training before using references; references are product-layer support, not scoring evidence.
- Classify the topic using the schema enum and use reusable error categories in `error_tags`.

Essay question (verbatim between markers):
<<<BEGIN_ESSAY_QUESTION>>>
{topic}
<<<END_ESSAY_QUESTION>>>

Student essay (verbatim between markers):
<<<BEGIN_STUDENT_ESSAY>>>
""" + essay + "\n<<<END_STUDENT_ESSAY>>>"


def build_structured_grading_prompt(task_type: str, topic: str, essay: str) -> str:
    """Compatibility alias for callers that previously requested one structured prompt."""
    return build_scoring_prompt(task_type, topic, essay)

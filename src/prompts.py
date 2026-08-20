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
FEEDBACK_REFERENCE_PATH = SKILL_DIR / "references" / "feedback-protocol.md"
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


def load_skill_feedback_rules() -> str:
    """Load teaching-only rules without changing the scoring prompt or cache."""
    try:
        text = FEEDBACK_REFERENCE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text


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
- For each criterion, first identify features sustained across the whole response. Select the best-fitting descriptor, not the lowest descriptor touched by any isolated flaw.
- In `reason`, describe the sustained current performance in concise Chinese, leading with demonstrated features before limitations.
- Put only short, contiguous, exact, unedited essay substrings in `positive_evidence` and `limitation_evidence` (normally 3-25 words and never more than one sentence per item). Never join separate fragments, add separators such as `/` or `','`, add ellipses, correct text, or paraphrase. Provide at least one positive item for every criterion and at least one limitation item unless the criterion score is 9.
- Classify the observed limitation frequency as isolated, occasional, recurring, or pervasive and its readability impact as none, minor, intermittent, or severe. A recurring/pervasive claim requires at least two separate exact examples.
- In `why_not_lower_band`, state which sustained descriptor feature rules out the adjacent lower band. Do not mention an Overall Band.
- In `next_band_limit`, explain the descriptor boundary to the next band in concise Chinese. If the score is 9, state that no higher public band exists.
- Do not return or infer an Overall Band. EssayPilot calculates it after validation.
- Set uncertainty to `material` only when the text genuinely supports an adjacent whole-band interpretation; identify `lower` or `higher`. Otherwise use `low` and `none`.
- Text diagnostics are audit context only. They cannot deduct points, cap a criterion, or override descriptor evidence.
- Judge errors by frequency, range, effect on readability, and the proportion of error-free sentences. A single local error cannot determine a band.
- Never let the weakest criterion pull down another criterion. Do not count one spelling error in both LR and GRA, use Band 9 perfection as the threshold for Band 7, or let teaching advice decide a score.
- Band 7 or 8 does not require Band 9 perfection. Occasional lapses can coexist with those bands when the descriptor's positive features are sustained; Band 6 requires characteristic limitations across the response.

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
    feedback_rules = load_skill_feedback_rules()
    if not feedback_rules:
        raise RuntimeError("The IELTS feedback Skill reference could not be loaded.")
    return f"""Create teaching feedback for a Chinese IELTS learner from the locked scoring decision below.

Locked scoring decision (read-only):
{locked}

Feedback Skill rules:
{feedback_rules}

Teaching contract:
- Return JSON matching the supplied teaching-only schema; never return Markdown.
- Do not output `criteria`, criterion scores, or an Overall Band, and do not revise or reinterpret the locked decision.
- Organise coaching conceptually as: Current performance, Why not the next band, and Next training action. The first two are already locked; make priorities and actions directly address those gaps.
- Use one short, contiguous, exact, unedited essay substring in every coaching `evidence`, `sentence_corrections.original`, `sentence_training.original`, and `logic_training.original` field. Never join fragments, use `/`, insert line breaks between separate quotations, or use ellipses.
- For every sentence correction, set `criterion` to TR, CC, LR, or GRA and give `issue_type` one concise Chinese subtype such as `主谓一致`, `搭配不自然`, `指代不清`, or `论证跳步`. Set `problem_spans` to the shortest exact substring(s) inside `original` that are genuinely wrong; use multiple items for separate issues and an empty list only when no reliable local span exists.
- Treat each sentence correction as a node in the learner's problem map. Add zero to two `learning_replacements` only when the correction contains a direct, reusable source-to-target language upgrade. Each `source` must be one exact contiguous substring of `original`, and each `target` must be one exact contiguous substring of `improved`; `headword` must occur inside `target`.
- A `learning_replacement` must explain the target in a learner-dictionary format: part of speech, concise Chinese meaning, a simple original English definition, a reusable grammar/collocation pattern, one to three collocations, and a Chinese usage contrast with the source. Write these fields yourself for the submitted context; never quote, imitate verbatim, or claim to reproduce any branded dictionary entry.
- Do not create a dictionary item for a spelling-only fix, punctuation, an article by itself, or a bare inflection/agreement repair such as `teaches` to `teach`. Return an empty `learning_replacements` array when there is no reliable and reusable lexical or phrase-level upgrade.
- Independently of sentence corrections, return 4-6 `vocabulary_recommendations` drawn from the whole submitted essay. Use `recommended` for a precise, natural, reusable word or phrase already used well; in that case `source` and `target` must be the same exact essay substring. Use `upgrade` for an ordinary, vague, or less natural exact essay substring that can be improved; in that case `target` must be a different, natural expression used verbatim in `example_en`. Mix both kinds when the essay genuinely supports both, but never invent a weakness to satisfy a quota.
- For every vocabulary recommendation, copy one exact `source_sentence` from the essay and make `source` one exact substring inside it. Set `headword` to the dictionary form corresponding to `target`, choose a truthful register, and provide one contextual sense in a classroom learner-dictionary style: part of speech, register, concise Chinese meaning, a simple original English definition, reusable pattern, one to four collocations, a Chinese selection/upgrade reason, and a newly written bilingual example. Prefer useful, natural academic language over rare or showy synonyms. Never copy or claim to reproduce Longman or another branded dictionary entry.
- Use Chinese for explanations and instructions. Keep submitted quotations, improved sentences, the model rewrite, reusable expressions, examples, the next IELTS question, sentence patterns, and sentence-training references in English.
- Return only genuine issues supported by the essay. Problems, sentence corrections, paragraph feedback, and optional extra tasks may be empty; never invent a defect merely to fill those sections.
- Return exactly two `priorities`, ordered by learning impact. For each, set `criterion` to TR, CC, LR, or GRA; select one allowed `action_type`; write one minimal, concrete `action`; and write an observable `success_check`.
- For the first priority, choose its criterion first and copy `evidence` exactly from that criterion's locked `limitation_evidence`. Do not shorten, combine, correct, or paraphrase it. The second priority evidence must be one contiguous exact essay substring.
- For each priority, create at least one `sentence_training` or `logic_training` item whose `original` exactly equals that priority's `evidence`. This is the required feedback-to-training closed loop.
- Make actions specific rather than prescribing a construction as a scoring requirement. Also create a realistic Band 7.5 model rewrite close to the student's ideas, 6-8 transferable expressions, and one next practice task.
- The learner should attempt training before using references; references are product-layer support, not scoring evidence.
- Classify the topic using the schema enum and use reusable error categories in `error_tags`.
- Select the first priority by learning impact: an unsupported or underdeveloped central idea normally precedes local language repair; a recurring language pattern may precede a minor structural issue. Never infer a flaw only because it is common in the corpus.
- Missing task response, position, central-idea development, and paragraph logic normally outrank local errors. Isolated spelling, article, punctuation, and word-choice errors belong in sentence corrections; only a recurring readability-affecting language pattern may become a priority.
- Never claim that IELTS requires a fixed word count, paragraph count, template, number of linking words, vocabulary quota, or named sentence construction.
- Distinguish a broad observed pattern from a specific subtype. If the essay supports only a general accuracy problem, do not invent a particular article, tense, or preposition diagnosis.

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

"""Read-only Kaggle training-case retrieval isolated from IELTS scoring references."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from src.text_utils import count_words


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TAXONOMY_PATH = ROOT_DIR / "data" / "training_taxonomy.json"
DEFAULT_LIBRARY_PATH = ROOT_DIR / "data" / "processed" / "kaggle_ielts" / "core_training_cases.jsonl"
TRAINING_CASE_METADATA_VERSION = "1.0"
MAX_EXCERPT_WORDS = 120
MIN_EXCERPT_SENTENCES = 2
MAX_EXCERPT_SENTENCES = 4


class TrainingCaseError(ValueError):
    """Raised when untrusted corpus data violates the training-only contract."""


@dataclass(frozen=True)
class TrainingCaseMatch:
    """The deliberately small, public projection shown to a learner."""

    case_id: str
    problem_tag: str
    training_goal: str
    essay_context: str
    excerpt: str
    similarity_explanation: str
    observation_question: str

    def as_public_dict(self) -> dict[str, str]:
        return asdict(self)


def load_taxonomy(path: str | Path = DEFAULT_TAXONOMY_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("task_type") != "task2" or not isinstance(data.get("tags"), dict):
        raise TrainingCaseError("The Task 2 training taxonomy is invalid.")
    return data


def _normalized_label(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


_TAG_RULES: tuple[tuple[str, str], ...] = (
    ("TR.partial_task_response", r"partial|部分回应|漏答|没有回应|address all|任务回应不充分|缺少对.*讨论"),
    ("TR.position_consistency", r"position consistency|立场不一致|前后矛盾"),
    ("TR.position_clarity", r"position|立场|观点不清|thesis"),
    ("TR.conclusion_quality", r"conclusion|结论"),
    ("TR.example_relevance", r"irrelevant example|例子.*不相关|举例.*无关"),
    ("TR.example_development", r"example development|例子.*展开|解释例子"),
    ("TR.overgeneralization", r"overgeneral|绝对化|以偏概全"),
    ("TR.argument_depth", r"argument depth|论证深度|论证浅|分析浅|缺乏深入|不够有说服力|深入论证"),
    ("TR.idea_development", r"idea development|develop|展开|支撑不足|论证简略|论证不充分|因果"),
    ("CC.paragraphing", r"paragraphing|分段|段落划分|全文(?:只有一个|没有)段落"),
    ("CC.paragraph_focus", r"paragraph focus|段落中心|跑题"),
    ("CC.logical_progression", r"logical progression|逻辑推进|逻辑|progression"),
    ("CC.mechanical_linkers", r"mechanical linker|机械连接|连接词生硬"),
    ("CC.overused_linkers", r"overused linker|连接词.*重复|连接词过多"),
    ("CC.reference_clarity", r"reference clarity|指代不清"),
    ("CC.repetition", r"coherence.*repetition|内容重复|观点重复"),
    ("LR.collocation", r"collocation|搭配"),
    ("LR.word_form", r"word form|词形"),
    ("LR.unnatural_expression", r"unnatural|不自然|中式表达"),
    ("LR.word_choice", r"word choice|用词|词汇选择"),
    ("LR.repetition", r"lexical.*repetition|词汇重复"),
    ("LR.spelling", r"spelling|拼写"),
    ("LR.precision", r"precision|准确用词|表达宽泛"),
    ("GRA.subject_verb_agreement", r"subject.?verb|主谓一致"),
    ("GRA.sentence_structure", r"sentence structure|句子结构|句法"),
    ("GRA.accuracy", r"grammar accuracy|grammatical error|语法错误|语法准确"),
    ("GRA.preposition", r"preposition|介词"),
    ("GRA.article", r"article|冠词"),
    ("GRA.run_on", r"run.?on|粘连句"),
    ("GRA.fragment", r"fragment|句子残缺"),
    ("GRA.tense", r"tense|时态"),
    ("GRA.punctuation", r"punctuation|标点"),
    ("GRA.plural", r"plural|单复数"),
    ("GRA.pronoun", r"pronoun|代词"),
    ("GRA.complex_sentence", r"complex sentence|复杂句|从句"),
)


def infer_problem_tag(priority: dict[str, Any], error_tags: Iterable[object]) -> str | None:
    """Map new report metadata to one stable tag without consulting corpus scores."""
    taxonomy = load_taxonomy()["tags"]
    supplied = [str(tag).strip() for tag in error_tags if str(tag).strip()]
    for value in supplied:
        if value in taxonomy:
            return value
        normalized = _normalized_label(value)
        for tag in taxonomy:
            if normalized in {_normalized_label(tag), _normalized_label(tag.split(".", 1)[-1])}:
                return tag
    # Titles classify the problem; explanations and actions often mention
    # neighbouring criteria incidentally. Search fields separately.
    for field in ("title", "why", "action"):
        text = str(priority.get(field) or "").casefold()
        if not text:
            continue
        for tag, pattern in _TAG_RULES:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return tag
    return None


def attach_training_target_metadata(structured: dict[str, Any]) -> dict[str, Any]:
    """Attach retrieval metadata only to newly graded reports after validation."""
    enriched = deepcopy(structured)
    priorities = enriched.get("priorities")
    if not isinstance(priorities, list) or not priorities or not isinstance(priorities[0], dict):
        return enriched
    tag = infer_problem_tag(priorities[0], enriched.get("error_tags") or [])
    if tag is None:
        return enriched
    details = load_taxonomy()["tags"][tag]
    priorities[0]["problem_tag"] = tag
    priorities[0]["training_goal"] = details["training_goal"]
    priorities[0]["essay_context"] = details["essay_context"]
    enriched["training_case_metadata_version"] = TRAINING_CASE_METADATA_VERSION
    return enriched


_SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+(?=\s|$)|$)", flags=re.MULTILINE)


def bounded_excerpt(text: str) -> str | None:
    """Return the first contiguous 2-4 sentence window within the public word cap."""
    source = str(text or "").strip()
    matches = [match for match in _SENTENCE_RE.finditer(source) if match.group(0).strip()]
    if len(matches) < MIN_EXCERPT_SENTENCES:
        return None
    chosen = matches[:MAX_EXCERPT_SENTENCES]
    while len(chosen) >= MIN_EXCERPT_SENTENCES:
        excerpt = source[chosen[0].start():chosen[-1].end()].strip()
        if count_words(excerpt) <= MAX_EXCERPT_WORDS:
            return excerpt
        chosen.pop()
    return None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrainingCaseError(f"Invalid training case JSON on line {line_number}.") from exc
        if not isinstance(value, dict):
            raise TrainingCaseError(f"Training case line {line_number} is not an object.")
        records.append(value)
    return records


def _validate_training_record(record: dict[str, Any], taxonomy: dict[str, Any]) -> None:
    if record.get("source") != "kaggle_ielts":
        raise TrainingCaseError("The Kaggle training loader received an unexpected source.")
    if record.get("use_for_score_calibration") is not False:
        raise TrainingCaseError("Kaggle cases must never be enabled for score calibration.")
    if record.get("task_type") != "task2":
        raise TrainingCaseError("Only Task 2 cases can enter this library.")
    tags = record.get("problem_tags")
    if not isinstance(tags, list) or not tags or any(tag not in taxonomy["tags"] for tag in tags):
        raise TrainingCaseError("The training case contains an invalid problem tag.")


def find_one_case(
    problem_tag: str,
    essay_context: str,
    task_type: str = "task2",
    training_goal: str | None = None,
    *,
    library_path: str | Path = DEFAULT_LIBRARY_PATH,
) -> TrainingCaseMatch | None:
    """Return at most one approved, excerpt-only match without using Band metadata."""
    if task_type != "task2":
        return None
    taxonomy = load_taxonomy()
    if problem_tag not in taxonomy["tags"]:
        return None
    ranked: list[tuple[int, str, dict[str, Any], str]] = []
    for record in _load_jsonl(Path(library_path)):
        _validate_training_record(record, taxonomy)
        if record.get("review_status") != "approved" or record.get("use_for_training") is not True:
            continue
        tags = record["problem_tags"]
        if problem_tag not in tags:
            continue
        excerpt = bounded_excerpt(str(record.get("student_excerpt") or ""))
        if excerpt is None:
            continue
        score = 100
        if record.get("essay_context") == essay_context:
            score += 20
        if training_goal and record.get("training_goal") == training_goal:
            score += 10
        score += {"none": 0, "low": 1, "medium": 2, "high": 3}.get(
            str(record.get("human_feedback_quality") or "none"), 0
        )
        ranked.append((score, str(record.get("case_id") or ""), record, excerpt))
    if not ranked:
        return None
    _, _, record, excerpt = sorted(ranked, key=lambda item: (-item[0], item[1]))[0]
    explanation = str(record.get("similarity_explanation") or "").strip()
    question = str(record.get("observation_question") or "").strip()
    if not explanation or len(explanation) > 60 or not question:
        return None
    return TrainingCaseMatch(
        case_id=str(record["case_id"]),
        problem_tag=problem_tag,
        training_goal=str(record.get("training_goal") or taxonomy["tags"][problem_tag]["training_goal"]),
        essay_context=str(record.get("essay_context") or essay_context),
        excerpt=excerpt,
        similarity_explanation=explanation,
        observation_question=question,
    )


def assert_not_scoring_reference(records: Iterable[dict[str, Any]]) -> None:
    """Fail closed if training records are ever passed toward score calibration."""
    for record in records:
        if record.get("source") == "kaggle_ielts" or record.get("use_for_score_calibration") is False:
            raise TrainingCaseError("Training-only Kaggle data cannot be loaded as scoring references.")

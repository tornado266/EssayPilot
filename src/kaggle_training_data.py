"""Deterministic, auditable cleaning for the Kaggle IELTS learner corpus."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import statistics
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from src.text_utils import count_words
from src.training_case_library import bounded_excerpt, load_taxonomy


PIPELINE_VERSION = "kaggle-task2-cleaning-v1"
FIELD_CANDIDATES = {
    "task_type": ("task_type", "tasktype", "writing_task", "type"),
    "question": ("question", "prompt", "topic", "task_question", "question_text"),
    "essay": ("essay", "essay_text", "response", "answer", "candidate_response", "text"),
    "overall": ("overall", "overall_score", "overall_band", "band", "score"),
    "feedback": (
        "examiner_comment", "examiner_comments", "examiner_commen",
        "teacher_feedback", "feedback", "comments", "comment"
    ),
}
TASK2_CUES = re.compile(
    r"\b(discuss both views|agree or disagree|to what extent|give your opinion|"
    r"advantages? and disadvantages?|problems? and solutions?|positive or negative development)\b",
    flags=re.IGNORECASE,
)
TASK1_CUES = re.compile(
    r"\b(graph|chart|table|diagram|map|process)\b|\bsummari[sz]e the information\b",
    flags=re.IGNORECASE,
)
FEEDBACK_HEADING = re.compile(
    r"(?im)^[ \t]*(examiner(?:'s)? comments?|teacher feedback|feedback|"
    r"task response|coherence and cohesion|lexical resource|"
    r"grammatical range and accuracy|overall(?: band)?(?: score)?)[ \t]*[:\-]"
)
HTML_TAG = re.compile(r"<[^>]{1,200}>")


@dataclass(frozen=True)
class SourceRow:
    source_file: str
    row_number: int
    values: dict[str, Any]
    mapping: dict[str, str | None]


def _field_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def detect_field_mapping(columns: Iterable[object]) -> dict[str, str | None]:
    actual = {_field_key(column): str(column) for column in columns}
    mapping: dict[str, str | None] = {}
    for canonical, candidates in FIELD_CANDIDATES.items():
        mapping[canonical] = next((actual[name] for name in candidates if name in actual), None)
    if mapping["essay"] is None:
        raise ValueError("No essay text column could be identified.")
    return mapping


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_manifest(paths: Iterable[Path], source_url: str = "") -> dict[str, Any]:
    files = [
        {"path": str(path.resolve()), "size": path.stat().st_size, "sha256": file_sha256(path)}
        for path in sorted(paths)
    ]
    return {
        "pipeline_version": PIPELINE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_url": source_url,
        "files": files,
    }


def _decode_bytes(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("The dataset text encoding could not be decoded.")


def _iter_tabular(name: str, payload: bytes) -> Iterator[tuple[int, dict[str, Any], list[str]]]:
    suffix = Path(name).suffix.casefold()
    text = _decode_bytes(payload)
    if suffix == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        columns = list(reader.fieldnames or [])
        for index, row in enumerate(reader, 2):
            yield index, dict(row), columns
        return
    if suffix == ".jsonl":
        for index, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{name}:{index} is not a JSON object.")
            yield index, value, list(value)
        return
    if suffix == ".json":
        value = json.loads(text)
        if isinstance(value, dict):
            for key in ("data", "records", "essays"):
                if isinstance(value.get(key), list):
                    value = value[key]
                    break
        if not isinstance(value, list):
            raise ValueError(f"{name} must contain a JSON array or a recognized record list.")
        for index, row in enumerate(value, 1):
            if not isinstance(row, dict):
                raise ValueError(f"{name}:{index} is not a JSON object.")
            yield index, row, list(row)
        return
    raise ValueError(f"Unsupported dataset file: {name}")


def _dataset_files(input_path: str | Path) -> list[Path]:
    path = Path(input_path)
    if path.is_file():
        return [path]
    if path.is_dir():
        files = [
            item for item in path.rglob("*")
            if item.is_file() and item.suffix.casefold() in {".csv", ".json", ".jsonl", ".zip"}
        ]
        if files:
            return sorted(files)
    raise FileNotFoundError(f"No supported dataset files found at {path}.")


def load_source_rows(input_path: str | Path) -> tuple[list[SourceRow], dict[str, Any], list[Path]]:
    rows: list[SourceRow] = []
    profiles: list[dict[str, Any]] = []
    files = _dataset_files(input_path)
    for path in files:
        members: list[tuple[str, bytes]] = []
        if path.suffix.casefold() == ".zip":
            with zipfile.ZipFile(path) as archive:
                members = [
                    (f"{path.name}!{name}", archive.read(name))
                    for name in archive.namelist()
                    if Path(name).suffix.casefold() in {".csv", ".json", ".jsonl"}
                    and not name.endswith("/")
                ]
        else:
            members = [(path.name, path.read_bytes())]
        for source_name, payload in members:
            parsed = list(_iter_tabular(source_name, payload))
            columns = parsed[0][2] if parsed else []
            mapping = detect_field_mapping(columns)
            profiles.append({
                "source_file": source_name,
                "row_count": len(parsed),
                "columns": columns,
                "field_mapping": mapping,
            })
            rows.extend(
                SourceRow(source_name, number, values, mapping)
                for number, values, _ in parsed
            )
    return rows, {"files": profiles, "total_rows": len(rows)}, files


def _value(row: SourceRow, field: str) -> str:
    column = row.mapping.get(field)
    value = row.values.get(column) if column else ""
    if value is None:
        return ""
    return str(value).strip()


def _task_type(task_value: str, question: str) -> str:
    normalized = _field_key(task_value)
    if normalized in {"2", "task_2", "task2", "writing_task_2", "ielts_writing_task_2"}:
        return "task2"
    if normalized in {"1", "task_1", "task1", "writing_task_1", "ielts_writing_task_1"}:
        return "task1"
    if "task_2" in normalized or "task2" in normalized:
        return "task2"
    if "task_1" in normalized or "task1" in normalized:
        return "task1"
    if TASK1_CUES.search(question) and not TASK2_CUES.search(question):
        return "task1"
    if TASK2_CUES.search(question):
        return "task2"
    return "unknown"


def normalize_for_matching(text: str) -> str:
    translation = str.maketrans({
        "’": "'", "‘": "'", "“": '"', "”": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    })
    value = unicodedata.normalize("NFKC", text).translate(translation).casefold()
    return " ".join(value.split())


def _stable_case_id(row: SourceRow, essay: str) -> str:
    material = f"{row.source_file}|{row.row_number}|{normalize_for_matching(essay)}"
    return "kaggle_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _clean_html(text: str) -> tuple[str, bool]:
    if not HTML_TAG.search(text):
        return text, False
    return html.unescape(HTML_TAG.sub("", text)), True


def _remove_exact_question_prefix(essay: str, question: str) -> tuple[str, bool]:
    stripped = essay.lstrip()
    prompt = question.strip()
    if len(prompt) < 40 or not stripped.casefold().startswith(prompt.casefold()):
        return essay, False
    return stripped[len(prompt):].lstrip(" \t\r\n:-"), True


def split_feedback_contamination(essay: str) -> tuple[str, str, str]:
    """Return essay, extracted feedback, and none/extracted/uncertain status."""
    matches = list(FEEDBACK_HEADING.finditer(essay))
    if not matches:
        suspicious = re.search(
            r"\b(examiner comment|overall band|lexical resource|coherence and cohesion)\b",
            essay,
            flags=re.IGNORECASE,
        )
        return essay, "", "uncertain" if suspicious else "none"
    for match in matches:
        prefix = essay[:match.start()].rstrip()
        tail = essay[match.start():].strip()
        heading = match.group(1).casefold()
        tail_heading_count = len(FEEDBACK_HEADING.findall(tail))
        explicit_feedback = any(token in heading for token in ("examiner", "teacher", "feedback"))
        if count_words(prefix) >= 80 and (explicit_feedback or tail_heading_count >= 2):
            return prefix, tail, "extracted"
    return essay, "", "uncertain"


def _english_likelihood(text: str) -> float:
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return 0.0
    ascii_letters = sum(character.isascii() for character in letters)
    return ascii_letters / len(letters)


def _score_value(value: str) -> float | None:
    if not value:
        return None
    match = re.search(r"(?<!\d)([0-9](?:\.[05])?)(?!\d)", value)
    if not match:
        return None
    score = float(match.group(1))
    return score if 0 <= score <= 9 else None


def _feedback_quality(text: str) -> str:
    words = count_words(text)
    specificity = len(re.findall(
        r"\b(idea|develop|example|paragraph|coher|link|vocab|word|grammar|sentence|article|tense)\w*\b",
        text,
        flags=re.IGNORECASE,
    ))
    if words >= 80 and specificity >= 4:
        return "high"
    if words >= 35 and specificity >= 2:
        return "medium"
    if words >= 8:
        return "low"
    return "none"


_FEEDBACK_TAG_PATTERNS: tuple[tuple[str, str], ...] = (
    ("TR.idea_development", r"\b(develop|elaborat|explain).*\bidea|\bsupport.*\bidea|main ideas?|body paragraph.*(?:add|more).*sentences?|展开|论证"),
    ("TR.position_clarity", r"\bposition\b|\bthesis\b|\bopinion\b|立场"),
    ("TR.partial_task_response", r"\b(covers?|address|accomplish).*\btask|\btask (?:response|prompt)\b|partial response|漏答|部分回应"),
    ("TR.example_relevance", r"irrelevant example|example.*relevant|例子.*相关"),
    ("TR.example_development", r"develop.*example|explain.*example|例子.*展开"),
    ("TR.overgeneralization", r"overgeneral|too general|assertive statement|绝对化|以偏概全"),
    ("TR.argument_depth", r"argument.*(?:depth|convinc)|analysis.*shallow|deeper analysis|论证深度"),
    ("TR.conclusion_quality", r"conclusion"),
    ("CC.paragraphing", r"paragraphing|paragraph structure|(?:make|restructure|re-structure).*paragraph|paragraphs?.*(?:smaller|organis|logical)|分段"),
    ("CC.logical_progression", r"\b(?:logical progression|logic|progression)\b|逻辑"),
    ("CC.mechanical_linkers", r"mechanical linker|linking.*(?:mechanical|primitive|forced|not natural)|连接词.*生硬"),
    ("CC.overused_linkers", r"overuse.*link|too many linker|linking.*repetit|连接词.*过多"),
    ("CC.reference_clarity", r"unclear.*refer|not always clear.*refer|\breference\b|指代"),
    ("CC.paragraph_focus", r"paragraph.*focus|topic sentence|段落中心"),
    ("CC.repetition", r"repetit.*idea|content.*repeat|内容重复"),
    ("LR.collocation", r"collocation|word combination|搭配"),
    ("LR.word_choice", r"word choice|inaccurate word|用词"),
    ("LR.word_form", r"word form|词形"),
    ("LR.unnatural_expression", r"unnatural|awkward expression|inaccurate expression|中式|不自然"),
    ("LR.repetition", r"repetit.*(?:word|expression)|vocabulary.*repeat|same expressions|词汇重复"),
    ("LR.spelling", r"spelling|拼写"),
    ("LR.precision", r"precision|precise vocabulary|准确.*词"),
    ("GRA.accuracy", r"\bgrammar\b|\bgrammatical errors?\b|语法错误|语法准确"),
    ("GRA.article", r"\barticle\b|冠词"),
    ("GRA.preposition", r"\bprepositions?\b|介词"),
    ("GRA.subject_verb_agreement", r"subject.?verb agreement|主谓一致"),
    ("GRA.tense", r"tense|时态"),
    ("GRA.sentence_structure", r"sentence structure|句子结构"),
    ("GRA.fragment", r"fragment|残句"),
    ("GRA.run_on", r"run.?on|粘连句"),
    ("GRA.punctuation", r"punctuation|标点"),
    ("GRA.plural", r"plural|singular|单复数"),
    ("GRA.pronoun", r"pronoun|代词"),
)


def feedback_tags(text: str) -> list[str]:
    taxonomy = load_taxonomy()["tags"]
    return [
        tag for tag, pattern in _FEEDBACK_TAG_PATTERNS
        if tag in taxonomy and re.search(pattern, text, flags=re.IGNORECASE)
    ]


def _base_case(row: SourceRow) -> dict[str, Any]:
    question = _value(row, "question")
    original = _value(row, "essay")
    task_type = _task_type(_value(row, "task_type"), question)
    feedback_original = _value(row, "feedback")
    essay, html_removed = _clean_html(original)
    essay, question_prefix_removed = _remove_exact_question_prefix(essay, question)
    essay, feedback_extracted, contamination = split_feedback_contamination(essay)
    essay = essay.strip()
    combined_feedback = "\n\n".join(value for value in (feedback_original, feedback_extracted) if value)
    word_count = count_words(essay)
    warnings: list[str] = []
    if word_count < 250:
        warnings.append("under_250_words")
    if html_removed:
        warnings.append("html_removed")
    if question_prefix_removed:
        warnings.append("question_prefix_removed")
    needs_review = contamination == "uncertain"
    rejection_reasons: list[str] = []
    if not original.strip():
        rejection_reasons.append("empty_essay")
    elif word_count < 30:
        rejection_reasons.append("severely_truncated_or_too_short")
    if essay and _english_likelihood(essay) < 0.70 and word_count < 100:
        rejection_reasons.append("probably_non_english")
    if contamination == "uncertain":
        rejection_reasons.append("uncertain_feedback_boundary")
    status = "rejected" if rejection_reasons else ("modified" if warnings or contamination == "extracted" else "clean")
    tags = feedback_tags(combined_feedback)
    quality = _feedback_quality(combined_feedback)
    return {
        "case_id": _stable_case_id(row, original),
        "source": "kaggle_ielts",
        "source_row": {"file": row.source_file, "row_number": row.row_number},
        "task_type": task_type,
        "question": question,
        "essay_original": original,
        "essay_clean": essay,
        "word_count": word_count,
        "word_count_warning": word_count < 250,
        "original_overall_score": _score_value(_value(row, "overall")),
        "score_confidence": "low",
        "provenance_tier": "examiner_claimed" if feedback_original else "learner_unlabelled",
        "use_for_score_calibration": False,
        "human_feedback_original": feedback_original or None,
        "feedback_extracted": feedback_extracted or None,
        "human_feedback_quality": quality,
        "feedback_tags": tags,
        "model_annotation": None,
        "cleaning_status": status,
        "cleaning_warnings": warnings,
        "rejection_reasons": rejection_reasons,
        "duplicate_status": "unique",
        "duplicate_group_id": None,
        "duplicate_count": 1,
        "possible_near_duplicate": False,
        "near_duplicate_case_ids": [],
        "near_duplicate_max_similarity": 0.0,
        "contamination_status": contamination,
        "needs_review": needs_review or bool(rejection_reasons),
        "training_value": "low",
        "use_for_training": not rejection_reasons,
        "useful_for": [],
        "difficulty": "intermediate",
    }


def _word_shingles(text: str, size: int = 5) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z]+(?:[-'][a-z]+)?", normalize_for_matching(text))
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[index:index + size]) for index in range(len(words) - size + 1)}


def _jaccard(left: set[tuple[str, ...]], right: set[tuple[str, ...]]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _mark_exact_duplicates(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        if case["task_type"] == "task2" and case["cleaning_status"] != "rejected":
            digest = hashlib.sha256(normalize_for_matching(case["essay_clean"]).encode("utf-8")).hexdigest()
            groups[digest].append(case)
    for digest, group in groups.items():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: item["case_id"])
        group_id = "dup_" + digest[:12]
        for index, case in enumerate(ordered):
            case["duplicate_group_id"] = group_id
            case["duplicate_count"] = len(ordered)
            case["duplicate_status"] = "canonical" if index == 0 else "exact_duplicate"
            if index:
                case["cleaning_status"] = "rejected"
                case["use_for_training"] = False
                case["needs_review"] = False
                case["rejection_reasons"].append("exact_duplicate")
    return cases


def _mark_near_duplicates(cases: list[dict[str, Any]]) -> None:
    eligible = [
        case for case in cases
        if case["task_type"] == "task2" and case["cleaning_status"] != "rejected"
    ]
    shingles = {case["case_id"]: _word_shingles(case["essay_clean"]) for case in eligible}
    for index, left in enumerate(eligible):
        for right in eligible[index + 1:]:
            length_ratio = min(left["word_count"], right["word_count"]) / max(left["word_count"], right["word_count"], 1)
            if length_ratio < 0.70:
                continue
            similarity = _jaccard(shingles[left["case_id"]], shingles[right["case_id"]])
            if similarity < 0.80:
                continue
            for case, other in ((left, right), (right, left)):
                case["possible_near_duplicate"] = True
                case["near_duplicate_case_ids"].append(other["case_id"])
                case["near_duplicate_max_similarity"] = max(case["near_duplicate_max_similarity"], round(similarity, 4))
                case["needs_review"] = True


def _training_metadata(case: dict[str, Any]) -> None:
    tags = case["feedback_tags"]
    useful_for = []
    for tag in tags:
        prefix = tag.split(".", 1)[0]
        useful_for.append({"TR": "idea_training", "CC": "coherence_training", "LR": "language_training", "GRA": "sentence_repair"}[prefix])
    case["useful_for"] = sorted(set(useful_for))
    quality_points = {"none": 0, "low": 10, "medium": 25, "high": 50}[case["human_feedback_quality"]]
    points = quality_points + min(20, len(tags) * 5)
    if case["cleaning_status"] == "clean":
        points += 15
    if 220 <= case["word_count"] <= 450:
        points += 10
    if not case["possible_near_duplicate"] and not case["needs_review"]:
        points += 10
    if not case["use_for_training"]:
        points = 0
    case["training_value_score"] = points
    case["training_value"] = "core" if points >= 80 else "high" if points >= 55 else "medium" if points >= 25 else "low"
    case["difficulty"] = "basic" if case["word_count"] < 180 else "advanced" if case["word_count"] > 380 else "intermediate"


def clean_dataset(rows: list[SourceRow], *, candidate_limit: int = 60) -> dict[str, Any]:
    if not 30 <= candidate_limit <= 60:
        raise ValueError("candidate_limit must be between 30 and 60.")
    cases = [_base_case(row) for row in rows]
    _mark_exact_duplicates(cases)
    _mark_near_duplicates(cases)
    for case in cases:
        _training_metadata(case)
    learner = [
        case for case in cases
        if case["task_type"] == "task2" and case["use_for_training"]
    ]
    quarantine = [
        case for case in cases
        if case["task_type"] in {"task2", "unknown"} and not case["use_for_training"]
    ]
    candidates = [
        case for case in learner
        if case["human_feedback_quality"] in {"medium", "high"}
        and case["feedback_tags"]
        and not case["needs_review"]
    ]
    candidates.sort(key=lambda case: (-case["training_value_score"], case["case_id"]))
    return {
        "all_records": cases,
        "clean_task2": learner,
        "learner_corpus": learner,
        "quarantine": quarantine,
        "core_training_case_candidates": candidates[:candidate_limit],
    }


def _distribution(values: list[int]) -> dict[str, float | int | None]:
    if not values:
        return {"min": None, "median": None, "max": None, "mean": None}
    return {
        "min": min(values), "median": statistics.median(values), "max": max(values),
        "mean": round(statistics.fmean(values), 2),
    }


def build_cleaning_report(result: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    cases = result["all_records"]
    task2 = [case for case in cases if case["task_type"] == "task2"]
    learner = result["learner_corpus"]
    quarantine = result["quarantine"]
    tags = Counter(tag for case in learner for tag in case["feedback_tags"])
    scores = Counter(
        str(case["original_overall_score"])
        for case in task2 if case["original_overall_score"] is not None
    )
    report = {
        "pipeline_version": PIPELINE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_rows": len(cases),
        "task2_original": len(task2),
        "excluded_task1": sum(case["task_type"] == "task1" for case in cases),
        "unknown_task_type": sum(case["task_type"] == "unknown" for case in cases),
        "clean": len(learner),
        "exact_duplicates": sum(case["duplicate_status"] == "exact_duplicate" for case in task2),
        "near_duplicate_suspects": sum(case["possible_near_duplicate"] for case in learner),
        "contaminated_extracted": sum(case["contamination_status"] == "extracted" for case in task2),
        "contamination_uncertain": sum(case["contamination_status"] == "uncertain" for case in task2),
        "rejected_or_quarantine": len(quarantine),
        "human_feedback_cases": sum(case["human_feedback_quality"] != "none" for case in task2),
        "high_quality_human_feedback_cases": sum(case["human_feedback_quality"] == "high" for case in task2),
        "core_case_candidates": len(result["core_training_case_candidates"]),
        "learner_corpus": len(learner),
        "word_count_distribution": _distribution([case["word_count"] for case in task2]),
        "score_distribution_metadata_only": dict(sorted(scores.items())),
        "feedback_tag_mentions_not_gold": dict(tags.most_common(20)),
        "rejection_reasons": dict(Counter(reason for case in quarantine for reason in case["rejection_reasons"])),
        "profile": profile,
        "invariants": {
            "all_kaggle_score_calibration_flags_false": all(case["use_for_score_calibration"] is False for case in cases),
            "human_feedback_separate_from_model_annotation": all(
                case["model_annotation"] is None for case in cases
            ),
        },
    }
    return report


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _candidate_markdown(candidates: list[dict[str, Any]]) -> str:
    lines = ["# Kaggle Task 2 Core Training Case Candidates", "", "> 候选案例尚未经过人工批准，不能作为 Gold cases。", ""]
    for index, case in enumerate(candidates, 1):
        feedback = str(case.get("human_feedback_original") or case.get("feedback_extracted") or "")
        lines.extend([
            f"## {index}. {case['case_id']}", "",
            f"- Question: {case['question']}",
            f"- Original score metadata: {case['original_overall_score']}",
            f"- Word count: {case['word_count']}",
            f"- Detected tags: {', '.join(case['feedback_tags'])}",
            f"- Why useful: 反馈质量为 {case['human_feedback_quality']}，可训练标签明确。",
            f"- Possible concerns: {'near duplicate / manual review' if case['needs_review'] else 'none detected'}",
            "",
            "Feedback:", "", feedback[:1200] or "(none)", "",
        ])
    return "\n".join(lines).strip() + "\n"


def _audit_markdown(report: dict[str, Any], result: dict[str, Any]) -> str:
    keys = (
        "task2_original", "clean", "exact_duplicates", "near_duplicate_suspects",
        "contaminated_extracted", "contamination_uncertain", "rejected_or_quarantine",
        "human_feedback_cases", "high_quality_human_feedback_cases", "core_case_candidates",
        "learner_corpus",
    )
    lines = ["# Kaggle IELTS Task 2 Data Audit", "", f"Pipeline: `{report['pipeline_version']}`", ""]
    lines.extend(f"- {key}: {report[key]}" for key in keys)
    lines.extend(["", "## Major contamination and rejection patterns", ""])
    lines.extend(f"- {reason}: {count}" for reason, count in report["rejection_reasons"].items())
    lines.extend(["", "## Comment tag mentions (not weakness gold labels)", ""])
    lines.extend(
        f"- {tag}: {count}"
        for tag, count in report["feedback_tag_mentions_not_gold"].items()
    )
    lines.extend(["", "## Ten retained examples", ""])
    for case in result["learner_corpus"][:10]:
        lines.append(f"- `{case['case_id']}` — clean learner response; tags: {', '.join(case['feedback_tags']) or 'none'}")
    lines.extend(["", "## Ten quarantined examples", ""])
    for case in result["quarantine"][:10]:
        lines.append(f"- `{case['case_id']}` — {', '.join(case['rejection_reasons']) or 'manual review'}")
    lines.extend([
        "", "## Safety conclusion", "",
        "Kaggle scores remain low-confidence metadata and are not available to the scoring-reference loader.", "",
    ])
    return "\n".join(lines)


def build_core_training_cases(
    candidates: list[dict[str, Any]], approved_case_ids: set[str]
) -> list[dict[str, Any]]:
    taxonomy = load_taxonomy()["tags"]
    output: list[dict[str, Any]] = []
    for case in candidates:
        if case["case_id"] not in approved_case_ids:
            continue
        tag = case["feedback_tags"][0]
        details = taxonomy[tag]
        excerpt = bounded_excerpt(case["essay_clean"])
        if excerpt is None:
            continue
        output.append({
            "case_id": case["case_id"],
            "source": "kaggle_ielts",
            "task_type": "task2",
            "problem_tags": case["feedback_tags"],
            "training_goal": details["training_goal"],
            "essay_context": details["essay_context"],
            "student_excerpt": excerpt,
            "similarity_explanation": "这段文字呈现了与你当前首要问题相似的写作模式。",
            "observation_question": details["observation_question"],
            "human_feedback_quality": case["human_feedback_quality"],
            "review_status": "approved",
            "training_value": "core",
            "use_for_training": True,
            "score_confidence": "low",
            "use_for_score_calibration": False,
            "training_sequence": [
                {"type": "identify", "instruction": details["observation_question"]},
                {"type": "diagnose", "instruction": "说明这个问题为什么会削弱表达。"},
                {"type": "guided_revision", "instruction": "只修改这一处并保持原意。"},
                {"type": "transfer_to_own_essay", "instruction": "回到自己的原句或原段落完成修改。"},
            ],
        })
    return output


def write_outputs(
    output_dir: str | Path,
    result: dict[str, Any],
    profile: dict[str, Any],
    manifest: dict[str, Any],
    *,
    approved_case_ids: set[str] | None = None,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = build_cleaning_report(result, profile)
    core_cases = build_core_training_cases(
        result["core_training_case_candidates"], approved_case_ids or set()
    )
    _write_jsonl(output / "clean_task2.jsonl", result["clean_task2"])
    _write_jsonl(output / "learner_corpus.jsonl", result["learner_corpus"])
    _write_jsonl(output / "quarantine.jsonl", result["quarantine"])
    _write_jsonl(output / "core_training_case_candidates.jsonl", result["core_training_case_candidates"])
    _write_jsonl(output / "core_training_cases.jsonl", core_cases)
    (output / "cleaning_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "source_profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "TOP_CORE_CASE_CANDIDATES.md").write_text(
        _candidate_markdown(result["core_training_case_candidates"]), encoding="utf-8"
    )
    (output / "KAGGLE_DATA_AUDIT.md").write_text(_audit_markdown(report, result), encoding="utf-8")
    return {**report, "core_training_cases": len(core_cases)}

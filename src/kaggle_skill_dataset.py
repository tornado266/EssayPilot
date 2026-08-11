"""Leakage-safe dataset splits and weak-label extraction for IELTS Skill work."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from src.kaggle_training_data import feedback_tags, normalize_for_matching
from src.text_utils import count_paragraphs, count_words
from src.training_case_library import load_taxonomy


SPLIT_VERSION = "examiner-claimed-group-stratified-v1"
TARGET_QUOTAS: dict[str, dict[int, int]] = {
    "development": {5: 5, 6: 16, 7: 12, 8: 9},
    "validation": {5: 1, 6: 3, 7: 2, 8: 2},
    "holdout": {5: 2, 6: 5, 7: 3, 8: 2},
}
SPLIT_ORDER = ("development", "validation", "holdout")
NEGATIVE_CUES = re.compile(
    r"\b(however|but|although|lack|limited|weak|error|incorrect|inaccurate|"
    r"unclear|not |needs? (?:to|work)|should|could (?:be|have)|fails?|problem|overuse|repetit)\w*\b",
    flags=re.IGNORECASE,
)
POSITIVE_CUES = re.compile(
    r"\b(?:clear(?:ly)?|well|good|fine|relevant|sufficient(?:ly)?|effective(?:ly)?|"
    r"accurate(?:ly)?|appropriate(?:ly)?|correct(?:ly)?|fully|logical(?:ly)?|"
    r"coherent(?:ly)?|flexible|strong|error-free)\b|\b(?:wide|broad) range\b",
    flags=re.IGNORECASE,
)
ABSENT_LIMITATION_CUES = re.compile(
    r"\b(?:no|without|hardly any|very few|essentially no)\s+(?:\w+\s+){0,3}"
    r"(?:errors?|problems?|mistakes?|limitations?)\b|"
    r"\b(?:errors?|problems?|mistakes?)\s+(?:are\s+)?(?:rare|absent)\b",
    flags=re.IGNORECASE,
)
ACTION_CUES = re.compile(
    r"\b(?:must|should|need(?:s)? to|has to|be careful|requires?|correct(?:ed)? by|"
    r"improve(?:d)?|re-?structure|add(?:ing)?\b)\b",
    flags=re.IGNORECASE,
)
RECURRING_CUES = re.compile(
    r"\b(?:recur|frequent|throughout|repeated|repetitive|many|several|not always)\w*\b",
    flags=re.IGNORECASE,
)
SOFT_CUES = re.compile(r"\b(?:occasional|some|at times?|a little|minor)\w*\b", flags=re.IGNORECASE)
SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+(?=\s|$)|$)", flags=re.MULTILINE)
CLAUSE_SPLIT_RE = re.compile(r"\s*(?:,?\s+however\b|,?\s+although\b|,?\s+but\b|;)\s*", flags=re.IGNORECASE)


class HoldoutAccessError(PermissionError):
    """Raised when ordinary development code attempts to read locked data."""


def _feedback_text(case: dict[str, Any]) -> str:
    return "\n\n".join(
        str(value).strip()
        for value in (case.get("human_feedback_original"), case.get("feedback_extracted"))
        if str(value or "").strip()
    )


def _overall_band(case: dict[str, Any]) -> int | None:
    value = case.get("original_overall_score")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value).is_integer():
        result = int(value)
        return result if result in {5, 6, 7, 8} else None
    return None


def _group_key(case: dict[str, Any]) -> str:
    question = normalize_for_matching(str(case.get("question") or ""))
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]


def examiner_claimed_cases(cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the clean, separately-commented records before any Skill sees them."""
    selected = [
        case for case in cases
        if case.get("task_type") == "task2"
        and case.get("provenance_tier") == "examiner_claimed"
        and _feedback_text(case)
        and _overall_band(case) is not None
        and case.get("duplicate_status") != "exact_duplicate"
    ]
    return sorted(selected, key=lambda case: str(case.get("case_id") or ""))


def _group_cases(cases: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    parent = {str(case["case_id"]): str(case["case_id"]) for case in cases}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_question: dict[str, list[str]] = defaultdict(list)
    valid_ids = set(parent)
    for case in cases:
        case_id = str(case["case_id"])
        by_question[_group_key(case)].append(case_id)
        for other in case.get("near_duplicate_case_ids") or []:
            if str(other) in valid_ids:
                union(case_id, str(other))
    for ids in by_question.values():
        for other in ids[1:]:
            union(ids[0], other)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        groups[find(str(case["case_id"]))].append(case)
    return [sorted(group, key=lambda case: case["case_id"]) for _, group in sorted(groups.items())]


def _group_band_counts(group: list[dict[str, Any]]) -> Counter[int]:
    return Counter(_overall_band(case) for case in group)


def _feedback_eval_eligible(case: dict[str, Any]) -> bool:
    return bool(structure_examiner_feedback(case)["weakness_tags"])


def _exact_group_assignment(groups: list[list[dict[str, Any]]]) -> dict[str, str] | None:
    multi = [group for group in groups if len(group) > 1]
    singles = [group for group in groups if len(group) == 1]
    multi.sort(key=lambda group: (-len(group), _group_key(group[0])))
    remaining = {split: dict(quota) for split, quota in TARGET_QUOTAS.items()}
    assignment: dict[str, str] = {}

    def can_fill_with_singles() -> bool:
        available = Counter(_overall_band(group[0]) for group in singles)
        eligible = Counter(
            _overall_band(group[0]) for group in singles if _feedback_eval_eligible(group[0])
        )
        required = Counter()
        for split in SPLIT_ORDER:
            required.update(remaining[split])
        return (
            all(required[band] == available[band] for band in available | required)
            and all(
                remaining["validation"][band] + remaining["holdout"][band] <= eligible[band]
                for band in available | required
            )
        )

    def visit(index: int) -> bool:
        if index == len(multi):
            return can_fill_with_singles()
        group = multi[index]
        counts = _group_band_counts(group)
        order = sorted(
            SPLIT_ORDER,
            key=lambda split: hashlib.sha256(f"{_group_key(group[0])}|{split}".encode()).hexdigest(),
        )
        for split in order:
            if split in {"validation", "holdout"} and not all(
                _feedback_eval_eligible(case) for case in group
            ):
                continue
            if any(remaining[split][band] < count for band, count in counts.items()):
                continue
            for band, count in counts.items():
                remaining[split][band] -= count
            if visit(index + 1):
                for case in group:
                    assignment[str(case["case_id"])] = split
                return True
            for band, count in counts.items():
                remaining[split][band] += count
        return False

    if not visit(0):
        return None
    single_by_band: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for group in singles:
        single_by_band[_overall_band(group[0])].append(group[0])
    for band, cases in single_by_band.items():
        ordered = sorted(cases, key=lambda case: hashlib.sha256(str(case["case_id"]).encode()).hexdigest())
        available = list(ordered)
        for split in ("holdout", "validation"):
            count = remaining[split][band]
            eligible = [case for case in available if _feedback_eval_eligible(case)]
            chosen = eligible[:count]
            if len(chosen) != count:
                return None
            for case in chosen:
                assignment[str(case["case_id"])] = split
                available.remove(case)
        if len(available) != remaining["development"][band]:
            return None
        for case in available:
            assignment[str(case["case_id"])] = "development"
    return assignment


def split_examiner_claimed(cases: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    selected = examiner_claimed_cases(cases)
    expected = sum(sum(quota.values()) for quota in TARGET_QUOTAS.values())
    if len(selected) != expected:
        raise ValueError(f"Expected {expected} clean examiner-claimed cases, found {len(selected)}.")
    groups = _group_cases(selected)
    assignment = _exact_group_assignment(groups)
    if assignment is None:
        raise ValueError("The fixed group-stratified 42/8/12 split is infeasible for this dataset version.")
    output = {split: [] for split in SPLIT_ORDER}
    for case in selected:
        split = assignment[str(case["case_id"])]
        record = dict(case)
        record["dataset_split"] = split
        output[split].append(record)
    for split in SPLIT_ORDER:
        output[split].sort(key=lambda case: case["case_id"])
        actual = Counter(_overall_band(case) for case in output[split])
        if dict(actual) != TARGET_QUOTAS[split]:
            raise AssertionError(f"Unexpected {split} score distribution: {dict(actual)}")
    return output


def frozen_split_from_manifest(
    cases: Iterable[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Reproduce an existing split by ID and content hash, never by current labels."""
    selected = {str(case["case_id"]): case for case in examiner_claimed_cases(cases)}
    output = {split: [] for split in SPLIT_ORDER}
    seen: set[str] = set()
    for split in SPLIT_ORDER:
        section = (manifest.get("splits") or {}).get(split) or {}
        records = section.get("records") or []
        expected_count = sum(TARGET_QUOTAS[split].values())
        if len(records) != expected_count or int(section.get("count") or 0) != expected_count:
            raise ValueError(f"Frozen {split} manifest count is invalid.")
        for frozen in records:
            case_id = str(frozen.get("case_id") or "")
            case = selected.get(case_id)
            if case is None or case_id in seen:
                raise ValueError("Frozen split contains a missing or repeated case ID.")
            actual_hash = hashlib.sha256(
                normalize_for_matching(str(case.get("essay_clean") or "")).encode("utf-8")
            ).hexdigest()
            if actual_hash != frozen.get("content_sha256"):
                raise ValueError(f"Frozen split content hash changed for {case_id}.")
            record = dict(case)
            record["dataset_split"] = split
            output[split].append(record)
            seen.add(case_id)
    if seen != set(selected):
        raise ValueError("Frozen split does not cover all examiner-claimed records exactly once.")
    return output


def structure_examiner_feedback(case: dict[str, Any]) -> dict[str, Any]:
    """Create weak structured labels without changing or fabricating human feedback."""
    feedback = _feedback_text(case)
    if not feedback:
        raise ValueError("A human examiner-claimed comment is required.")
    strengths: list[str] = []
    weaknesses: list[str] = []
    evidence: list[dict[str, str]] = []
    first_weak_position: dict[str, int] = {}
    weakness_priority: dict[str, int] = {}
    position = 0
    scan_feedback = re.sub(
        r"\b(?:e\.g|i\.e)\.",
        lambda match: match.group(0).replace(".", "·"),
        feedback,
        flags=re.IGNORECASE,
    )
    for match in SENTENCE_RE.finditer(scan_feedback):
        sentence = feedback[match.start():match.end()].strip()
        sentence_tags = feedback_tags(sentence)
        for clause in (part.strip(" ,") for part in CLAUSE_SPLIT_RE.split(sentence)):
            if not clause:
                continue
            tags = feedback_tags(clause)
            negative = bool(NEGATIVE_CUES.search(clause))
            absent_limitation = bool(ABSENT_LIMITATION_CUES.search(clause))
            if not tags and negative and len(sentence_tags) == 1:
                # Preserve the subject of a contrast such as "the main ideas are
                # relevant, but not all of them are developed well enough".
                tags = list(sentence_tags)
            if not tags:
                position += 1
                continue
            positive = bool(POSITIVE_CUES.search(clause))
            for tag in tags:
                if (negative and not absent_limitation) or (not positive and not absent_limitation):
                    if tag not in weaknesses:
                        weaknesses.append(tag)
                        first_weak_position[tag] = position
                    priority = 10
                    priority += 12 if ACTION_CUES.search(clause) else 0
                    priority += 6 if RECURRING_CUES.search(clause) else 0
                    priority -= 2 if SOFT_CUES.search(clause) else 0
                    weakness_priority[tag] = max(weakness_priority.get(tag, 0), priority)
                    kind = "weakness"
                else:
                    if tag not in strengths:
                        strengths.append(tag)
                    kind = "strength"
                evidence.append({"tag": tag, "kind": kind, "comment_excerpt": clause})
            position += 1
    impact_priority = {
        "TR.idea_development": 6,
        "TR.partial_task_response": 6,
        "TR.position_clarity": 5,
        "TR.argument_depth": 5,
        "CC.logical_progression": 5,
        "CC.paragraphing": 4,
        "GRA.accuracy": 3,
    }
    priorities = sorted(
        weaknesses,
        key=lambda tag: (
            -(weakness_priority.get(tag, 0) + impact_priority.get(tag, 0)),
            first_weak_position[tag],
            tag,
        ),
    )[:2]
    taxonomy = load_taxonomy()["tags"]
    return {
        "case_id": case["case_id"],
        "strength_tags": strengths,
        "weakness_tags": weaknesses,
        "priority_tags": priorities,
        "feedback_evidence": evidence,
        "recommended_action": [taxonomy[tag]["training_goal"] for tag in priorities],
        "provenance_tier": str(case.get("provenance_tier") or "examiner_claimed"),
        "label_method": "deterministic-comment-structure-v2",
    }


def load_official_verified(path: str | Path) -> list[dict[str, Any]]:
    """Load the existing private official corpus without reclassifying it as blind."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise ValueError("The official Task 2 dataset is invalid.")
    output: list[dict[str, Any]] = []
    for case in cases:
        model_input = case.get("model_input") or {}
        evaluation = case.get("evaluation") or {}
        if not isinstance(model_input, dict) or not isinstance(evaluation, dict):
            raise ValueError("An official case is malformed.")
        output.append({
            "case_id": str(evaluation.get("case_id") or ""),
            "source": "official_ielts",
            "provenance_tier": "official_verified",
            "task_type": "task2",
            "question": str(model_input.get("task_prompt") or ""),
            "essay_clean": str(model_input.get("candidate_response") or ""),
            "human_feedback_original": str(evaluation.get("examiner_comment") or ""),
            "expected_overall": evaluation.get("expected_overall"),
            "source_reference": str(evaluation.get("source_reference") or ""),
        })
    if any(not case["case_id"] or not case["essay_clean"] or not case["human_feedback_original"] for case in output):
        raise ValueError("Every official case needs an ID, essay, and examiner comment.")
    return output


def build_skill_rule_audit(
    official_cases: list[dict[str, Any]],
    development_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate abstract support counts; never emit essays, labels, or comment text."""
    official_labels = [structure_examiner_feedback(case) for case in official_cases]
    claimed_labels = [structure_examiner_feedback(case) for case in development_cases]
    official_strengths = Counter(tag for label in official_labels for tag in label["strength_tags"])
    official_weaknesses = Counter(tag for label in official_labels for tag in label["weakness_tags"])
    claimed_strengths = Counter(tag for label in claimed_labels for tag in label["strength_tags"])
    claimed_weaknesses = Counter(tag for label in claimed_labels for tag in label["weakness_tags"])
    taxonomy = load_taxonomy()["tags"]
    accepted: list[dict[str, Any]] = []
    for tag in taxonomy:
        official_support = official_strengths[tag] + official_weaknesses[tag]
        claimed_support = claimed_strengths[tag] + claimed_weaknesses[tag]
        if official_support or claimed_support >= 3:
            accepted.append({
                "problem_tag": tag,
                "official_support": official_support,
                "examiner_claimed_development_support": claimed_support,
                "authority": "official_supported" if official_support else "coverage_only",
                "training_goal": taxonomy[tag]["training_goal"],
            })
    eligible = 0
    exclusion_reasons: Counter[str] = Counter()
    for case, label in zip(development_cases, claimed_labels, strict=True):
        allowed, reasons = scoring_skill_eligibility(case, label)
        eligible += int(allowed)
        exclusion_reasons.update(reasons)
    return {
        "official_verified_cases": len(official_cases),
        "examiner_claimed_development_cases": len(development_cases),
        "scoring_skill_eligible_claimed_cases": eligible,
        "scoring_skill_exclusion_reasons": dict(exclusion_reasons),
        "accepted_abstract_rules": accepted,
        "invariants": {
            "contains_essay_text": False,
            "contains_examiner_comment_text": False,
            "kaggle_can_override_official": False,
            "kaggle_can_define_criterion_bands": False,
        },
    }


def prediction_priority_tags(structured_report: dict[str, Any]) -> list[str]:
    """Map at most two produced priorities to the stable evaluation taxonomy."""
    from src.training_case_library import infer_problem_tag

    output: list[str] = []
    priorities = structured_report.get("priorities") or []
    for priority in priorities[:2]:
        if not isinstance(priority, dict):
            continue
        tag = infer_problem_tag(priority, [])
        if tag and tag not in output:
            output.append(tag)
    return output


def scoring_skill_eligibility(case: dict[str, Any], structured: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    feedback = _feedback_text(case)
    prefixes = {tag.split(".", 1)[0] for tag in structured["strength_tags"] + structured["weakness_tags"]}
    if case.get("cleaning_status") == "rejected" or case.get("needs_review"):
        reasons.append("unclean_or_review_required")
    if case.get("possible_near_duplicate"):
        reasons.append("near_duplicate")
    if _overall_band(case) is None:
        reasons.append("missing_integer_overall")
    if count_words(feedback) < 35:
        reasons.append("feedback_under_35_words")
    if len(prefixes) < 2:
        reasons.append("feedback_covers_fewer_than_two_dimensions")
    score = _overall_band(case)
    if score is not None:
        if score <= 5 and structured["strength_tags"] and not structured["weakness_tags"]:
            reasons.append("comment_score_contradiction")
        if score >= 8 and structured["weakness_tags"] and not structured["strength_tags"]:
            reasons.append("comment_score_contradiction")
    return not reasons, reasons


def public_split_manifest(splits: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    manifest: dict[str, Any] = {"split_version": SPLIT_VERSION, "provenance_tier": "examiner_claimed", "splits": {}}
    for split, cases in splits.items():
        manifest["splits"][split] = {
            "count": len(cases),
            "score_distribution": dict(sorted(Counter(str(_overall_band(case)) for case in cases).items())),
            "records": [
                {
                    "case_id": case["case_id"],
                    "content_sha256": hashlib.sha256(
                        normalize_for_matching(str(case["essay_clean"])).encode("utf-8")
                    ).hexdigest(),
                }
                for case in cases
            ],
        }
    return manifest


def learner_coverage_profile(cases: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Use every unlabelled essay for local coverage without creating fake gold labels."""
    selected = [
        case for case in cases
        if case.get("task_type") == "task2"
        and case.get("provenance_tier") == "learner_unlabelled"
        and case.get("use_for_training") is True
    ]
    signal_counts: Counter[str] = Counter()
    scored: list[tuple[int, str, dict[str, Any], list[str]]] = []
    linker_pattern = re.compile(
        r"\b(firstly|secondly|moreover|furthermore|however|therefore|in conclusion)\b",
        flags=re.IGNORECASE,
    )
    for case in selected:
        essay = str(case.get("essay_clean") or "")
        signals: list[str] = []
        words = count_words(essay)
        paragraphs = count_paragraphs(essay)
        if words < 250:
            signals.append("under_250_words")
        if paragraphs <= 1:
            signals.append("single_paragraph")
        sentence_lengths = [count_words(match.group(0)) for match in SENTENCE_RE.finditer(essay)]
        if sentence_lengths and max(sentence_lengths) >= 45:
            signals.append("very_long_sentence")
        linkers = Counter(match.group(1).casefold() for match in linker_pattern.finditer(essay))
        if any(count >= 3 for count in linkers.values()):
            signals.append("repeated_explicit_linker")
        if not signals:
            signals.append("no_simple_stress_signal")
        signal_counts.update(signals)
        risk = len([signal for signal in signals if signal != "no_simple_stress_signal"])
        scored.append((risk, str(case["case_id"]), case, signals))
    candidates: list[dict[str, Any]] = []
    seen_signatures: Counter[tuple[str, ...]] = Counter()
    for _, _, case, signals in sorted(scored, key=lambda item: (-item[0], item[1])):
        signature = tuple(sorted(signals))
        if seen_signatures[signature] >= 5:
            continue
        seen_signatures[signature] += 1
        record = dict(case)
        record["stress_signals"] = list(signature)
        record["model_annotation"] = None
        candidates.append(record)
        if len(candidates) == 20:
            break
    return {
        "corpus_size": len(selected),
        "signal_counts": dict(signal_counts),
        "paid_annotation_cap": 20,
        "model_annotations_are_gold": False,
        "all_records_used_for_local_profile": True,
    }, candidates


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_skill_splits(
    splits: dict[str, list[dict[str, Any]]],
    *,
    processed_dir: str | Path,
    private_dir: str | Path,
    public_manifest_path: str | Path,
    learner_unlabelled: Iterable[dict[str, Any]] = (),
) -> None:
    processed = Path(processed_dir)
    private = Path(private_dir)
    for split in ("development", "validation"):
        records = []
        for case in splits[split]:
            record = dict(case)
            structured = structure_examiner_feedback(case)
            eligible, reasons = scoring_skill_eligibility(case, structured)
            record["structured_examiner_feedback"] = structured
            record["score_skill_eligible"] = eligible
            record["score_skill_exclusion_reasons"] = reasons
            records.append(record)
        _write_jsonl(processed / f"examiner_claimed_{split}.jsonl", records)
    holdout_records = []
    for case in splits["holdout"]:
        record = dict(case)
        record["structured_examiner_feedback"] = structure_examiner_feedback(case)
        holdout_records.append(record)
    _write_jsonl(private / "examiner_claimed_holdout.jsonl", holdout_records)
    unlabelled_records = [
        case for case in learner_unlabelled
        if case.get("task_type") == "task2"
        and case.get("provenance_tier") == "learner_unlabelled"
        and case.get("use_for_training") is True
    ]
    _write_jsonl(
        processed / "learner_unlabelled.jsonl",
        unlabelled_records,
    )
    coverage, annotation_candidates = learner_coverage_profile(unlabelled_records)
    (processed / "learner_corpus_coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_jsonl(processed / "model_annotation_candidates.jsonl", annotation_candidates)
    Path(public_manifest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(public_manifest_path).write_text(
        json.dumps(public_split_manifest(splits), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_skill_split(
    path: str | Path,
    *,
    split: str,
    unlock_holdout: bool = False,
) -> list[dict[str, Any]]:
    if split == "holdout" and not unlock_holdout:
        raise HoldoutAccessError("The Kaggle holdout is locked outside the explicit final evaluator.")
    if split not in SPLIT_ORDER:
        raise ValueError("split must be development, validation, or holdout.")
    return [
        json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def feedback_metrics(gold: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    def supported(predicted_tag: str, expected_tags: set[str]) -> bool:
        if predicted_tag in expected_tags:
            return True
        if "GRA.accuracy" in expected_tags and predicted_tag.startswith("GRA."):
            return True
        if "LR.word_choice" in expected_tags and predicted_tag in {
            "LR.collocation", "LR.precision", "LR.unnatural_expression", "LR.word_form"
        }:
            return True
        return False

    by_id = {str(item["case_id"]): item for item in predictions}
    top1_hits = 0
    top2_hits = 0
    unsupported_cases = 0
    valid = 0
    for expected in gold:
        predicted = by_id.get(str(expected["case_id"]))
        if not predicted:
            continue
        valid += 1
        predicted_tags = list(predicted.get("priority_tags") or [])[:2]
        gold_priority = set(expected.get("priority_tags") or [])
        gold_weaknesses = set(expected.get("weakness_tags") or [])
        if predicted_tags and supported(predicted_tags[0], gold_priority):
            top1_hits += 1
        if any(supported(tag, gold_priority) for tag in predicted_tags):
            top2_hits += 1
        if any(not supported(tag, gold_weaknesses) for tag in predicted_tags):
            unsupported_cases += 1
    total = len(gold)
    return {
        "cases": total,
        "valid_predictions": valid,
        "top1_hits": top1_hits,
        "top2_hits": top2_hits,
        "unsupported_cases": unsupported_cases,
        "schema_valid_rate": valid / total if total else 0.0,
        "passed": total == 12 and valid == 12 and top1_hits >= 10 and top2_hits >= 11 and unsupported_cases <= 1,
    }


def blind_scoring_input(case: dict[str, Any]) -> dict[str, str]:
    """Project a holdout record to the only fields the scoring model may receive."""
    return {
        "task_type": "Task 2",
        "topic": str(case.get("question") or ""),
        "essay": str(case.get("essay_clean") or ""),
    }


def weak_scoring_metrics(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(item["case_id"]): item for item in predictions if item.get("status") == "complete"}
    errors: list[float] = []
    signed: list[float] = []
    for case in cases:
        prediction = by_id.get(str(case["case_id"]))
        expected = case.get("original_overall_score")
        if prediction is None or not isinstance(expected, (int, float)):
            continue
        predicted = float(prediction["predicted_overall"])
        errors.append(abs(predicted - float(expected)))
        signed.append(predicted - float(expected))
    valid = len(errors)
    return {
        "cases": len(cases),
        "valid_predictions": valid,
        "mae": sum(errors) / valid if valid else None,
        "within_0_5": sum(error <= 0.5 for error in errors),
        "within_1_0": sum(error <= 1.0 for error in errors),
        "mean_signed_bias": sum(signed) / valid if valid else None,
        "secondary_low_confidence_only": True,
        "combined_with_official_metrics": False,
    }

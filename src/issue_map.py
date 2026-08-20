"""Pure helpers for the report problem map and its replacement-word branches."""

from __future__ import annotations

import difflib
import html
import re
from collections.abc import Mapping

from src.learning_assets import infer_category
from src.problem_spans import correction_problem_ranges, highlight_problem_text


CRITERION_ORDER = ("TR", "CC", "LR", "GRA")
CRITERION_LABELS = {
    "TR": "任务回应",
    "CC": "连贯与衔接",
    "LR": "词汇资源",
    "GRA": "语法准确性",
}
_CATEGORY_TO_CRITERION = {
    "task_response": "TR",
    "coherence": "CC",
    "vocabulary": "LR",
    "expression": "LR",
    "grammar": "GRA",
}
_LEGACY_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_FUNCTION_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had", "to", "of", "for", "in",
    "on", "at", "by", "from", "with", "and", "or", "but", "that", "this",
    "these", "those", "it", "its", "should", "would", "could", "can", "may",
    "might", "must",
}


def _legacy_lexical_pair(correction: Mapping[str, object]) -> tuple[str, str] | None:
    """Pair source and target from one diff opcode; reject grammar/spelling-only edits."""
    before = _LEGACY_WORD.findall(str(correction.get("original") or ""))
    after = _LEGACY_WORD.findall(str(correction.get("improved") or ""))
    if not before or not after:
        return None
    raw_spans = correction.get("problem_spans")
    spans = (
        [str(value).casefold() for value in raw_spans if str(value).strip()]
        if isinstance(raw_spans, list) else []
    )
    matcher = difflib.SequenceMatcher(
        a=[word.casefold() for word in before],
        b=[word.casefold() for word in after],
        autojunk=False,
    )
    candidates: list[tuple[int, int, int, str, str]] = []
    for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
        if opcode != "replace" or i2 <= i1 or j2 <= j1:
            continue
        source_words = before[i1:i2]
        target_words = after[j1:j2]
        if len(source_words) > 4 or len(target_words) > 4:
            continue
        source = " ".join(source_words)
        target = " ".join(target_words)
        folded_source = source.casefold()
        folded_target = target.casefold()
        if (
            all(word.casefold() in _FUNCTION_WORDS for word in source_words)
            and all(word.casefold() in _FUNCTION_WORDS for word in target_words)
        ):
            continue
        if len(source_words) == len(target_words) == 1 and difflib.SequenceMatcher(
            None, folded_source, folded_target, autojunk=False,
        ).ratio() >= 0.72:
            continue
        span_match = int(any(
            folded_source in span or span in folded_source for span in spans
        ))
        candidates.append((span_match, -len(target_words), -len(target), source, target))
    if not candidates:
        return None
    _span_match, _word_score, _length_score, source, target = max(candidates)
    return source, target


def correction_criterion(correction: Mapping[str, object]) -> str:
    """Return a structured criterion, with a safe legacy-report fallback."""
    explicit = str(correction.get("criterion") or "").upper()
    if explicit in CRITERION_ORDER:
        return explicit
    category = infer_category(str(correction.get("problem") or ""))
    return _CATEGORY_TO_CRITERION.get(category, "GRA")


def correction_issue_type(correction: Mapping[str, object]) -> str:
    """Return the model subtype or a concise deterministic legacy label."""
    explicit = " ".join(str(correction.get("issue_type") or "").split())
    if explicit:
        return explicit
    problem = str(correction.get("problem") or "")
    rules = (
        (r"主谓|subject.?verb|agreement", "主谓一致"),
        (r"单复数|plural|singular|不可数", "名词单复数"),
        (r"搭配|collocation", "搭配不自然"),
        (r"用词|词汇|word choice|lexical", "用词不准确"),
        (r"重复|repetition", "表达重复"),
        (r"指代|reference|referencing", "指代不清"),
        (r"衔接|cohesion|link", "衔接生硬"),
        (r"逻辑|展开|论证|develop|support", "论证展开"),
        (r"时态|tense", "时态控制"),
        (r"冠词|article", "冠词使用"),
    )
    folded = problem.casefold()
    for pattern, label in rules:
        if re.search(pattern, folded):
            return label
    return {
        "TR": "任务回应",
        "CC": "段落推进",
        "LR": "词汇与搭配",
        "GRA": "语法与句型",
    }[correction_criterion(correction)]


def learning_replacements(correction: Mapping[str, object]) -> list[dict[str, object]]:
    """Return explicit learning items, or one conservative legacy LR fallback."""
    if "learning_replacements" in correction:
        explicit = correction.get("learning_replacements")
        # An explicit empty list is meaningful in the new schema: the coach has
        # decided that this node must not be turned into a dictionary item.
        return (
            [dict(item) for item in explicit if isinstance(item, dict)]
            if isinstance(explicit, list) else []
        )
    if correction_criterion(correction) != "LR":
        return []
    pair = _legacy_lexical_pair(correction)
    if pair is None:
        return []
    source, target = pair
    return [{
        "source": source,
        "target": target,
        "headword": target,
        "part_of_speech": "",
        "meaning_zh": "",
        "simple_definition": "",
        "pattern": "",
        "collocations": [],
        "usage_note_zh": str(correction.get("problem") or ""),
        "legacy": True,
    }]


def grouped_corrections(
    corrections: list[dict[str, object]],
) -> list[tuple[str, list[tuple[int, dict[str, object]]]]]:
    """Group corrections by criterion while preserving their original map numbers."""
    grouped: dict[str, list[tuple[int, dict[str, object]]]] = {
        criterion: [] for criterion in CRITERION_ORDER
    }
    for index, correction in enumerate(corrections, start=1):
        grouped[correction_criterion(correction)].append((index, correction))
    return [(criterion, grouped[criterion]) for criterion in CRITERION_ORDER if grouped[criterion]]


def build_issue_map_html(corrections: list[dict[str, object]]) -> str:
    """Build an escaped criterion → issue → replacement hierarchy."""
    branches: list[str] = []
    for criterion, items in grouped_corrections(corrections):
        nodes: list[str] = []
        for index, correction in items:
            replacements = learning_replacements(correction)
            replacement_html = ""
            if replacements:
                paths = []
                for item in replacements:
                    source = str(item.get("source") or "原表达")
                    target = str(item.get("target") or "目标表达")
                    paths.append(
                        '<div class="issue-map-replacement">'
                        f'<span>{html.escape(source)}</span><b>→</b>'
                        f'<strong>{html.escape(target)}</strong></div>'
                    )
                replacement_html = "".join(paths)
            problem = " ".join(str(correction.get("problem") or "").split())
            title_id = f"issue-map-node-{index}"
            nodes.append(
                '<article class="issue-map-node" role="listitem" '
                f'aria-labelledby="{title_id}">'
                f'<h5 id="{title_id}" class="issue-map-node__title"><span>#{index}</span>'
                f'{html.escape(correction_issue_type(correction))}</h5>'
                '<blockquote class="issue-map-node__evidence" aria-label="原文证据">'
                f'{highlight_problem_text(dict(correction))}</blockquote>'
                f'<p class="issue-map-node__problem">{html.escape(problem)}</p>'
                f'{replacement_html}</article>'
            )
        criterion_id = f"issue-map-criterion-{criterion}"
        branches.append(
            '<section class="issue-map-branch" '
            f'aria-labelledby="{criterion_id}">'
            '<div class="issue-map-criterion">'
            f'<h4 id="{criterion_id}"><strong>{criterion}</strong>'
            f'<span>{html.escape(CRITERION_LABELS[criterion])}</span></h4>'
            f'<em>{len(items)}</em></div>'
            f'<div class="issue-map-nodes">{"".join(nodes)}</div></section>'
        )
        branches[-1] = branches[-1].replace(
            '<div class="issue-map-nodes">',
            '<div class="issue-map-nodes" role="list">',
        )
    return (
        '<div class="issue-map-tree">'
        '<div class="issue-map-tree__header" aria-label="问题地图阅读顺序">'
        '<span>原文证据</span><b aria-hidden="true">→</b>'
        '<strong>评分维度</strong><b aria-hidden="true">→</b><span>问题节点</span>'
        '<b aria-hidden="true">→</b><span>目标表达学习</span></div>'
        f'{"".join(branches)}</div>'
    )


def _flexible_quote_pattern(value: str) -> re.Pattern[str] | None:
    """Match an exact quote while tolerating line-wrap and whitespace differences."""
    pieces = value.strip().split()
    if not pieces:
        return None
    return re.compile(r"\s+".join(re.escape(piece) for piece in pieces), re.IGNORECASE)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def map_essay_issues(
    essay: str, corrections: list[dict[str, object]],
) -> tuple[str, list[int]]:
    """Return escaped essay HTML with stable issue numbers and unmatched node IDs."""
    located: list[tuple[int, int, int, dict[str, object]]] = []
    unmatched: list[int] = []
    occurrence_cursors: dict[str, int] = {}
    for index, correction in enumerate(corrections, start=1):
        original = str(correction.get("original") or "").strip()
        pattern = _flexible_quote_pattern(original)
        matches = list(pattern.finditer(essay)) if pattern is not None else []
        if not matches:
            unmatched.append(index)
            continue
        quote_key = " ".join(original.casefold().split())
        cursor = occurrence_cursors.get(quote_key, 0)
        match = matches[cursor % len(matches)]
        occurrence_cursors[quote_key] = cursor + 1
        located.append((match.start(), match.end(), index, correction))

    groups: list[dict[str, object]] = []
    for start, end, index, correction in sorted(located, key=lambda item: (item[0], item[1])):
        if groups and start < int(groups[-1]["end"]):
            group = groups[-1]
            group["end"] = max(int(group["end"]), end)
            group_items = group["items"]
            if isinstance(group_items, list):
                group_items.append((start, end, index, correction))
        else:
            groups.append({"start": start, "end": end, "items": [(start, end, index, correction)]})

    parts: list[str] = []
    essay_cursor = 0
    for group in groups:
        group_start = int(group["start"])
        group_end = int(group["end"])
        group_items = group["items"] if isinstance(group["items"], list) else []
        problem_ranges: list[tuple[int, int]] = []
        labels: list[int] = []
        for match_start, match_end, index, correction in group_items:
            labels.append(index)
            original = str(correction.get("original") or "")
            actual_segment = essay[match_start:match_end]
            for local_start, local_end in correction_problem_ranges(dict(correction)):
                problem_text = original[local_start:local_end]
                problem_pattern = _flexible_quote_pattern(problem_text)
                match = problem_pattern.search(actual_segment) if problem_pattern is not None else None
                if match is not None:
                    problem_ranges.append(
                        (match_start + match.start(), match_start + match.end())
                    )
        parts.append(html.escape(essay[essay_cursor:group_start]))
        marked_parts: list[str] = []
        marked_cursor = group_start
        for start, end in _merge_ranges(problem_ranges):
            start = max(group_start, start)
            end = min(group_end, end)
            if start < marked_cursor or end <= start:
                continue
            marked_parts.append(html.escape(essay[marked_cursor:start]))
            marked_parts.append(
                f'<u class="problem-span">{html.escape(essay[start:end])}</u>'
            )
            marked_cursor = end
        marked_parts.append(html.escape(essay[marked_cursor:group_end]))
        label_text = " · ".join(str(value) for value in sorted(set(labels)))
        parts.append(
            '<mark class="issue-mark">'
            + "".join(marked_parts)
            + f'<sup>{label_text}</sup></mark>'
        )
        essay_cursor = group_end
    parts.append(html.escape(essay[essay_cursor:]))
    return "".join(parts).replace("\n", "<br>"), unmatched


def report_essay_from_state(state: Mapping[str, object]) -> str:
    """Resolve report text from durable snapshots before the transient editor widget."""
    snapshot = state.get("draft_1_snapshot")
    pending = state.get("pending_guest_claim")
    structured = state.get("latest_structured")
    latest_ids = state.get("latest_cloud_ids")
    active_run_id = str(state.get("active_run_id") or "")
    current_run_id = active_run_id or (
        str(latest_ids.get("grading_run_id") or "")
        if isinstance(latest_ids, Mapping) else ""
    )

    def matches_report(value: object) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if not isinstance(structured, Mapping):
            return True
        corrections = structured.get("sentence_corrections")
        if not isinstance(corrections, list):
            return True
        originals = [
            " ".join(str(item.get("original") or "").casefold().split())
            for item in corrections if isinstance(item, Mapping) and item.get("original")
        ]
        normalized_text = " ".join(text.casefold().split())
        return not originals or all(original in normalized_text for original in originals)

    candidates: list[object] = []
    if isinstance(snapshot, Mapping):
        snapshot_run_id = str(snapshot.get("grading_run_id") or "")
        snapshot_structured = snapshot.get("structured")
        run_matches = not current_run_id or not snapshot_run_id or current_run_id == snapshot_run_id
        report_matches = (
            not isinstance(structured, Mapping)
            or not isinstance(snapshot_structured, Mapping)
            or snapshot_structured == structured
        )
        if run_matches and report_matches:
            candidates.append(snapshot.get("text"))
    if isinstance(pending, Mapping):
        package = pending.get("package")
        pending_structured = package.get("structured") if isinstance(package, Mapping) else None
        if (
            not isinstance(structured, Mapping)
            or not isinstance(pending_structured, Mapping)
            or pending_structured == structured
        ):
            candidates.append(pending.get("essay"))
    candidates.append(state.get("essay_input"))
    return next((str(value) for value in candidates if matches_report(value)), "")

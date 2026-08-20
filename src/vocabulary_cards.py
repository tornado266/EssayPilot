"""Vocabulary recommendations linked to exact essay evidence."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping

from src.issue_map import learning_replacements


KIND_LABELS = {
    "recommended": "原文好词 · 推荐保留",
    "upgrade": "可优化词 / 短语",
}


def _normalise(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _flexible_pattern(value: str) -> re.Pattern[str] | None:
    pieces = value.strip().split()
    if not pieces:
        return None
    return re.compile(r"\s+".join(re.escape(piece) for piece in pieces), re.IGNORECASE)


def _sentence_containing(essay: str, expression: str) -> str:
    pattern = _flexible_pattern(expression)
    match = pattern.search(essay) if pattern is not None else None
    if match is None:
        return ""
    start = max(essay.rfind(".", 0, match.start()), essay.rfind("!", 0, match.start()))
    start = max(start, essay.rfind("?", 0, match.start())) + 1
    endings = [position for marker in ".!?" if (position := essay.find(marker, match.end())) >= 0]
    end = min(endings) + 1 if endings else len(essay)
    return essay[start:end].strip()


def report_vocabulary_items(
    report: Mapping[str, object], essay: str = "",
) -> list[dict[str, object]]:
    """Return structured recommendations, with conservative legacy fallbacks."""
    if "vocabulary_recommendations" in report:
        explicit = report.get("vocabulary_recommendations")
        return (
            [dict(item) for item in explicit if isinstance(item, Mapping)]
            if isinstance(explicit, list) else []
        )

    items: list[dict[str, object]] = []
    seen: set[str] = set()
    raw_corrections = report.get("sentence_corrections")
    corrections = raw_corrections if isinstance(raw_corrections, list) else []
    for correction in corrections:
        if not isinstance(correction, Mapping):
            continue
        improved = str(correction.get("improved") or "").strip()
        for replacement in learning_replacements(correction):
            target = str(replacement.get("target") or "").strip()
            source = str(replacement.get("source") or "").strip()
            source_sentence = _sentence_containing(essay, source)
            key = _normalise(target)
            if not source or not target or not source_sentence or not key or key in seen:
                continue
            seen.add(key)
            raw_collocations = replacement.get("collocations")
            collocations = (
                [str(value).strip() for value in raw_collocations if str(value).strip()]
                if isinstance(raw_collocations, list) else []
            )
            items.append({
                "kind": "upgrade",
                "source": source,
                "target": target,
                "headword": str(replacement.get("headword") or target),
                "part_of_speech": str(replacement.get("part_of_speech") or "phrase"),
                "register": "neutral",
                "meaning_zh": str(replacement.get("meaning_zh") or ""),
                "simple_definition": str(replacement.get("simple_definition") or ""),
                "pattern": str(replacement.get("pattern") or ""),
                "collocations": collocations,
                "source_sentence": source_sentence,
                "reason_zh": str(
                    replacement.get("usage_note_zh") or correction.get("problem") or ""
                ),
                "example_en": improved,
                "example_zh": "",
                "legacy": True,
            })
            if len(items) >= 6:
                return items

    raw_expressions = report.get("useful_expressions")
    expressions = raw_expressions if isinstance(raw_expressions, list) else []
    for expression in expressions:
        if not isinstance(expression, Mapping):
            continue
        source = str(expression.get("expression") or "").strip()
        sentence = _sentence_containing(essay, source)
        key = _normalise(source)
        if not source or not sentence or key in seen:
            continue
        seen.add(key)
        items.append({
            "kind": "recommended",
            "source": source,
            "target": source,
            "headword": source,
            "part_of_speech": "phrase",
            "register": "neutral",
            "meaning_zh": str(expression.get("meaning") or ""),
            "simple_definition": "",
            "pattern": str(expression.get("usage_note") or ""),
            "collocations": [],
            "source_sentence": sentence,
            "reason_zh": "这条表达已经在原文中使用得较自然，值得保留并迁移到同类题目。",
            "example_en": str(expression.get("example") or ""),
            "example_zh": "",
            "legacy": True,
        })
        if len(items) >= 6:
            break
    return items


def _highlight_source(sentence: str, source: str) -> str:
    pattern = _flexible_pattern(source)
    match = pattern.search(sentence) if pattern is not None else None
    if match is None:
        return html.escape(sentence)
    return (
        html.escape(sentence[:match.start()])
        + f'<mark class="vocab-source-mark">{html.escape(sentence[match.start():match.end()])}</mark>'
        + html.escape(sentence[match.end():])
    )


def build_vocabulary_cards_html(items: list[dict[str, object]]) -> str:
    """Render escaped learner-dictionary cards similar to a classroom handout."""
    cards: list[str] = []
    for index, item in enumerate(items, start=1):
        kind = str(item.get("kind") or "recommended")
        kind = kind if kind in KIND_LABELS else "recommended"
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or source).strip()
        headword = str(item.get("headword") or target).strip()
        part_of_speech = str(item.get("part_of_speech") or "").strip()
        register = str(item.get("register") or "").strip()
        meaning = str(item.get("meaning_zh") or "").strip()
        definition = str(item.get("simple_definition") or "").strip()
        pattern = str(item.get("pattern") or "").strip()
        sentence = str(item.get("source_sentence") or "").strip()
        reason = str(item.get("reason_zh") or "").strip()
        example_en = str(item.get("example_en") or "").strip()
        example_zh = str(item.get("example_zh") or "").strip()
        raw_collocations = item.get("collocations")
        collocations = (
            [str(value).strip() for value in raw_collocations if str(value).strip()]
            if isinstance(raw_collocations, list) else []
        )
        route = ""
        if kind == "upgrade":
            route = (
                '<div class="vocab-card__route">'
                f'<span>{html.escape(source)}</span><b>→</b><strong>{html.escape(target)}</strong>'
                '</div>'
            )
        elif _normalise(headword) != _normalise(target):
            route = f'<div class="vocab-card__target">原文词形：<strong>{html.escape(target)}</strong></div>'
        evidence = (
            '<blockquote class="vocab-card__evidence">'
            f'{_highlight_source(sentence, source)}</blockquote>'
            if sentence else ""
        )
        entry_meta = "".join(
            value for value in (
                f'<span>{html.escape(part_of_speech)}</span>' if part_of_speech else "",
                f'<em>{html.escape(register)}</em>' if register else "",
            )
        )
        sense = ""
        if definition or meaning:
            sense = (
                '<div class="vocab-card__sense"><b>1.</b>'
                f'<p>{html.escape(definition)}'
                + (f'<span>{html.escape(meaning)}</span>' if meaning else "")
                + '</p></div>'
            )
        collocation_html = ""
        if collocations:
            collocation_html = (
                '<div class="vocab-card__collocations"><strong>常用搭配</strong>'
                + "".join(f'<span>{html.escape(value)}</span>' for value in collocations)
                + '</div>'
            )
        example = ""
        if example_en:
            example = (
                '<div class="vocab-card__example"><b>•</b><p>'
                f'<strong>{html.escape(example_en)}</strong>'
                + (f'<span>{html.escape(example_zh)}</span>' if example_zh else "")
                + '</p></div>'
            )
        cards.append(
            f'<article class="vocab-card vocab-card--{kind}">'
            '<header>'
            f'<span class="vocab-card__number">V{index}</span>'
            f'<span class="vocab-card__kind">{html.escape(KIND_LABELS[kind])}</span>'
            '</header>'
            f'{evidence}{route}'
            f'<h4>{html.escape(headword)} {entry_meta}</h4>'
            f'{sense}'
            + (f'<div class="vocab-card__pattern"><code>{html.escape(pattern)}</code></div>' if pattern else "")
            + collocation_html
            + example
            + (f'<footer>{html.escape(reason)}</footer>' if reason else "")
            + '</article>'
        )
    return (
        '<section class="vocab-learning-panel" aria-label="原文词汇推荐与可优化词">'
        '<div class="vocab-learning-panel__legend">'
        '<span class="recommended">原文好词</span><span class="upgrade">可优化词 / 短语</span>'
        '<small>词条按本文语境整理，不复制品牌词典原文</small></div>'
        f'<div class="vocab-card-grid">{"".join(cards)}</div></section>'
    )

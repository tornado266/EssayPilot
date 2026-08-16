"""Safe, deterministic issue-span and lexical-replacement helpers."""

from __future__ import annotations

import difflib
import html
import re
from typing import Any


_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")


def verified_problem_ranges(original: str, spans: object) -> list[tuple[int, int]]:
    """Return non-overlapping exact-substring ranges supplied by a new report."""
    if not isinstance(spans, list):
        return []
    ranges: list[tuple[int, int]] = []
    for value in spans:
        span = str(value) if isinstance(value, str) else ""
        if not span or span not in original:
            continue
        start = original.find(span)
        end = start + len(span)
        if not any(start < old_end and end > old_start for old_start, old_end in ranges):
            ranges.append((start, end))
    return sorted(ranges)


def fallback_problem_ranges(original: str, improved: str) -> list[tuple[int, int]]:
    """Locate changed source words for old reports; reject broad/uncertain rewrites."""
    before = [(match.group(0), match.start(), match.end()) for match in _WORD.finditer(original)]
    after = [match.group(0) for match in _WORD.finditer(improved)]
    if not before or not after:
        return []
    matcher = difflib.SequenceMatcher(
        a=[item[0].casefold() for item in before],
        b=[item.casefold() for item in after],
        autojunk=False,
    )
    changed: list[tuple[int, int]] = []
    changed_words = 0
    for opcode, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if opcode in {"delete", "replace"}:
            changed_words += i2 - i1
            if i2 > i1:
                changed.append((before[i1][1], before[i2 - 1][2]))
    if not changed or changed_words / len(before) > 0.6:
        return []
    return changed


def correction_problem_ranges(correction: dict[str, Any]) -> list[tuple[int, int]]:
    original = str(correction.get("original") or "")
    explicit = verified_problem_ranges(original, correction.get("problem_spans"))
    if explicit:
        return explicit
    return fallback_problem_ranges(original, str(correction.get("improved") or ""))


def highlight_problem_text(correction: dict[str, Any]) -> str:
    """Escape a red source sentence and underline only verified problem ranges."""
    original = str(correction.get("original") or "")
    ranges = correction_problem_ranges(correction)
    parts: list[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(html.escape(original[cursor:start]))
        parts.append(f'<u class="problem-span">{html.escape(original[start:end])}</u>')
        cursor = end
    parts.append(html.escape(original[cursor:]))
    return "".join(parts)


def lexical_replacement(correction: dict[str, Any]) -> str:
    """Extract a concise added word/phrase without any model call."""
    original_words = _WORD.findall(str(correction.get("original") or ""))
    improved_words = _WORD.findall(str(correction.get("improved") or ""))
    matcher = difflib.SequenceMatcher(
        a=[word.casefold() for word in original_words],
        b=[word.casefold() for word in improved_words],
        autojunk=False,
    )
    candidates: list[list[str]] = []
    for opcode, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if opcode in {"insert", "replace"} and j2 > j1:
            candidates.append(improved_words[j1:j2])
    if not candidates:
        return ""
    words = min(candidates, key=lambda item: (len(item), sum(map(len, item))))
    return " ".join(words[:4])


def contextual_collocation(sentence: str, replacement: str) -> str:
    """Take a small local collocation window directly from the improved sentence."""
    words = _WORD.findall(sentence)
    target = _WORD.findall(replacement)
    if not words or not target:
        return ""
    folded = [word.casefold() for word in words]
    target_folded = [word.casefold() for word in target]
    for index in range(len(words) - len(target) + 1):
        if folded[index:index + len(target)] == target_folded:
            start = max(0, index - 2)
            end = min(len(words), index + len(target) + 2)
            return " ".join(words[start:end])
    return ""

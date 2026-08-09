import re


WORD_PATTERN = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)?|\d+(?:\.\d+)?")


def count_words(text: str) -> int:
    """Count English words and numbers in an IELTS essay."""
    return len(WORD_PATTERN.findall(text))


def word_count_warning(task_type: str, word_count: int) -> str:
    """Return a word-count warning when the essay is under IELTS limits."""
    minimum = 150 if task_type == "Task 1" else 250
    if word_count >= minimum:
        return ""

    missing = minimum - word_count
    return f"{task_type} 通常至少需要 {minimum} 词；当前为 {word_count} 词，还差约 {missing} 词。"

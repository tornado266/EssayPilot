"""Validated, side-effect-free data loader for the zero-token walkthrough."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.chinese_report import examiner_result_to_markdown
from src.report_schema import ExaminerResultError, validate_examiner_result
from src.text_utils import count_words


DEFAULT_DEMO_PACKAGE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "demo_package.json"
)


class DemoPackageError(RuntimeError):
    """Raised when the bundled walkthrough data cannot be loaded safely."""


@dataclass(frozen=True)
class DemoPackage:
    """One complete local walkthrough package with no runtime service dependencies."""

    question: str
    essay: str
    structured: dict[str, Any]
    report: str
    draft_2: str
    draft_changes: dict[str, str]
    word_count: int


def _required_text(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DemoPackageError(f"The demo package has no valid {field} field.")
    return value.strip()


def load_demo_package(path: Path | None = None) -> DemoPackage:
    """Load and validate a static demo without Streamlit, network, or model calls."""

    package_path = Path(path) if path is not None else DEFAULT_DEMO_PACKAGE_PATH
    try:
        raw = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DemoPackageError("The zero-token demo package could not be loaded.") from exc
    if not isinstance(raw, dict):
        raise DemoPackageError("The zero-token demo package must be a JSON object.")

    question = _required_text(raw, "question")
    essay = _required_text(raw, "essay")
    draft_2 = _required_text(raw, "draft_2")
    structured = raw.get("structured")
    if not isinstance(structured, dict):
        raise DemoPackageError("The demo package has no valid structured report.")

    raw_changes = raw.get("draft_changes")
    if not isinstance(raw_changes, dict) or not raw_changes:
        raise DemoPackageError("The demo package has no valid draft_changes field.")
    draft_changes: dict[str, str] = {}
    for key, value in raw_changes.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
        ):
            raise DemoPackageError(
                "Every demo draft change must have a non-empty label and description."
            )
        draft_changes[key.strip()] = value.strip()

    try:
        validated = validate_examiner_result(deepcopy(structured), essay)
        report = examiner_result_to_markdown(validated)
    except (ExaminerResultError, KeyError, TypeError, ValueError) as exc:
        raise DemoPackageError(
            "The zero-token demo package does not match the current report schema."
        ) from exc

    return DemoPackage(
        question=question,
        essay=essay,
        structured=validated,
        report=report,
        draft_2=draft_2,
        draft_changes=draft_changes,
        word_count=count_words(essay),
    )

"""Import the private official calibration transcript without third-party packages.

The generated JSON deliberately separates the only fields allowed to reach the
grader (task prompt and candidate response) from labels and examiner comments.
Both the source DOCX and generated dataset are private evaluation material.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _style_names(archive: zipfile.ZipFile) -> dict[str, str]:
    root = ET.fromstring(archive.read("word/styles.xml"))
    names: dict[str, str] = {}
    for style in root.findall(f"{W}style"):
        style_id = style.get(f"{W}styleId", "")
        name = style.find(f"{W}name")
        names[style_id] = name.get(f"{W}val", style_id) if name is not None else style_id
    return names


def read_paragraphs(path: Path) -> list[tuple[str, str]]:
    """Return non-empty ``(style name, text)`` paragraphs in document order."""
    with zipfile.ZipFile(path) as archive:
        styles = _style_names(archive)
        root = ET.fromstring(archive.read("word/document.xml"))

    paragraphs: list[tuple[str, str]] = []
    for paragraph in root.iter(f"{W}p"):
        style_node = paragraph.find(f"{W}pPr/{W}pStyle")
        style_id = style_node.get(f"{W}val", "") if style_node is not None else ""
        style = styles.get(style_id, style_id or "Normal")
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{W}t":
                pieces.append(node.text or "")
            elif node.tag == f"{W}tab":
                pieces.append("\t")
            elif node.tag in {f"{W}br", f"{W}cr"}:
                pieces.append("\n")
        text = "".join(pieces).strip()
        if text:
            paragraphs.append((style, text))
    return paragraphs


def _heading_level(style: str) -> int:
    normalized = re.sub(r"\s+", "", style).casefold()
    if normalized in {"heading1", "1"}:
        return 1
    if normalized in {"heading2", "2"}:
        return 2
    return 0


def parse_transcript(path: Path) -> dict[str, Any]:
    """Parse the known heading contract while preserving essay paragraphs."""
    cases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section = ""

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        missing = [
            name
            for name, value in (
                ("task prompt", current["prompt"]),
                ("candidate response", current["essay"]),
                ("official band", current["band"]),
                ("examiner comment", current["comment"]),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"{current['heading']}: missing {', '.join(missing)}")
        case_id = f"official-{len(cases) + 1:02d}"
        cases.append(
            {
                "model_input": {
                    "task_prompt": "\n\n".join(current["prompt"]),
                    "candidate_response": "\n\n".join(current["essay"]),
                },
                "evaluation": {
                    "case_id": case_id,
                    "expected_overall": current["band"],
                    "examiner_comment": "\n\n".join(current["comment"]),
                    "source_reference": current["source"],
                    "source_heading": current["heading"],
                },
            }
        )
        current = None

    for style, text in read_paragraphs(path):
        level = _heading_level(style)
        if level == 1:
            finish()
            current = {
                "heading": text,
                "source": "",
                "band": None,
                "prompt": [],
                "essay": [],
                "comment": [],
            }
            section = ""
            continue
        if current is None:
            continue
        if level == 2:
            lowered = text.casefold()
            if lowered.startswith("task prompt"):
                section = "prompt"
            elif lowered.startswith("candidate response"):
                section = "essay"
            elif lowered.startswith("examiner comment"):
                section = "comment"
            else:
                section = ""
            continue

        band_match = re.fullmatch(r"Official band\s+([0-9](?:\.[05])?)", text, re.IGNORECASE)
        if band_match:
            current["band"] = float(band_match.group(1))
        elif text.casefold().startswith("source ") and not section:
            current["source"] = text[7:].strip()
        elif section == "essay" and text.casefold().startswith("transcription note"):
            continue
        elif section in {"prompt", "essay", "comment"}:
            current[section].append(text)

    finish()
    if len(cases) != 7:
        raise ValueError(f"Expected 7 official scripts, found {len(cases)}")
    return {
        "dataset_version": "1.0",
        "source_type": "official_internal",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_document_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    dataset = parse_transcript(args.docx)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Imported {len(dataset['cases'])} private calibration cases to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

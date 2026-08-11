#!/usr/bin/env python3
"""Refresh weak feedback labels without changing the frozen split membership."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.kaggle_skill_dataset import (  # noqa: E402
    scoring_skill_eligibility,
    structure_examiner_feedback,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--processed-dir", type=Path,
        default=ROOT_DIR / "data" / "processed" / "kaggle_ielts",
    )
    parser.add_argument(
        "--private-dir", type=Path,
        default=ROOT_DIR / ".private" / "kaggle_ielts",
    )
    parser.add_argument(
        "--manifest", type=Path,
        default=ROOT_DIR / "data" / "kaggle_skill_split_manifest.json",
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--unlock-holdout", action="store_true")
    return parser.parse_args()


def _load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _expected_ids(manifest: dict, split: str) -> list[str]:
    return sorted(
        str(record["case_id"])
        for record in manifest["splits"][split]["records"]
    )


def _refresh(path: Path, *, split: str, expected_ids: list[str], write: bool) -> dict:
    records = _load_jsonl(path)
    actual_ids = sorted(str(record["case_id"]) for record in records)
    if actual_ids != expected_ids:
        raise RuntimeError(f"{split} membership differs from the frozen public manifest.")
    refreshed: list[dict] = []
    weakness_cases = 0
    for record in records:
        output = dict(record)
        structured = structure_examiner_feedback(record)
        output["structured_examiner_feedback"] = structured
        weakness_cases += int(bool(structured["weakness_tags"]))
        if split != "holdout":
            eligible, reasons = scoring_skill_eligibility(record, structured)
            output["score_skill_eligible"] = eligible
            output["score_skill_exclusion_reasons"] = reasons
        refreshed.append(output)
    if write:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in refreshed:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "split": split,
        "records": len(records),
        "cases_with_weakness_labels": weakness_cases,
        "membership_unchanged": True,
        "written": write,
    }


def main() -> int:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    paths = {
        "development": args.processed_dir / "examiner_claimed_development.jsonl",
        "validation": args.processed_dir / "examiner_claimed_validation.jsonl",
    }
    if args.unlock_holdout:
        paths["holdout"] = args.private_dir / "examiner_claimed_holdout.jsonl"
    results = [
        _refresh(
            path,
            split=split,
            expected_ids=_expected_ids(manifest, split),
            write=args.write,
        )
        for split, path in paths.items()
    ]
    print(json.dumps({
        "label_method": "deterministic-comment-structure-v2",
        "holdout_content_printed": False,
        "splits": results,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

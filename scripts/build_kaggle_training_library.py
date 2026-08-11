#!/usr/bin/env python3
"""Build the private Kaggle Task 2 learner corpus and audit artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.kaggle_training_data import (  # noqa: E402
    clean_dataset,
    load_source_rows,
    source_manifest,
    write_outputs,
)
from src.kaggle_skill_dataset import (  # noqa: E402
    frozen_split_from_manifest,
    split_examiner_claimed,
    write_skill_splits,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=ROOT_DIR / "data" / "raw" / "kaggle_ielts",
        help="CSV, JSON, JSONL, ZIP, or directory containing the immutable source dataset.",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT_DIR / "data" / "processed" / "kaggle_ielts",
    )
    parser.add_argument("--source-url", default="")
    parser.add_argument("--candidate-limit", type=int, default=60, choices=range(30, 61))
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N source rows for a pilot.")
    parser.add_argument("--approved-case-ids", type=Path, default=None)
    parser.add_argument(
        "--private-dir", type=Path,
        default=ROOT_DIR / ".private" / "kaggle_ielts",
    )
    parser.add_argument(
        "--public-split-manifest", type=Path,
        default=ROOT_DIR / "data" / "kaggle_skill_split_manifest.json",
    )
    parser.add_argument("--dry-run", action="store_true", help="Profile and clean in memory without writing outputs.")
    return parser.parse_args()


def _approved_ids(path: Path | None) -> set[str]:
    if path is None:
        return set()
    return {
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def main() -> int:
    args = parse_args()
    rows, profile, files = load_source_rows(args.input)
    before = source_manifest(files, args.source_url)
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("--limit must be positive.")
        rows = rows[:args.limit]
        profile = {**profile, "pilot_limit": args.limit, "processed_rows": len(rows)}
    result = clean_dataset(rows, candidate_limit=args.candidate_limit)
    if args.public_split_manifest.exists():
        frozen_manifest = json.loads(args.public_split_manifest.read_text(encoding="utf-8"))
        splits = frozen_split_from_manifest(result["all_records"], frozen_manifest)
    else:
        splits = split_examiner_claimed(result["all_records"])
    after = source_manifest(files, args.source_url)
    if [(item["path"], item["sha256"]) for item in before["files"]] != [
        (item["path"], item["sha256"]) for item in after["files"]
    ]:
        raise RuntimeError("A raw dataset file changed while the cleaning pipeline was running.")
    if args.dry_run:
        summary = {
            "input_rows": len(rows),
            "clean_task2": len(result["clean_task2"]),
            "quarantine": len(result["quarantine"]),
            "core_training_case_candidates": len(result["core_training_case_candidates"]),
            "examiner_claimed_splits": {name: len(records) for name, records in splits.items()},
            "dry_run": True,
        }
    else:
        summary = write_outputs(
            args.output,
            result,
            profile,
            before,
            approved_case_ids=_approved_ids(args.approved_case_ids),
        )
        write_skill_splits(
            splits,
            processed_dir=args.output,
            private_dir=args.private_dir,
            public_manifest_path=args.public_split_manifest,
            learner_unlabelled=result["learner_corpus"],
        )
        summary["examiner_claimed_splits"] = {
            name: len(records) for name, records in splits.items()
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

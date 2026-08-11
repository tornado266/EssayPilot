#!/usr/bin/env python3
"""Build aggregate Skill evidence from official and development-only feedback."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.kaggle_skill_dataset import (  # noqa: E402
    build_skill_rule_audit,
    load_official_verified,
    load_skill_split,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--official", type=Path,
        default=ROOT_DIR / ".private" / "calibration" / "official_task2-expanded.json",
    )
    parser.add_argument(
        "--development", type=Path,
        default=ROOT_DIR / "data" / "processed" / "kaggle_ielts" / "examiner_claimed_development.jsonl",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT_DIR / ".private" / "kaggle_ielts" / "skill_rule_audit.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    official = load_official_verified(args.official)
    development = load_skill_split(args.development, split="development")
    report = build_skill_rule_audit(official, development)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "official_verified_cases": report["official_verified_cases"],
        "examiner_claimed_development_cases": report["examiner_claimed_development_cases"],
        "scoring_skill_eligible_claimed_cases": report["scoring_skill_eligible_claimed_cases"],
        "accepted_abstract_rules": len(report["accepted_abstract_rules"]),
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

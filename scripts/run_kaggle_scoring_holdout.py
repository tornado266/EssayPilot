#!/usr/bin/env python3
"""One-time score-only audit on the low-confidence Kaggle holdout; dry-run by default."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.ai_grader import PRODUCTION_SCORING_MODEL, grade_scoring_decision  # noqa: E402
from src.kaggle_skill_dataset import (  # noqa: E402
    blind_scoring_input,
    load_skill_split,
    weak_scoring_metrics,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--unlock-holdout", action="store_true")
    parser.add_argument("--model", default=PRODUCTION_SCORING_MODEL)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT_DIR / ".private" / "kaggle_ielts" / "scoring_eval",
    )
    return parser.parse_args()


def _existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {record["case_id"]: record for record in records if record.get("status") == "complete"}


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    if not args.unlock_holdout:
        raise PermissionError("The scoring holdout requires explicit --unlock-holdout.")
    holdout_path = ROOT_DIR / ".private" / "kaggle_ielts" / "examiner_claimed_holdout.jsonl"
    cases = load_skill_split(holdout_path, split="holdout", unlock_holdout=True)
    output = args.output_dir / "predictions.jsonl"
    consumed = args.output_dir / "holdout-consumed.json"
    if consumed.exists():
        raise RuntimeError("The low-confidence scoring holdout has already been consumed.")
    previous = _existing(output) if args.resume else {}
    pending = [case for case in cases if case["case_id"] not in previous]
    if not args.execute:
        print(json.dumps({
            "dry_run": True,
            "selected": len(cases),
            "would_call": len(pending),
            "model": args.model,
            "secondary_low_confidence_only": True,
        }, ensure_ascii=False, indent=2))
        return 0
    predictions = dict(previous)
    total_tokens = 0
    for case in pending:
        model_input = blind_scoring_input(case)
        try:
            package = grade_scoring_decision(model=args.model, **model_input)
            record = {
                "case_id": case["case_id"],
                "status": "complete",
                "predicted_overall": package["structured"]["overall_band"],
                "prompt_version": package["prompt_version"],
                "usage": package.get("usage") or {},
            }
            total_tokens += int((package.get("usage") or {}).get("total_tokens") or 0)
        except Exception as exc:
            record = {"case_id": case["case_id"], "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        _append(output, record)
        if record["status"] == "complete":
            predictions[case["case_id"]] = record
    metrics = weak_scoring_metrics(cases, list(predictions.values()))
    metrics["total_tokens"] = total_tokens
    metrics_path = args.output_dir / "metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    if metrics["valid_predictions"] == 12:
        consumed.write_text(json.dumps({"completed": True, "metrics_file": str(metrics_path)}, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if metrics["valid_predictions"] == 12 else 1


if __name__ == "__main__":
    raise SystemExit(main())

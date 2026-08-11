#!/usr/bin/env python3
"""Score a fixed development-only Kaggle sanity sample; dry-run by default."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ai_grader import grade_scoring_decision  # noqa: E402
from src.kaggle_skill_dataset import (  # noqa: E402
    blind_scoring_input,
    load_skill_split,
    weak_scoring_metrics,
)


STRICT_SAMPLE_IDS = (
    "kaggle_08c349b3f2632515",  # Band 5
    "kaggle_f63117a81f2bb41b", "kaggle_0eb6b3d3a52ce2b6",
    "kaggle_894b4ffc6282f0a0",  # Band 6
    "kaggle_58eaa03c6f3ee660", "kaggle_e16f30179c1383b2",
    "kaggle_8bd3527644fbc50b", "kaggle_911ffb02aafb4c1f",  # Band 8
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model", default="gpt-5.4-mini-2026-03-17")
    parser.add_argument(
        "--input", type=Path,
        default=ROOT / "data" / "processed" / "kaggle_ielts" / "examiner_claimed_development.jsonl",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / ".private" / "kaggle_ielts" / "scoring_sanity_strict8",
    )
    return parser.parse_args()


def _latest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[str(record["case_id"])] = record
    return {key: value for key, value in records.items() if value.get("status") == "complete"}


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _counterfactual(cases: list[dict], predictions: list[dict], mode: str) -> dict:
    by_id = {str(item["case_id"]): item for item in predictions if item.get("status") == "complete"}
    differences = []
    for case in cases:
        prediction = by_id.get(str(case["case_id"]))
        if not prediction:
            continue
        predicted = float(prediction["predicted_overall"])
        if mode == "all_plus_half":
            predicted = min(9.0, predicted + 0.5)
        elif mode == "high_plus_half" and predicted >= 6.5:
            predicted = min(9.0, predicted + 0.5)
        differences.append(predicted - float(case["original_overall_score"]))
    return {
        "mae": sum(abs(value) for value in differences) / len(differences),
        "mean_signed_bias": sum(differences) / len(differences),
        "within_0_5": sum(abs(value) <= 0.5 for value in differences),
        "max_absolute_error": max(abs(value) for value in differences),
    } if differences else {}


def main() -> int:
    args = parse_args()
    all_cases = load_skill_split(args.input, split="development")
    by_id = {str(case["case_id"]): case for case in all_cases}
    cases = [by_id[case_id] for case_id in STRICT_SAMPLE_IDS]
    if any(not case.get("score_skill_eligible") for case in cases):
        raise ValueError("Every sanity case must pass strict scoring-skill eligibility.")
    output = args.output_dir / "predictions.jsonl"
    existing = _latest(output) if args.resume else {}
    pending = [case for case in cases if str(case["case_id"]) not in existing]
    if not args.execute:
        print(json.dumps({
            "dry_run": True, "selected": len(cases), "would_call": len(pending),
            "bands": [case["original_overall_score"] for case in cases],
            "model": args.model, "thinking": "none", "holdout_used": False,
        }, ensure_ascii=False, indent=2))
        return 0
    predictions = dict(existing)
    for case in pending:
        events: list[dict] = []
        try:
            package = grade_scoring_decision(
                **blind_scoring_input(case), provider="OpenAI", model=args.model,
                reasoning_effort="none", audit_hook=events.append,
            )
            record = {
                "case_id": case["case_id"], "status": "complete",
                "predicted_overall": package["structured"]["overall_band"],
                "criteria": package["structured"]["criteria"],
                "provider": package["provider"], "model": package["model"],
                "prompt_version": package["prompt_version"],
                "usage": package.get("usage") or {},
                "attempts": len(events),
            }
            predictions[str(case["case_id"])] = record
        except Exception as exc:
            record = {
                "case_id": case["case_id"], "status": "error",
                "error": f"{type(exc).__name__}: {exc}", "attempts": len(events),
            }
        _append(output, record)
    selected_predictions = [predictions[key] for key in STRICT_SAMPLE_IDS if key in predictions]
    metrics = weak_scoring_metrics(cases, selected_predictions)
    metrics["raw"] = _counterfactual(cases, selected_predictions, "raw")
    metrics["all_plus_half"] = _counterfactual(cases, selected_predictions, "all_plus_half")
    metrics["predicted_at_least_6_5_plus_half"] = _counterfactual(
        cases, selected_predictions, "high_plus_half"
    )
    metrics["rows"] = [
        {
            "case_id": case["case_id"],
            "examiner_claimed": case["original_overall_score"],
            "predicted": predictions.get(str(case["case_id"]), {}).get("predicted_overall"),
        }
        for case in cases
    ]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if metrics["valid_predictions"] == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())

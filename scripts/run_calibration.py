"""Run paid repeatability or licensed gold-set evaluation for Task 2 scoring."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ai_grader import grade_essay_package
from src.report_schema import CRITERIA, score_snapshot


SNAPSHOT_KEY_BY_CRITERION = {
    "Task Response": "Task Response",
    "Coherence and Cohesion": "Coherence & Cohesion",
    "Lexical Resource": "Lexical Resource",
    "Grammatical Range and Accuracy": "Grammar Range & Accuracy",
}


def validate_dataset(cases: list[dict[str, Any]], mode: str) -> None:
    """Reject provenance or label claims that are insufficient for the selected mode."""
    if not cases:
        raise ValueError("The calibration dataset is empty.")
    for case in cases:
        missing = [key for key in ("id", "question", "essay", "source_type", "provenance") if not case.get(key)]
        if missing:
            raise ValueError(f"{case.get('id', '<unknown>')}: missing {', '.join(missing)}")
        if mode == "gold":
            if case["source_type"] not in {"official", "human_gold"}:
                raise ValueError(f"{case['id']}: synthetic cases cannot be used as gold labels")
            if not case.get("license_or_permission"):
                raise ValueError(f"{case['id']}: gold use requires licence or permission metadata")
            if "annotator_count" not in case or "adjudication_status" not in case:
                raise ValueError(f"{case['id']}: gold provenance must include annotator and adjudication metadata")
            gold = case.get("gold")
            if not isinstance(gold, dict) or "overall" not in gold or "criteria" not in gold:
                raise ValueError(f"{case['id']}: gold criteria and overall are required")
            if set(gold["criteria"]) != set(CRITERIA):
                raise ValueError(f"{case['id']}: gold labels must include all four official criteria")
            if case["source_type"] == "human_gold" and case.get("adjudication_status") != "adjudicated":
                raise ValueError(f"{case['id']}: human gold labels must be adjudicated")


def repeatability_metrics(snapshots: list[dict[str, float | None]]) -> dict[str, Any]:
    """Compute deterministic spread and signed-difference diagnostics."""
    keys = tuple(snapshots[0])
    values = {key: [float(item[key]) for item in snapshots if item[key] is not None] for key in keys}
    vector_counts: dict[str, int] = defaultdict(int)
    for item in snapshots:
        vector = tuple(item[key] for key in keys if key != "Overall Band")
        vector_counts[str(vector)] += 1
    return {
        "criterion_vector_exact_agreement": max(vector_counts.values()) / len(snapshots),
        "max_spread": {key: max(items) - min(items) for key, items in values.items()},
        "mean_signed_difference_from_first": {
            key: mean(value - items[0] for value in items) for key, items in values.items()
        },
    }


def gold_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate transparent accuracy metrics without inventing missing labels."""
    errors: dict[str, list[float]] = defaultdict(list)
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for record in records:
        predicted = record["predicted"]
        gold = record["gold"]
        pairs = {"Overall Band": (predicted["Overall Band"], gold["overall"])}
        pairs.update(
            {
                name: (predicted[SNAPSHOT_KEY_BY_CRITERION[name]], value)
                for name, value in gold["criteria"].items()
            }
        )
        for name, (prediction, target) in pairs.items():
            difference = float(prediction) - float(target)
            errors[name].append(difference)
            confusion[name][f"{target}->{prediction}"] += 1
    return {
        "mae": {name: mean(abs(value) for value in values) for name, values in errors.items()},
        "within_0_5_rate": {name: mean(abs(value) <= 0.5 for value in values) for name, values in errors.items()},
        "mean_bias": {name: mean(values) for name, values in errors.items()},
        "confusion_matrix": {name: dict(cells) for name, cells in confusion.items()},
        "weighted_kappa": None,
        "weighted_kappa_note": "Compute only after a sufficiently large, appropriately labelled gold set is available.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "data" / "calibration_cases.json")
    parser.add_argument("--mode", choices=("repeatability", "gold"), default="repeatability")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--case", default="")
    args = parser.parse_args()
    repeats = args.repeats if args.repeats is not None else (5 if args.mode == "repeatability" else 1)
    if repeats < (2 if args.mode == "repeatability" else 1):
        parser.error("repeatability needs at least 2 runs; gold mode needs at least 1")

    load_dotenv()
    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    if args.case:
        cases = [case for case in cases if case["id"] == args.case]
    validate_dataset(cases, args.mode)
    results: list[dict[str, Any]] = []
    failures: list[str] = []

    for case in cases:
        snapshots = [
            score_snapshot(grade_essay_package(task_type="Task 2", topic=case["question"], essay=case["essay"])["structured"])
            for _ in range(repeats)
        ]
        if args.mode == "repeatability":
            metrics = repeatability_metrics(snapshots)
            spreads = metrics["max_spread"]
            if spreads["Overall Band"] > 0.5:
                failures.append(f"{case['id']}: Overall spread exceeded 0.5")
            if any(value > 1.0 for key, value in spreads.items() if key != "Overall Band"):
                failures.append(f"{case['id']}: a criterion spread exceeded 1 band")
            results.append({"case": case["id"], "snapshots": snapshots, "metrics": metrics})
        else:
            for snapshot in snapshots:
                results.append({"case": case["id"], "predicted": snapshot, "gold": case["gold"]})

    payload: dict[str, Any] = {"mode": args.mode, "ok": not failures, "failures": failures}
    payload["results"] = results
    if args.mode == "gold":
        payload["metrics"] = gold_metrics(results)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

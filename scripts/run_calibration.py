"""Run repeatability checks against fixed Task 2 essays (uses paid API calls)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from src.ai_grader import grade_essay_package
from src.report_schema import score_snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--case", default="")
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("--repeats must be at least 2")

    load_dotenv()
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "data" / "calibration_cases.json").read_text(encoding="utf-8"))
    if args.case:
        cases = [case for case in cases if case["id"] == args.case]
    failures: list[str] = []
    summary: list[dict[str, object]] = []

    for case in cases:
        snapshots = []
        for _ in range(args.repeats):
            package = grade_essay_package(
                task_type="Task 2",
                topic=case["question"],
                essay=case["essay"],
            )
            snapshots.append(score_snapshot(package["structured"]))
        spreads = {
            key: max(float(item[key]) for item in snapshots) - min(float(item[key]) for item in snapshots)
            for key in snapshots[0]
            if all(item[key] is not None for item in snapshots)
        }
        overall_values = [float(item["Overall Band"]) for item in snapshots]
        if any(spread > 0.5 for spread in spreads.values()):
            failures.append(f"{case['id']}: score spread exceeded 0.5")
        if not all(case["expected_overall_min"] <= value <= case["expected_overall_max"] for value in overall_values):
            failures.append(f"{case['id']}: score outside expected calibration range")
        summary.append({"case": case["id"], "snapshots": snapshots, "spreads": spreads})

    print(json.dumps({"ok": not failures, "failures": failures, "results": summary}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

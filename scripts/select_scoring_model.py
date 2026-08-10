"""Select the cheapest scoring model that passes development and holdout gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.run_calibration import acceptance_status


def load_run(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), dict):
        raise ValueError(f"Invalid calibration run: {path}")
    return payload


def low_band_mae(run: dict[str, Any]) -> float | None:
    errors = [
        float(item["absolute_error"])
        for item in run["summary"].get("cases", [])
        if item.get("absolute_error") is not None
        and float(item.get("expected_overall", 10)) <= 5.5
    ]
    return sum(errors) / len(errors) if errors else None


def _latency_per_success(run: dict[str, Any]) -> float:
    runs = [item for case in run.get("results", []) for item in case.get("runs", [])]
    successful = [item for item in runs if item.get("status", "ok") == "ok"]
    return (
        sum(float(item.get("latency_seconds") or 0) for item in successful) / len(successful)
        if successful
        else float("inf")
    )


def _cost_per_success(*runs: dict[str, Any]) -> float:
    cost = sum(float(run.get("metadata", {}).get("usage", {}).get("estimated_usd") or 0) for run in runs)
    successes = sum(
        int(run.get("summary", {}).get("overall", {}).get("successful_runs") or 0)
        for run in runs
    )
    return cost / successes if successes else float("inf")


def select_cheapest_passing(
    baseline_development: dict[str, Any],
    candidates: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Apply quality gates first, then cost, max-error, and latency tie-breaks."""
    baseline_low = low_band_mae(baseline_development)
    evaluated = []
    for name, development, holdout in candidates:
        development_gate = acceptance_status(development["summary"], "development")
        holdout_gate = acceptance_status(holdout["summary"], "holdout")
        candidate_low = low_band_mae(development)
        low_band_not_worse = (
            baseline_low is None
            or candidate_low is None
            or candidate_low <= baseline_low
        )
        max_error = max(
            float(development["summary"]["overall"]["max_absolute_error"]),
            float(holdout["summary"]["overall"]["max_absolute_error"]),
        )
        latency = (
            _latency_per_success(development) + _latency_per_success(holdout)
        ) / 2
        item = {
            "name": name,
            "provider": development.get("metadata", {}).get("provider"),
            "model": development.get("metadata", {}).get("model"),
            "development_passed": development_gate["passed"],
            "holdout_passed": holdout_gate["passed"],
            "low_band_not_worse": low_band_not_worse,
            "passed": development_gate["passed"] and holdout_gate["passed"] and low_band_not_worse,
            "cost_per_successful_score_usd": _cost_per_success(development, holdout),
            "max_absolute_error": max_error,
            "mean_latency_seconds": latency,
        }
        evaluated.append(item)
    passing = [item for item in evaluated if item["passed"]]
    passing.sort(
        key=lambda item: (
            item["cost_per_successful_score_usd"],
            item["max_absolute_error"],
            item["mean_latency_seconds"],
        )
    )
    return {"selected": passing[0] if passing else None, "candidates": evaluated}


def _parse_candidate(value: str) -> tuple[str, Path, Path]:
    try:
        name, paths = value.split("=", 1)
        development, holdout = paths.split(",", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "candidate must be NAME=DEVELOPMENT_RUN,HOLDOUT_RUN"
        ) from exc
    return name, Path(development), Path(holdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-development", type=Path, required=True)
    parser.add_argument("--candidate", type=_parse_candidate, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidates = [
        (name, load_run(development), load_run(holdout))
        for name, development, holdout in args.candidate
    ]
    result = select_cheapest_passing(load_run(args.baseline_development), candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

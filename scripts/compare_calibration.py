"""Compare private calibration runs without embedding private dataset content."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_run(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
        raise ValueError(f"Invalid calibration run: {path}")
    return data


def case_map(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["case_id"]: item for item in run["summary"].get("cases", [])}


def acceptance(run: dict[str, Any]) -> dict[str, bool]:
    cases = list(case_map(run).values())
    overall = run["summary"].get("overall", {})
    valid_errors = [item.get("absolute_error") for item in cases]
    spreads = [item.get("max_spread", {}).get("Overall Band") for item in cases]
    return {
        "at_least_6_of_7_within_0_5": sum(error is not None and error <= 0.5 for error in valid_errors) >= 6,
        "no_case_error_over_1_0": len(cases) == 7 and all(error is not None and error <= 1.0 for error in valid_errors),
        "mae_at_most_0_5": overall.get("mae") is not None and overall["mae"] <= 0.5,
        "all_spreads_at_most_0_5": len(cases) == 7 and all(spread is not None and spread <= 0.5 for spread in spreads),
        "all_cases_have_a_score": len(cases) == 7 and all(error is not None for error in valid_errors),
    }


def low_band_mae(run: dict[str, Any]) -> float | None:
    values = [
        item["absolute_error"]
        for item in case_map(run).values()
        if item.get("expected_overall", 10) <= 5.5 and item.get("absolute_error") is not None
    ]
    return sum(values) / len(values) if values else None


def reasoning_gate(none_run: dict[str, Any], low_run: dict[str, Any]) -> dict[str, bool]:
    none_overall = none_run["summary"]["overall"]
    low_overall = low_run["summary"]["overall"]
    none_cases = case_map(none_run)
    low_cases = case_map(low_run)
    shared = sorted(set(none_cases) & set(low_cases))
    no_large_regression = all(
        none_cases[case_id].get("absolute_error") is not None
        and low_cases[case_id].get("absolute_error") is not None
        and low_cases[case_id]["absolute_error"] <= none_cases[case_id]["absolute_error"] + 0.5
        for case_id in shared
    ) and len(shared) == 7
    none_low = low_band_mae(none_run)
    low_low = low_band_mae(low_run)
    low_not_worse = none_low is not None and low_low is not None and low_low <= none_low
    low_spreads = [item.get("max_spread", {}).get("Overall Band") for item in low_cases.values()]
    return {
        "mae_improves_by_at_least_0_10": (
            none_overall.get("mae") is not None
            and low_overall.get("mae") is not None
            and none_overall["mae"] - low_overall["mae"] >= 0.10
        ),
        "no_case_worsens_by_over_0_5": no_large_regression,
        "low_band_segment_not_worse": low_not_worse,
        "all_spreads_at_most_0_5": len(low_spreads) == 7 and all(
            spread is not None and spread <= 0.5 for spread in low_spreads
        ),
        "success_rate_not_worse": low_overall.get("success_rate", 0) >= none_overall.get("success_rate", 0),
    }


def render_report(named_runs: list[tuple[str, dict[str, Any]]]) -> str:
    labels = [name for name, _ in named_runs]
    maps = {name: case_map(run) for name, run in named_runs}
    case_ids = sorted(set().union(*(mapping.keys() for mapping in maps.values())))
    rows = []
    for case_id in case_ids:
        expected = next(
            mapping[case_id]["expected_overall"] for mapping in maps.values() if case_id in mapping
        )
        cells = [case_id, f"{expected:.1f}"]
        for name in labels:
            item = maps[name].get(case_id, {})
            mean_overall = item.get("mean_scores", {}).get("Overall Band")
            error = item.get("absolute_error")
            spread = item.get("max_spread", {}).get("Overall Band")
            cells.extend(
                [
                    "failed" if mean_overall is None else f"{mean_overall:.2f}",
                    "n/a" if error is None else f"{error:.2f}",
                    "n/a" if spread is None else f"{spread:.1f}",
                ]
            )
        rows.append("| " + " | ".join(cells) + " |")

    headers = ["Case", "Official"]
    for name in labels:
        headers.extend([f"{name} mean", f"{name} error", f"{name} spread"])
    lines = [
        "# EssayPilot calibration comparison",
        "",
        "Private official anchors are identified only by opaque case IDs.",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] + ["---:"] * (len(headers) - 1)) + " |",
        *rows,
        "",
        "## Aggregate",
        "",
        "| Run | MAE | Within +/-0.5 | Max error | Success | Cost (USD) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, run in named_runs:
        overall = run["summary"]["overall"]
        usage = run.get("metadata", {}).get("usage", {})
        lines.append(
            f"| {name} | {overall.get('mae')} | {overall.get('within_0_5_rate')} | "
            f"{overall.get('max_absolute_error')} | {overall.get('successful_runs')}/"
            f"{overall.get('attempted_runs')} | {usage.get('estimated_usd')} |"
        )
    candidate_name, candidate = named_runs[1]
    checks = acceptance(candidate)
    lines.extend(["", f"## Acceptance: {candidate_name}", ""])
    lines.extend(f"- [{'x' if passed else ' '}] {name}" for name, passed in checks.items())
    if len(named_runs) >= 3:
        alternative_name, alternative = named_runs[2]
        gate = reasoning_gate(candidate, alternative)
        lines.extend(["", f"## Reasoning adoption gate: {alternative_name}", ""])
        lines.extend(f"- [{'x' if passed else ' '}] {name}" for name, passed in gate.items())
        lines.append("")
        lines.append(f"Adopt {alternative_name}: {'yes' if all(gate.values()) else 'no'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--alternative", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = [("baseline", load_run(args.baseline)), ("improved-none", load_run(args.candidate))]
    if args.alternative:
        runs.append(("improved-low", load_run(args.alternative)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_report(runs), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

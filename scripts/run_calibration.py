"""Run paid repeatability or private gold-set evaluation for Task 2 scoring."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ai_grader import (
    PRODUCTION_MODEL_SNAPSHOT,
    grade_essay_package,
    grade_scoring_decision,
)
from src.report_schema import PROMPT_VERSION, SCHEMA_VERSION, SKILL_VERSION, score_snapshot


SCORE_KEYS = (
    "Overall Band",
    "Task Response",
    "Coherence & Cohesion",
    "Lexical Resource",
    "Grammar Range & Accuracy",
)

MODEL_PRICES_USD_PER_MILLION = {
    ("OpenAI", "gpt-5.4-mini-2026-03-17"): (0.75, 4.50),
    ("OpenAI", "gpt-5.4-2026-03-05"): (2.50, 15.00),
    ("OpenAI", "gpt-5.5-2026-04-23"): (5.00, 30.00),
    ("DeepSeek", "deepseek-v4-flash"): (0.14, 0.28),
    ("DeepSeek", "deepseek-v4-pro"): (0.435, 0.87),
}


@dataclass(frozen=True)
class BlindModelInput:
    task_prompt: str
    candidate_response: str


@dataclass(frozen=True)
class EvaluationLabel:
    case_id: str
    expected_overall: float | None = None
    examiner_comment: str = ""
    source_reference: str = ""
    expected_overall_range: tuple[float, float] | None = None


@dataclass(frozen=True)
class CalibrationCase:
    model_input: BlindModelInput
    evaluation: EvaluationLabel
    source_type: str
    provenance: str
    split: str = "unspecified"


def normalize_cases(payload: Any) -> list[CalibrationCase]:
    """Accept the private split schema and the legacy public synthetic fixtures."""
    raw_cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("Calibration dataset must contain a list of cases.")
    cases: list[CalibrationCase] = []
    dataset_source = str(payload.get("source_type", "")) if isinstance(payload, dict) else ""
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("Every calibration case must be an object.")
        if "model_input" in raw:
            model_input = raw.get("model_input") or {}
            evaluation = raw.get("evaluation") or {}
            case = CalibrationCase(
                model_input=BlindModelInput(
                    task_prompt=str(model_input.get("task_prompt") or ""),
                    candidate_response=str(model_input.get("candidate_response") or ""),
                ),
                evaluation=EvaluationLabel(
                    case_id=str(evaluation.get("case_id") or ""),
                    expected_overall=(
                        float(evaluation["expected_overall"])
                        if evaluation.get("expected_overall") is not None
                        else None
                    ),
                    examiner_comment=str(evaluation.get("examiner_comment") or ""),
                    source_reference=str(evaluation.get("source_reference") or ""),
                    expected_overall_range=(
                        tuple(float(value) for value in evaluation["expected_overall_range"])
                        if evaluation.get("expected_overall_range") is not None
                        else None
                    ),
                ),
                source_type=str(raw.get("source_type") or dataset_source),
                provenance=str(evaluation.get("source_heading") or raw.get("provenance") or ""),
                split=str(raw.get("split") or "unspecified"),
            )
        else:
            gold = raw.get("gold") if isinstance(raw.get("gold"), dict) else {}
            case = CalibrationCase(
                model_input=BlindModelInput(
                    task_prompt=str(raw.get("question") or ""),
                    candidate_response=str(raw.get("essay") or ""),
                ),
                evaluation=EvaluationLabel(
                    case_id=str(raw.get("id") or ""),
                    expected_overall=float(gold["overall"]) if gold.get("overall") is not None else None,
                ),
                source_type=str(raw.get("source_type") or dataset_source),
                provenance=str(raw.get("provenance") or ""),
                split=str(raw.get("split") or "unspecified"),
            )
        cases.append(case)
    return cases


def apply_split_manifest(cases: list[CalibrationCase], payload: dict[str, Any]) -> list[CalibrationCase]:
    """Apply private split membership and interval labels without altering model input."""
    split_by_id: dict[str, str] = {}
    for split_name in ("development", "holdout", "sensitivity"):
        for case_id in payload.get(split_name, []):
            if case_id in split_by_id:
                raise ValueError(f"{case_id}: appears in more than one calibration split")
            split_by_id[str(case_id)] = split_name
    ranges = payload.get("label_ranges") or {}
    known_ids = {case.evaluation.case_id for case in cases}
    unknown = (set(split_by_id) | set(ranges)) - known_ids
    if unknown:
        raise ValueError(f"Unknown case ids in split manifest: {sorted(unknown)}")
    result = []
    for case in cases:
        interval = ranges.get(case.evaluation.case_id)
        evaluation = case.evaluation
        if interval is not None:
            if not isinstance(interval, list) or len(interval) != 2:
                raise ValueError(f"{evaluation.case_id}: label range must contain two bands")
            low, high = (float(interval[0]), float(interval[1]))
            if low > high or any(value < 0 or value > 9 for value in (low, high)):
                raise ValueError(f"{evaluation.case_id}: invalid label range")
            evaluation = replace(evaluation, expected_overall_range=(low, high))
        result.append(
            replace(case, evaluation=evaluation, split=split_by_id.get(evaluation.case_id, case.split))
        )
    return result


def validate_dataset(cases: list[Any], mode: str) -> None:
    """Validate provenance and labels without requiring unavailable criterion gold."""
    normalized = cases if cases and isinstance(cases[0], CalibrationCase) else normalize_cases(cases)
    if not normalized:
        raise ValueError("The calibration dataset is empty.")
    for case in normalized:
        missing = []
        if not case.evaluation.case_id:
            missing.append("case id")
        if not case.model_input.task_prompt:
            missing.append("task prompt")
        if not case.model_input.candidate_response:
            missing.append("candidate response")
        if not case.source_type:
            missing.append("source type")
        if not case.provenance:
            missing.append("provenance")
        if missing:
            raise ValueError(f"{case.evaluation.case_id or '<unknown>'}: missing {', '.join(missing)}")
        if mode == "gold":
            if case.source_type not in {"official", "official_internal", "human_gold"}:
                raise ValueError(f"{case.evaluation.case_id}: synthetic cases cannot be used as gold labels")
            expected = case.evaluation.expected_overall
            if expected is None or expected < 0 or expected > 9 or expected * 2 != int(expected * 2):
                raise ValueError(f"{case.evaluation.case_id}: a valid official half-band is required")
            interval = case.evaluation.expected_overall_range
            if interval is not None:
                if len(interval) != 2 or interval[0] > interval[1] or any(
                    value < 0 or value > 9 or value * 2 != int(value * 2)
                    for value in interval
                ):
                    raise ValueError(f"{case.evaluation.case_id}: invalid official interval label")


def repeatability_metrics(snapshots: list[dict[str, float | None]]) -> dict[str, Any]:
    keys = tuple(snapshots[0])
    values = {key: [float(item[key]) for item in snapshots if item.get(key) is not None] for key in keys}
    vector_counts: dict[str, int] = defaultdict(int)
    for item in snapshots:
        vector = tuple(item.get(key) for key in keys if key != "Overall Band")
        vector_counts[str(vector)] += 1
    return {
        "criterion_vector_exact_agreement": max(vector_counts.values()) / len(snapshots),
        "max_spread": {key: max(items) - min(items) for key, items in values.items()},
        "means": {key: mean(items) for key, items in values.items()},
    }


def gold_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Back-compatible flat gold metrics; criterion labels remain optional."""
    errors: dict[str, list[float]] = defaultdict(list)
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for record in records:
        predicted = record["predicted"]
        gold = record["gold"]
        pairs = {"Overall Band": (predicted["Overall Band"], gold["overall"])}
        criteria = gold.get("criteria") or {}
        criterion_keys = {
            "Task Response": "Task Response",
            "Coherence and Cohesion": "Coherence & Cohesion",
            "Lexical Resource": "Lexical Resource",
            "Grammatical Range and Accuracy": "Grammar Range & Accuracy",
        }
        pairs.update(
            (criterion_keys[name], (predicted[criterion_keys[name]], value))
            for name, value in criteria.items()
            if name in criterion_keys and predicted.get(criterion_keys[name]) is not None
        )
        for name, (prediction, target) in pairs.items():
            difference = float(prediction) - float(target)
            errors[name].append(difference)
            confusion[name][f"{target}->{prediction}"] += 1
    return {
        "mae": {name: mean(abs(value) for value in values) for name, values in errors.items()},
        "within_0_5_rate": {name: mean(abs(value) <= 0.5 for value in values) for name, values in errors.items()},
        "within_1_0_rate": {name: mean(abs(value) <= 1.0 for value in values) for name, values in errors.items()},
        "mean_bias": {name: mean(values) for name, values in errors.items()},
        "max_absolute_error": {name: max(abs(value) for value in values) for name, values in errors.items()},
        "confusion_matrix": {name: dict(cells) for name, cells in confusion.items()},
        "weighted_kappa": None,
        "weighted_kappa_note": "Not reported for this small, overall-only official anchor set.",
    }


def summarize_gold(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[float] = []
    high_bias: list[float] = []
    low_bias: list[float] = []
    within_0_5 = 0
    within_1_0 = 0
    summaries: list[dict[str, Any]] = []
    attempted_runs = 0
    successful_runs = 0
    for result in case_results:
        expected = float(result["expected_overall"])
        expected_range = result.get("expected_overall_range")
        attempted_runs += len(result["runs"])
        valid_runs = [run for run in result["runs"] if run.get("status", "ok") == "ok"]
        successful_runs += len(valid_runs)
        if not valid_runs:
            summaries.append(
                {
                    "case_id": result["case_id"],
                    "expected_overall": expected,
                    "expected_overall_range": expected_range,
                    "mean_scores": {key: None for key in SCORE_KEYS},
                    "absolute_error": None,
                    "signed_error": None,
                    "max_spread": {key: None for key in SCORE_KEYS},
                    "successful_runs": 0,
                    "failed_runs": len(result["runs"]),
                }
            )
            continue
        metrics = repeatability_metrics([run["snapshot"] for run in valid_runs])
        predicted_mean = metrics["means"]["Overall Band"]
        if expected_range is not None:
            low, high = (float(expected_range[0]), float(expected_range[1]))
            if predicted_mean < low:
                signed_error = predicted_mean - low
            elif predicted_mean > high:
                signed_error = predicted_mean - high
            else:
                signed_error = 0.0
        else:
            signed_error = predicted_mean - expected
        absolute_error = abs(signed_error)
        errors.append(absolute_error)
        within_0_5 += absolute_error <= 0.5
        within_1_0 += absolute_error <= 1.0
        if expected >= 7.0:
            high_bias.append(signed_error)
        if expected <= 5.5:
            low_bias.append(signed_error)
        summaries.append(
            {
                "case_id": result["case_id"],
                "expected_overall": expected,
                "expected_overall_range": expected_range,
                "mean_scores": metrics["means"],
                "absolute_error": absolute_error,
                "signed_error": signed_error,
                "max_spread": metrics["max_spread"],
                "successful_runs": len(valid_runs),
                "failed_runs": len(result["runs"]) - len(valid_runs),
            }
        )
    return {
        "cases": summaries,
        "overall": {
            "mae": mean(errors) if errors else None,
            "within_0_5_rate": within_0_5 / len(errors) if errors else None,
            "within_1_0_rate": within_1_0 / len(errors) if errors else None,
            "max_absolute_error": max(errors) if errors else None,
            "high_band_mean_bias": mean(high_bias) if high_bias else None,
            "low_band_mean_bias": mean(low_bias) if low_bias else None,
            "attempted_runs": attempted_runs,
            "successful_runs": successful_runs,
            "failed_runs": attempted_runs - successful_runs,
            "success_rate": successful_runs / attempted_runs if attempted_runs else None,
        },
    }


def acceptance_status(summary: dict[str, Any], split: str) -> dict[str, Any]:
    """Evaluate the preregistered development or holdout quality gates."""
    cases = [item for item in summary.get("cases", []) if item.get("absolute_error") is not None]
    required_hits = 6 if split == "development" else 5
    expected_cases = 7 if split == "development" else 6
    hits = sum(float(item["absolute_error"]) <= 0.5 for item in cases)
    spread_ok = all(
        float(item["max_spread"]["Overall Band"]) <= 0.5 for item in cases
    )
    overall = summary.get("overall", {})
    checks = {
        "complete_case_count": len(cases) == expected_cases,
        "within_0_5_case_count": hits >= required_hits,
        "mae": overall.get("mae") is not None and float(overall["mae"]) <= 0.5,
        "max_absolute_error": (
            overall.get("max_absolute_error") is not None
            and float(overall["max_absolute_error"]) <= 1.0
        ),
        "overall_spread": spread_ok,
    }
    return {
        "split": split,
        "passed": all(checks.values()),
        "checks": checks,
        "within_0_5_cases": hits,
        "required_within_0_5_cases": required_hits,
        "expected_cases": expected_cases,
    }


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csvs(output: Path, case_results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    with (output / "runs.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "case_id", "run", "expected_overall", "status", *SCORE_KEYS,
                "provider", "model", "response_model", "system_fingerprint",
                "latency_seconds", "input_tokens", "output_tokens", "total_tokens",
                "error_type", "error_message",
            ],
        )
        writer.writeheader()
        for case in case_results:
            for run_index, run in enumerate(case["runs"], 1):
                writer.writerow(
                    {
                        "case_id": case["case_id"],
                        "run": run_index,
                        "expected_overall": case.get("expected_overall"),
                        "status": run.get("status", "ok"),
                        **(run.get("snapshot") or {}),
                        "provider": run.get("provider"),
                        "model": run.get("model"),
                        "response_model": run.get("response_model"),
                        "system_fingerprint": run.get("system_fingerprint"),
                        "latency_seconds": run["latency_seconds"],
                        **run["usage"],
                        "error_type": run.get("error_type"),
                        "error_message": run.get("error_message"),
                    }
                )
    with (output / "summary.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "expected_overall", "mean_overall", "absolute_error", "signed_error", "overall_spread"])
        writer.writeheader()
        for item in summary.get("cases", []):
            writer.writerow(
                {
                    "case_id": item["case_id"],
                    "expected_overall": item["expected_overall"],
                    "mean_overall": item["mean_scores"]["Overall Band"],
                    "absolute_error": item["absolute_error"],
                    "signed_error": item["signed_error"],
                    "overall_spread": item["max_spread"]["Overall Band"],
                }
            )


def _markdown(summary: dict[str, Any], metadata: dict[str, Any]) -> str:
    rows = []
    for item in summary.get("cases", []):
        predicted = item["mean_scores"]["Overall Band"]
        attempts = item.get("successful_runs", 0) + item.get("failed_runs", 0)
        if predicted is None:
            rows.append(
                f"| {item['case_id']} | {item['expected_overall']:.1f} | failed | n/a | n/a | "
                f"{item.get('successful_runs', 0)}/{attempts} |"
            )
            continue
        rows.append(
            f"| {item['case_id']} | {item['expected_overall']:.1f} | "
            f"{predicted:.2f} | {item['absolute_error']:.2f} | "
            f"{item['max_spread']['Overall Band']:.1f} | "
            f"{item.get('successful_runs', attempts)}/{attempts} |"
        )
    overall = summary.get("overall", {})
    metric = lambda name: "n/a" if overall.get(name) is None else f"{overall[name]:.3f}"
    rate = lambda name: "n/a" if overall.get(name) is None else f"{overall[name]:.1%}"
    return f"""# EssayPilot private calibration report

This is a small official-anchor calibration, not a claim of examiner-level accuracy.

- Label: {metadata['label']}
- Provider: {metadata.get('provider')}
- Model: {metadata['model']}
- Pipeline: {metadata.get('pipeline', 'score-only')}
- Repeats: {metadata['repeats']}
- Prompt version: {metadata['prompt_version']}
- Skill version: {metadata['skill_version']}
- Schema version: {metadata['schema_version']}
- Reasoning effort: {metadata.get('reasoning_effort', 'none')}
- Estimated API cost: ${metadata.get('usage', {}).get('estimated_usd', 0):.4f}
- Scoring latency total: {metadata.get('stage_latency_seconds', {}).get('scoring', 'not captured')} seconds
- Teaching latency total: {metadata.get('stage_latency_seconds', {}).get('teaching', 'not captured')} seconds

| Case | Official | Mean predicted | Absolute error | Max spread | Successful runs |
|---|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## Aggregate

- Overall MAE: {metric('mae')}
- Within +/-0.5: {rate('within_0_5_rate')}
- Within +/-1.0: {rate('within_1_0_rate')}
- Maximum error: {metric('max_absolute_error')}
- High-band mean bias: {overall.get('high_band_mean_bias')}
- Low-band mean bias: {overall.get('low_band_mean_bias')}
- Successful runs: {overall.get('successful_runs', 0)}/{overall.get('attempted_runs', 0)}
- Failed runs: {overall.get('failed_runs', 0)}
- Acceptance: {metadata.get('acceptance', {}).get('passed', 'not evaluated')}
"""


def run_evaluation(
    cases: list[CalibrationCase],
    repeats: int,
    grader: Callable[..., dict[str, object]] = grade_scoring_decision,
    reasoning_effort: str = "none",
    provider: str = "OpenAI",
    model: str = PRODUCTION_MODEL_SNAPSHOT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run blind production calls; labels never cross the grader boundary."""
    results: list[dict[str, Any]] = []
    audit_events: list[dict[str, Any]] = []
    for case in cases:
        runs = []
        for run_number in range(1, repeats + 1):
            event_batch: list[dict[str, Any]] = []
            started = time.perf_counter()
            try:
                package = grader(
                    task_type="Task 2",
                    topic=case.model_input.task_prompt,
                    essay=case.model_input.candidate_response,
                    audit_hook=event_batch.append,
                    reasoning_effort=reasoning_effort,
                    provider=provider,
                    model=model,
                )
                latency = time.perf_counter() - started
                structured = dict(package["structured"])
                usage = dict(package.get("usage") or {})
                runs.append(
                    {
                        "status": "ok",
                        "snapshot": score_snapshot(structured),
                        "latency_seconds": round(latency, 3),
                        "usage": {
                            "input_tokens": usage.get("input_tokens"),
                            "output_tokens": usage.get("output_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                        },
                        "stage_usage": package.get("stage_usage"),
                        "model": package.get("model"),
                        "provider": package.get("provider", provider),
                        "response_model": package.get("response_model"),
                        "system_fingerprint": package.get("system_fingerprint"),
                        "prompt_version": package.get("prompt_version"),
                    }
                )
            except Exception as exc:
                event_usage = [event.get("usage") or {} for event in event_batch]
                def summed_usage(name: str) -> int | None:
                    values = [usage.get(name) for usage in event_usage]
                    return sum(int(value) for value in values if value is not None) if any(value is not None for value in values) else None

                def stage_usage(stage: str) -> dict[str, int | None]:
                    stage_events = [
                        event for event in event_batch if event.get("stage") == stage
                    ]
                    return {
                        name: (
                            sum(
                                int((event.get("usage") or {}).get(name))
                                for event in stage_events
                                if (event.get("usage") or {}).get(name) is not None
                            )
                            if any(
                                (event.get("usage") or {}).get(name) is not None
                                for event in stage_events
                            )
                            else None
                        )
                        for name in ("input_tokens", "output_tokens", "total_tokens")
                    }

                runs.append(
                    {
                        "status": "error",
                        "snapshot": None,
                        "latency_seconds": round(time.perf_counter() - started, 3),
                        "usage": {
                            "input_tokens": summed_usage("input_tokens"),
                            "output_tokens": summed_usage("output_tokens"),
                            "total_tokens": summed_usage("total_tokens"),
                        },
                        "stage_usage": {
                            "scoring": stage_usage("scoring"),
                            "teaching": stage_usage("teaching"),
                        },
                        "model": model,
                        "provider": provider,
                        "prompt_version": PROMPT_VERSION,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
            forbidden = {
                case.evaluation.case_id,
                case.evaluation.examiner_comment,
                case.evaluation.source_reference,
                case.provenance,
            }
            request_text = json.dumps(
                [event.get("messages") for event in event_batch], ensure_ascii=False
            )
            leaked = [value for value in forbidden if value and value in request_text]
            if leaked:
                raise RuntimeError("Evaluation metadata leaked into a model request.")
            for event in event_batch:
                event["case_id"] = case.evaluation.case_id
                event["run"] = run_number
            audit_events.extend(event_batch)
            print(
                f"[{case.evaluation.case_id}] run {run_number}/{repeats}: {runs[-1]['status']}",
                file=sys.stderr,
                flush=True,
            )
        results.append(
            {
                "case_id": case.evaluation.case_id,
                "expected_overall": case.evaluation.expected_overall,
                "expected_overall_range": (
                    list(case.evaluation.expected_overall_range)
                    if case.evaluation.expected_overall_range is not None
                    else None
                ),
                "split": case.split,
                "runs": runs,
            }
        )
    return results, audit_events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "data" / "calibration_cases.json")
    parser.add_argument("--mode", choices=("repeatability", "gold"), default="repeatability")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--case", default="")
    parser.add_argument("--split-manifest", type=Path)
    parser.add_argument(
        "--subset", choices=("all", "development", "holdout", "sensitivity"), default="all"
    )
    parser.add_argument("--label", default="calibration")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".private" / "calibration" / "runs")
    parser.add_argument("--reasoning-effort", choices=("none", "low"), default="none")
    parser.add_argument("--provider", choices=("OpenAI", "DeepSeek"), default="OpenAI")
    parser.add_argument("--model", default=PRODUCTION_MODEL_SNAPSHOT)
    parser.add_argument(
        "--full-package", action="store_true",
        help="Also generate teaching feedback; candidate screening is score-only by default.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    args = parser.parse_args()
    repeats = args.repeats if args.repeats is not None else (5 if args.mode == "repeatability" else 3)
    if repeats < (2 if args.mode == "repeatability" else 1):
        parser.error("repeatability needs at least 2 runs; gold mode needs at least 1")
    default_prices = MODEL_PRICES_USD_PER_MILLION.get((args.provider, args.model))
    if default_prices is None and (
        args.input_price_per_million is None or args.output_price_per_million is None
    ):
        parser.error(
            "Unknown model pricing: provide both --input-price-per-million and "
            "--output-price-per-million."
        )

    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = normalize_cases(payload)
    if args.split_manifest:
        split_payload = json.loads(args.split_manifest.read_text(encoding="utf-8"))
        cases = apply_split_manifest(cases, split_payload)
    if args.subset != "all":
        cases = [case for case in cases if case.split == args.subset]
    if args.case:
        cases = [case for case in cases if case.evaluation.case_id == args.case]
    validate_dataset(cases, args.mode)
    if args.dry_run:
        print(json.dumps({
            "ok": True,
            "mode": args.mode,
            "cases": len(cases),
            "subset": args.subset,
            "provider": args.provider,
            "model": args.model,
            "pipeline": "full-package" if args.full_package else "score-only",
            "labels_isolated": True,
        }, indent=2))
        return 0

    load_dotenv()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir / f"{args.label}-{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    if args.full_package:
        def selected_grader(**kwargs: Any) -> dict[str, object]:
            provider = str(kwargs.pop("provider"))
            model = str(kwargs.pop("model"))
            return grade_essay_package(
                **kwargs, scoring_provider=provider, scoring_model=model
            )
    else:
        selected_grader = grade_scoring_decision
    results, audit_events = run_evaluation(
        cases,
        repeats,
        grader=selected_grader,
        reasoning_effort=args.reasoning_effort,
        provider=args.provider,
        model=args.model,
    )
    summary = summarize_gold(results) if args.mode == "gold" else {"cases": results}
    total_input = sum((run["usage"].get("input_tokens") or 0) for case in results for run in case["runs"])
    total_output = sum((run["usage"].get("output_tokens") or 0) for case in results for run in case["runs"])
    input_price = args.input_price_per_million
    output_price = args.output_price_per_million
    if input_price is None:
        input_price = default_prices[0] if default_prices else 0.0
    if output_price is None:
        output_price = default_prices[1] if default_prices else 0.0
    acceptance = (
        acceptance_status(summary, args.subset)
        if args.mode == "gold" and args.subset in {"development", "holdout"}
        else {}
    )
    estimated_cost = total_input / 1_000_000 * input_price + total_output / 1_000_000 * output_price
    cost_breakdown: dict[str, Any] = {}
    if args.full_package:
        scoring_input = sum(
            int(((run.get("stage_usage") or {}).get("scoring") or {}).get("input_tokens") or 0)
            for case in results for run in case["runs"]
        )
        scoring_output = sum(
            int(((run.get("stage_usage") or {}).get("scoring") or {}).get("output_tokens") or 0)
            for case in results for run in case["runs"]
        )
        teaching_input = sum(
            int(((run.get("stage_usage") or {}).get("teaching") or {}).get("input_tokens") or 0)
            for case in results for run in case["runs"]
        )
        teaching_output = sum(
            int(((run.get("stage_usage") or {}).get("teaching") or {}).get("output_tokens") or 0)
            for case in results for run in case["runs"]
        )
        scoring_cost = scoring_input / 1_000_000 * input_price + scoring_output / 1_000_000 * output_price
        teaching_cost = teaching_input / 1_000_000 * 0.75 + teaching_output / 1_000_000 * 4.50
        estimated_cost = scoring_cost + teaching_cost
        cost_breakdown = {
            "scoring_usd": round(scoring_cost, 6),
            "teaching_usd": round(teaching_cost, 6),
            "teaching_model": "gpt-5.4-mini-2026-03-17",
        }
    metadata = {
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": args.mode,
        "repeats": repeats,
        "provider": args.provider,
        "model": args.model,
        "pipeline": "full-package" if args.full_package else "score-only",
        "subset": args.subset,
        "reasoning_effort": args.reasoning_effort,
        "prompt_version": PROMPT_VERSION,
        "skill_version": SKILL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "dataset_sha256": _file_sha256(args.dataset),
        "split_manifest_sha256": (
            _file_sha256(args.split_manifest) if args.split_manifest else None
        ),
        "response_models": sorted({
            str(run.get("response_model"))
            for case in results for run in case["runs"] if run.get("response_model")
        }),
        "system_fingerprints": sorted({
            str(run.get("system_fingerprint"))
            for case in results for run in case["runs"] if run.get("system_fingerprint")
        }),
        "acceptance": acceptance,
        "production_file_sha256": {
            str(path.relative_to(ROOT)): _file_sha256(path)
            for path in (
                ROOT / "src" / "ai_grader.py",
                ROOT / "src" / "prompts.py",
                ROOT / "src" / "report_schema.py",
                ROOT / "src" / "text_utils.py",
                ROOT / "skills" / "ielts-writing" / "SKILL.md",
                ROOT / "skills" / "ielts-writing" / "references" / "scoring-protocol.md",
            )
        },
        "stage_latency_seconds": {
            stage: round(sum(float(event.get("latency_seconds") or 0) for event in audit_events if event.get("stage") == stage), 3)
            for stage in ("scoring", "teaching")
        },
        "usage": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "estimated_usd": round(estimated_cost, 6),
            "input_price_per_million": input_price,
            "output_price_per_million": output_price,
            "breakdown": cost_breakdown,
        },
    }
    run_payload = {"metadata": metadata, "summary": summary, "results": results, "audit_events": audit_events}
    (output / "run.json").write_text(json.dumps(run_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csvs(output, results, summary)
    (output / "report.md").write_text(_markdown(summary, metadata), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(output),
        "summary": summary.get("overall"),
        "acceptance": acceptance,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

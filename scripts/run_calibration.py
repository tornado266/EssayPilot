"""Run paid repeatability or private gold-set evaluation for Task 2 scoring."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ai_grader import PRODUCTION_MODEL_SNAPSHOT, grade_essay_package
from src.report_schema import PROMPT_VERSION, SCHEMA_VERSION, SKILL_VERSION, score_snapshot


SCORE_KEYS = (
    "Overall Band",
    "Task Response",
    "Coherence & Cohesion",
    "Lexical Resource",
    "Grammar Range & Accuracy",
)


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


@dataclass(frozen=True)
class CalibrationCase:
    model_input: BlindModelInput
    evaluation: EvaluationLabel
    source_type: str
    provenance: str


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
                ),
                source_type=str(raw.get("source_type") or dataset_source),
                provenance=str(evaluation.get("source_heading") or raw.get("provenance") or ""),
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
            )
        cases.append(case)
    return cases


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
        attempted_runs += len(result["runs"])
        valid_runs = [run for run in result["runs"] if run.get("status", "ok") == "ok"]
        successful_runs += len(valid_runs)
        if not valid_runs:
            summaries.append(
                {
                    "case_id": result["case_id"],
                    "expected_overall": expected,
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


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_csvs(output: Path, case_results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    with (output / "runs.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "case_id", "run", "expected_overall", "status", *SCORE_KEYS,
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
- Model: {metadata['model']}
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
"""


def run_evaluation(
    cases: list[CalibrationCase],
    repeats: int,
    grader: Callable[..., dict[str, object]] = grade_essay_package,
    reasoning_effort: str = "none",
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
                        "model": package.get("model"),
                        "prompt_version": package.get("prompt_version"),
                    }
                )
            except Exception as exc:
                event_usage = [event.get("usage") or {} for event in event_batch]
                def summed_usage(name: str) -> int | None:
                    values = [usage.get(name) for usage in event_usage]
                    return sum(int(value) for value in values if value is not None) if any(value is not None for value in values) else None

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
                        "model": PRODUCTION_MODEL_SNAPSHOT,
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
    parser.add_argument("--label", default="calibration")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".private" / "calibration" / "runs")
    parser.add_argument("--reasoning-effort", choices=("none", "low"), default="none")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--input-price-per-million", type=float, default=0.75)
    parser.add_argument("--output-price-per-million", type=float, default=4.50)
    args = parser.parse_args()
    repeats = args.repeats if args.repeats is not None else (5 if args.mode == "repeatability" else 3)
    if repeats < (2 if args.mode == "repeatability" else 1):
        parser.error("repeatability needs at least 2 runs; gold mode needs at least 1")

    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = normalize_cases(payload)
    if args.case:
        cases = [case for case in cases if case.evaluation.case_id == args.case]
    validate_dataset(cases, args.mode)
    if args.dry_run:
        print(json.dumps({"ok": True, "mode": args.mode, "cases": len(cases), "labels_isolated": True}, indent=2))
        return 0

    load_dotenv()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir / f"{args.label}-{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    results, audit_events = run_evaluation(cases, repeats, reasoning_effort=args.reasoning_effort)
    summary = summarize_gold(results) if args.mode == "gold" else {"cases": results}
    total_input = sum((run["usage"].get("input_tokens") or 0) for case in results for run in case["runs"])
    total_output = sum((run["usage"].get("output_tokens") or 0) for case in results for run in case["runs"])
    metadata = {
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": args.mode,
        "repeats": repeats,
        "model": (
            results[0]["runs"][0].get("model")
            if results and results[0]["runs"]
            else PRODUCTION_MODEL_SNAPSHOT
        ),
        "reasoning_effort": args.reasoning_effort,
        "prompt_version": PROMPT_VERSION,
        "skill_version": SKILL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "dataset_sha256": _file_sha256(args.dataset),
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
            "estimated_usd": round(total_input / 1_000_000 * args.input_price_per_million + total_output / 1_000_000 * args.output_price_per_million, 6),
            "input_price_per_million": args.input_price_per_million,
            "output_price_per_million": args.output_price_per_million,
        },
    }
    run_payload = {"metadata": metadata, "summary": summary, "results": results, "audit_events": audit_events}
    (output / "run.json").write_text(json.dumps(run_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csvs(output, results, summary)
    (output / "report.md").write_text(_markdown(summary, metadata), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "summary": summary.get("overall")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

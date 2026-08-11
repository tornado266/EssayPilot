#!/usr/bin/env python3
"""Run gated mixed-model feedback evaluation; dry-run by default."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.ai_grader import grade_essay_package, grade_scoring_decision  # noqa: E402
from src.kaggle_skill_dataset import (  # noqa: E402
    feedback_metrics,
    load_skill_split,
    prediction_priority_tags,
)
from src.report_schema import (  # noqa: E402
    FEEDBACK_PROMPT_VERSION,
    FEEDBACK_SKILL_VERSION,
    SCORING_PROMPT_VERSION,
    SCORING_SKILL_VERSION,
    feedback_quality_flags,
    validate_scoring_decision,
)


PRICE_PER_MILLION = {
    ("DeepSeek", "deepseek-v4-pro"): (0.435, 0.87),
    ("OpenAI", "gpt-5.4-mini-2026-03-17"): (0.75, 4.5),
}
EXPECTED_COUNTS = {"validation": 8, "official": 14, "holdout": 12}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=tuple(EXPECTED_COUNTS), default="validation")
    parser.add_argument("--execute", action="store_true", help="Explicitly authorize paid calls.")
    parser.add_argument("--unlock-holdout", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--metrics-only", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--scoring-provider", choices=("OpenAI", "DeepSeek"), default="OpenAI")
    parser.add_argument("--scoring-model", default="gpt-5.4-mini-2026-03-17")
    parser.add_argument("--scoring-reasoning-effort", choices=("none", "low", "high", "max"), default="none")
    parser.add_argument("--feedback-provider", choices=("OpenAI", "DeepSeek"), default="DeepSeek")
    parser.add_argument("--feedback-model", default="deepseek-v4-pro")
    parser.add_argument("--feedback-reasoning-effort", choices=("none", "low", "high", "max"), default="none")
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT_DIR / ".private" / "kaggle_ielts" / "feedback_eval_v2",
    )
    return parser.parse_args()


def _load_official_cases() -> list[dict[str, Any]]:
    path = ROOT_DIR / ".private" / "calibration" / "official_task2-expanded.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError("The private official dataset has no cases array.")
    cases = []
    for raw in records:
        model_input = raw.get("model_input") or {}
        evaluation = raw.get("evaluation") or {}
        cases.append({
            "case_id": str(evaluation.get("case_id") or raw.get("case_id") or ""),
            "question": str(model_input.get("task_prompt") or ""),
            "essay_clean": str(model_input.get("candidate_response") or ""),
            "structured_examiner_feedback": {},
        })
    if len(cases) != EXPECTED_COUNTS["official"] or any(
        not case["case_id"] or not case["question"] or not case["essay_clean"] for case in cases
    ):
        raise ValueError("The private official feedback audit requires 14 complete blind inputs.")
    return cases


def load_cases(split: str, *, unlock_holdout: bool) -> list[dict[str, Any]]:
    if split == "official":
        return _load_official_cases()
    path = (
        ROOT_DIR / ".private" / "kaggle_ielts" / "examiner_claimed_holdout.jsonl"
        if split == "holdout"
        else ROOT_DIR / "data" / "processed" / "kaggle_ielts" / "examiner_claimed_validation.jsonl"
    )
    return load_skill_split(path, split=split, unlock_holdout=unlock_holdout)


def _load_latest(path: Path, *, status: str = "complete") -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("case_id"):
            latest[str(record["case_id"])] = record
    return {
        case_id: record for case_id, record in latest.items()
        if record.get("status") == status
    }


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def import_official_score_locks(
    cases: list[dict[str, Any]], path: Path, profile: dict[str, str]
) -> int:
    """Rebuild reusable blind locks from the existing v10 official audit responses."""
    candidates = sorted(
        (ROOT_DIR / ".private" / "calibration" / "runs").glob(
            "skill-v10-official-once-*/run.json"
        ),
        reverse=True,
    )
    if not candidates:
        return 0
    source = json.loads(candidates[0].read_text(encoding="utf-8"))
    case_map = {str(case["case_id"]): case for case in cases}
    rebuilt: dict[str, dict[str, Any]] = {}
    for event in source.get("audit_events") or []:
        case_id = str(event.get("case_id") or "")
        if (
            case_id not in case_map
            or event.get("stage") != "scoring"
            or event.get("provider") != profile["scoring_provider"]
            or event.get("model") != profile["scoring_model"]
            or event.get("reasoning_effort") != profile["scoring_reasoning_effort"]
        ):
            continue
        try:
            scoring = validate_scoring_decision(
                json.loads(str(event.get("raw_response") or "")),
                str(case_map[case_id]["essay_clean"]),
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        scoring_package = {
            "provider": profile["scoring_provider"],
            "model": profile["scoring_model"],
            "response_model": event.get("response_model"),
            "system_fingerprint": event.get("system_fingerprint"),
            "reasoning_effort": profile["scoring_reasoning_effort"],
            "prompt_version": profile["scoring_prompt_version"],
            "skill_version": profile["scoring_skill_version"],
            "scoring": scoring, "structured": scoring,
            "usage": event.get("usage") or {},
        }
        rebuilt[case_id] = {
            "case_id": case_id, "status": "complete",
            **{key: profile[key] for key in (
                "scoring_provider", "scoring_model", "scoring_reasoning_effort",
                "scoring_prompt_version", "scoring_skill_version",
            )},
            "scoring_package": scoring_package,
            "reconstructed_from_private_audit": candidates[0].name,
        }
    for record in rebuilt.values():
        _append(path, record)
    return len(rebuilt)


def _profile(args: argparse.Namespace) -> dict[str, str]:
    return {
        "scoring_provider": args.scoring_provider,
        "scoring_model": args.scoring_model,
        "scoring_reasoning_effort": args.scoring_reasoning_effort,
        "feedback_provider": args.feedback_provider,
        "feedback_model": args.feedback_model,
        "feedback_reasoning_effort": args.feedback_reasoning_effort,
        "scoring_prompt_version": SCORING_PROMPT_VERSION,
        "scoring_skill_version": SCORING_SKILL_VERSION,
        "feedback_prompt_version": FEEDBACK_PROMPT_VERSION,
        "feedback_skill_version": FEEDBACK_SKILL_VERSION,
    }


def _matches_profile(record: dict[str, Any], profile: dict[str, str]) -> bool:
    return all(record.get(key) == value for key, value in profile.items())


def _require_previous_gate(split: str, output_dir: Path) -> None:
    previous = "validation" if split == "official" else "official" if split == "holdout" else None
    if previous is None:
        return
    path = output_dir / f"{previous}-metrics.json"
    if not path.exists():
        raise RuntimeError(f"{previous} metrics are missing; the next evaluation stage is locked.")
    metrics = json.loads(path.read_text(encoding="utf-8"))
    if not metrics.get("passed") or metrics.get("feedback_prompt_version") != FEEDBACK_PROMPT_VERSION:
        raise RuntimeError(f"{previous} did not pass the current feedback-version gate.")


def _gate(split: str, metrics: dict[str, Any]) -> bool:
    expected = EXPECTED_COUNTS[split]
    closure_min = {"validation": 7, "official": 13, "holdout": 11}[split]
    return (
        metrics["selected_cases"] == expected
        and metrics["complete_cases"] == expected
        and metrics["structure_valid_count"] == expected
        and metrics["evidence_valid_count"] == expected
        and metrics["primary_limitation_aligned_count"] == expected
        and metrics["feedback_training_closed_loop_count"] >= closure_min
        and metrics["action_success_complete_count"] >= closure_min
        and metrics["pseudo_scoring_rule_count"] == 0
    )


def _review_cards(cases: list[dict[str, Any]], predictions: dict[str, dict[str, Any]]) -> str:
    lines = ["# Optional feedback review cards", "", "These cards are optional; no user review is required.", ""]
    for case in cases:
        record = predictions.get(str(case["case_id"]))
        if not record:
            continue
        lines.extend([f"## {case['case_id']}", ""])
        for index, item in enumerate(record.get("priority_items") or [], 1):
            lines.extend([
                f"### Priority {index}: {item.get('criterion', '-')} / {item.get('action_type', '-')}",
                f"- Evidence: {item.get('evidence', '')}",
                f"- Action: {item.get('action', '')}",
                f"- Success check: {item.get('success_check', '')}", "",
            ])
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    args = parse_args()
    if args.split == "holdout" and not args.unlock_holdout:
        raise PermissionError("Holdout requires --unlock-holdout after the earlier gates pass.")
    if args.split == "holdout" and args.limit is not None:
        raise ValueError("The locked holdout cannot be previewed.")
    _require_previous_gate(args.split, args.output_dir)
    cases = load_cases(args.split, unlock_holdout=args.unlock_holdout)
    if args.limit is not None:
        cases = cases[:args.limit]
    profile = _profile(args)
    predictions_path = args.output_dir / f"{args.split}-predictions.jsonl"
    locks_path = args.output_dir / f"{args.split}-locked-scores.jsonl"
    consumed_path = args.output_dir / "holdout-consumed.json"
    if args.split == "holdout" and consumed_path.exists():
        raise RuntimeError("The locked feedback holdout has already been consumed.")
    if args.split == "official" and not locks_path.exists():
        import_official_score_locks(cases, locks_path, profile)
    previous = {
        case_id: record for case_id, record in _load_latest(predictions_path).items()
        if _matches_profile(record, profile)
    } if args.resume or args.metrics_only else {}
    locks = {
        case_id: record for case_id, record in _load_latest(locks_path).items()
        if all(record.get(key) == profile[key] for key in (
            "scoring_provider", "scoring_model", "scoring_reasoning_effort",
            "scoring_prompt_version", "scoring_skill_version",
        ))
    }
    pending = [case for case in cases if str(case["case_id"]) not in previous]
    if args.metrics_only and pending:
        raise RuntimeError("Metrics-only requires one current-version prediction per case.")
    if not args.execute and not args.metrics_only:
        print(json.dumps({
            "dry_run": True, "split": args.split, "selected": len(cases),
            "reusable_scores": sum(str(case["case_id"]) in locks for case in pending),
            "would_score": sum(str(case["case_id"]) not in locks for case in pending),
            "would_generate_feedback": len(pending), **profile,
        }, ensure_ascii=False, indent=2))
        return 0

    predictions = dict(previous)
    for case in pending:
        case_id = str(case["case_id"])
        audit_summary: list[dict[str, Any]] = []

        def audit_hook(event: dict[str, Any]) -> None:
            audit_summary.append({
                "stage": event.get("stage"), "attempt": event.get("attempt"),
                "latency_seconds": event.get("latency_seconds"),
                "validation_error": event.get("validation_error"),
                "response_model": event.get("response_model"),
                "system_fingerprint": event.get("system_fingerprint"),
            })

        try:
            lock_record = locks.get(case_id)
            if lock_record:
                scoring_package = lock_record["scoring_package"]
            else:
                scoring_package = grade_scoring_decision(
                    task_type="Task 2", topic=str(case["question"]),
                    essay=str(case["essay_clean"]), audit_hook=audit_hook,
                    provider=args.scoring_provider, model=args.scoring_model,
                    reasoning_effort=args.scoring_reasoning_effort,
                )
                lock_record = {
                    "case_id": case_id, "status": "complete", **{
                        key: profile[key] for key in (
                            "scoring_provider", "scoring_model", "scoring_reasoning_effort",
                            "scoring_prompt_version", "scoring_skill_version",
                        )
                    }, "scoring_package": scoring_package,
                }
                _append(locks_path, lock_record)
                locks[case_id] = lock_record
            package = grade_essay_package(
                task_type="Task 2", topic=str(case["question"]),
                essay=str(case["essay_clean"]), audit_hook=audit_hook,
                scoring_provider=args.scoring_provider, scoring_model=args.scoring_model,
                reasoning_effort=args.scoring_reasoning_effort,
                teaching_provider=args.feedback_provider, teaching_model=args.feedback_model,
                teaching_reasoning_effort=args.feedback_reasoning_effort,
                locked_scoring_package=scoring_package,
            )
            structured = dict(package["structured"])
            quality = feedback_quality_flags(structured, str(case["essay_clean"]), package["scoring"])
            record = {
                "case_id": case_id, "status": "complete", **profile,
                "priority_tags": prediction_priority_tags(structured),
                "priority_items": list(structured.get("priorities") or [])[:2],
                "quality": quality, "schema_valid": True,
                "feedback_contract": {
                    key: structured.get(key) for key in (
                        "priorities", "problems", "sentence_training", "logic_training"
                    )
                },
                "audit_summary": audit_summary, "usage": package.get("usage") or {},
                "stage_usage": package.get("stage_usage") or {},
            }
            predictions[case_id] = record
        except Exception as exc:
            record = {
                "case_id": case_id, "status": "error", **profile,
                "audit_summary": audit_summary, "error": f"{type(exc).__name__}: {exc}",
            }
        _append(predictions_path, record)

    selected_predictions = [
        predictions[str(case["case_id"])] for case in cases
        if str(case["case_id"]) in predictions
    ]
    metrics: dict[str, Any] = {
        "split": args.split, "selected_cases": len(cases),
        "complete_cases": len(selected_predictions),
        "error_cases": len(cases) - len(selected_predictions), **profile,
    }
    quality_rows = [record.get("quality") or {} for record in selected_predictions]
    for key in (
        "structure_valid", "evidence_valid", "primary_limitation_aligned",
        "feedback_training_closed_loop", "action_success_complete",
    ):
        metrics[f"{key}_count"] = sum(bool(row.get(key)) for row in quality_rows)
    metrics["pseudo_scoring_rule_count"] = sum(
        int(row.get("pseudo_scoring_rule_count") or 0) for row in quality_rows
    )
    if args.split != "official":
        gold = [case["structured_examiner_feedback"] for case in cases]
        metrics["examiner_claimed_tag_coverage"] = feedback_metrics(
            gold, selected_predictions
        )
    for stage, provider, model in (
        ("scoring", args.scoring_provider, args.scoring_model),
        ("teaching", args.feedback_provider, args.feedback_model),
    ):
        input_tokens = sum(
            int(((record.get("stage_usage") or {}).get(stage) or {}).get("input_tokens") or 0)
            for record in selected_predictions
        )
        output_tokens = sum(
            int(((record.get("stage_usage") or {}).get(stage) or {}).get("output_tokens") or 0)
            for record in selected_predictions
        )
        input_price, output_price = PRICE_PER_MILLION.get((provider, model), (0.0, 0.0))
        metrics[f"{stage}_input_tokens"] = input_tokens
        metrics[f"{stage}_output_tokens"] = output_tokens
        metrics[f"{stage}_estimated_usd"] = round(
            input_tokens * input_price / 1_000_000 + output_tokens * output_price / 1_000_000,
            6,
        )
    metrics["latency_seconds"] = round(sum(
        float(event.get("latency_seconds") or 0)
        for record in selected_predictions for event in record.get("audit_summary") or []
    ), 3)
    metrics["retry_count"] = sum(
        int(event.get("attempt") or 1) > 1
        for record in selected_predictions for event in record.get("audit_summary") or []
    )
    metrics["passed"] = _gate(args.split, metrics)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = args.output_dir / f"{args.split}-metrics.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / f"{args.split}-review-cards.md").write_text(
        _review_cards(cases, predictions), encoding="utf-8"
    )
    if args.split == "holdout" and args.execute and len(pending) == EXPECTED_COUNTS["holdout"]:
        consumed_path.write_text(json.dumps({
            "consumed": True, "passed": metrics["passed"], "metrics_file": str(metrics_path)
        }, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if metrics["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

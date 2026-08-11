#!/usr/bin/env python3
"""Pilot LLM annotations for at most 20 unlabelled stress cases; dry-run by default."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.kaggle_annotation import (  # noqa: E402
    annotate_case,
    cache_record,
    load_annotation_cache,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=ROOT_DIR / "data" / "processed" / "kaggle_ielts" / "model_annotation_candidates.jsonl",
    )
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT_DIR / ".private" / "kaggle_ielts" / "annotation_cache.jsonl",
    )
    parser.add_argument("--model", default="gpt-5.4-mini-2026-03-17")
    parser.add_argument("--limit", type=int, default=10, choices=range(1, 21))
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--resume", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true", help="Explicitly authorize paid API calls.")
    return parser.parse_args()


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive.")
    candidates = _read_jsonl(args.input)[:args.limit]
    previous = load_annotation_cache(args.cache) if args.resume else {}
    pending = [case for case in candidates if previous.get(case["case_id"], {}).get("status") != "complete"]
    if not args.execute:
        print(json.dumps({
            "dry_run": True,
            "model": args.model,
            "selected": len(candidates),
            "already_complete": len(candidates) - len(pending),
            "would_call": len(pending),
        }, ensure_ascii=False, indent=2))
        return 0
    buffer: list[dict] = []
    completed = 0
    failed = 0
    for case in pending:
        try:
            annotation, usage = annotate_case(case, model=args.model)
            buffer.append(cache_record(case["case_id"], model=args.model, status="complete", annotation=annotation, usage=usage))
            completed += 1
        except Exception as exc:  # keep the batch resumable after a single API/schema failure
            buffer.append(cache_record(case["case_id"], model=args.model, status="error", error=f"{type(exc).__name__}: {exc}"))
            failed += 1
        if len(buffer) >= args.batch_size:
            _append(args.cache, buffer)
            buffer.clear()
    if buffer:
        _append(args.cache, buffer)
    print(json.dumps({"completed": completed, "failed": failed, "cache": str(args.cache)}, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

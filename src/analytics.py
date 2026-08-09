"""Anonymous usage analytics stored in a small JSON file."""

import json
import os
import threading
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any


ANALYTICS_PATH = Path("data") / "analytics.json"
_ANALYTICS_LOCK = threading.Lock()


def _empty_analytics() -> dict[str, Any]:
    return {"version": 1, "events": []}


def _load_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_analytics()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_analytics()
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return _empty_analytics()
    return data


def record_grading_event(
    *,
    user_id: str,
    overall_band: float | None,
    essay_word_count: int,
    model_name: str,
    api_tokens: int | None = None,
    api_cost: float | None = None,
    path: Path = ANALYTICS_PATH,
) -> bool:
    """Append one successful grading event without storing essay content."""
    event = {
        "anonymous_user": hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16],
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "overall_band": overall_band,
        "essay_word_count": essay_word_count,
        "model_name": model_name,
        "api_tokens": api_tokens,
        "api_cost": api_cost,
    }

    try:
        with _ANALYTICS_LOCK:
            data = _load_unlocked(path)
            data["events"].append(event)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_suffix(".tmp")
            temporary_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_path, path)
    except OSError:
        return False
    return True


def load_analytics(path: Path = ANALYTICS_PATH) -> dict[str, Any]:
    """Return aggregate metrics and recent anonymous grading events."""
    with _ANALYTICS_LOCK:
        data = _load_unlocked(path)

    events = [event for event in data["events"] if isinstance(event, dict)]
    today = datetime.now().astimezone().date().isoformat()
    bands = [
        float(event["overall_band"])
        for event in events
        if isinstance(event.get("overall_band"), (int, float))
    ]
    word_counts = [
        int(event["essay_word_count"])
        for event in events
        if isinstance(event.get("essay_word_count"), (int, float))
    ]

    return {
        "total_users": len(
            {
                event.get("anonymous_user") or event.get("user_id")
                for event in events
                if event.get("anonymous_user") or event.get("user_id")
            }
        ),
        "total_essays": len(events),
        "essays_today": sum(
            1 for event in events if str(event.get("timestamp", ""))[:10] == today
        ),
        "total_api_calls": len(events),
        "average_band": sum(bands) / len(bands) if bands else None,
        "average_word_count": sum(word_counts) / len(word_counts) if word_counts else None,
        "recent_activity": list(reversed(events[-20:])),
    }

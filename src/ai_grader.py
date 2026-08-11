"""AI provider client and IELTS grading request."""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
import streamlit as st

from src.prompts import build_scoring_prompt, build_teaching_prompt, load_skill_scoring_rules
from src.chinese_report import examiner_result_to_markdown
from src.report_schema import (
    FEEDBACK_PROMPT_VERSION,
    FEEDBACK_SKILL_VERSION,
    OVERALL_CALIBRATION_OFFSET,
    OVERALL_CALIBRATION_VERSION,
    PROMPT_VERSION,
    REPORT_PROMPT_VERSION,
    SCHEMA_VERSION,
    SCORING_PROMPT_VERSION,
    SCORING_SKILL_VERSION,
    SCORING_DECISION_JSON_SCHEMA,
    SKILL_VERSION,
    TEACHING_FEEDBACK_JSON_SCHEMA,
    drop_unverified_optional_teaching_items,
    estimated_band_range,
    format_practice_band_interval,
    restore_score_evidence_roles,
    validate_examiner_result,
    validate_scoring_decision,
)


DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
PRODUCTION_MODEL = "gpt-5.4-mini"
PRODUCTION_MODEL_SNAPSHOT = "gpt-5.4-mini-2026-03-17"
PRODUCTION_SCORING_PROVIDER = "OpenAI"
PRODUCTION_SCORING_MODEL = PRODUCTION_MODEL_SNAPSHOT
PRODUCTION_TEACHING_PROVIDER = "OpenAI"
PRODUCTION_TEACHING_MODEL = PRODUCTION_MODEL_SNAPSHOT


@dataclass(frozen=True)
class GradingModelConfig:
    """One explicit, auditable model role in the grading pipeline."""

    provider: str
    model: str
    reasoning_effort: str = "none"

    def __post_init__(self) -> None:
        if self.provider not in {"OpenAI", "DeepSeek"}:
            raise ValueError("provider must be 'OpenAI' or 'DeepSeek'.")
        allowed_efforts = (
            {"none", "low", "high", "max"}
            if self.provider == "DeepSeek"
            else {"none", "low"}
        )
        if self.reasoning_effort not in allowed_efforts:
            allowed = ", ".join(sorted(allowed_efforts))
            raise ValueError(
                f"reasoning_effort for {self.provider} must be one of: {allowed}."
            )


def get_runtime_setting(name: str, default: str | None = None) -> str | None:
    """Read Streamlit Cloud secrets first, then local environment variables."""
    try:
        value = st.secrets.get(name)
    except (FileNotFoundError, KeyError):
        value = None

    if value not in (None, ""):
        return str(value)
    return os.getenv(name, default)


class AIGraderError(Exception):
    """Detailed error raised when an AI provider request fails."""

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str,
        api_key_loaded: bool,
        original_error: Exception,
        status_code: int | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key_loaded = api_key_loaded
        self.original_error = original_error
        self.status_code = status_code
        super().__init__(self._build_message())

    def _format_error_chain(self, error: BaseException) -> str:
        lines = [f"{type(error).__name__}: {error}"]
        cause = error.__cause__ or error.__context__
        if cause:
            lines.append("\nCaused by:")
            lines.append(self._format_error_chain(cause))
        return "\n".join(lines)

    def _build_message(self) -> str:
        error_type = type(self.original_error).__name__
        status = self.status_code if self.status_code is not None else "N/A"
        return (
            f"Provider: {self.provider}\n"
            f"Model: {self.model}\n"
            f"Base URL: {self.base_url}\n"
            f"API Key Loaded: {self.api_key_loaded}\n"
            f"Exception Type: {error_type}\n"
            f"HTTP Status Code: {status}\n\n"
            f"Full Exception Chain:\n{self._format_error_chain(self.original_error)}"
        )


def get_provider_config(provider: str) -> tuple[str, str | None, str]:
    """Return secret name, API key, and base URL for a provider."""
    if provider == "DeepSeek":
        return (
            "DEEPSEEK_API_KEY",
            get_runtime_setting("DEEPSEEK_API_KEY"),
            get_runtime_setting("DEEPSEEK_BASE_URL", DEEPSEEK_DEFAULT_BASE_URL)
            or DEEPSEEK_DEFAULT_BASE_URL,
        )

    return (
        "OPENAI_API_KEY",
        get_runtime_setting("OPENAI_API_KEY"),
        "https://api.openai.com/v1",
    )


def build_client(provider: str) -> OpenAI:
    """Create an API client for DeepSeek or OpenAI."""
    key_name, api_key, base_url = get_provider_config(provider)
    if provider == "DeepSeek":
        if not api_key:
            raise ValueError(
                f"{key_name} is missing. Please set it before running the app."
            )

        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=75.0,
            max_retries=2,
        )

    if not api_key:
        raise ValueError(
            f"{key_name} is missing. Please set it before running the app."
        )

    return OpenAI(api_key=api_key, timeout=75.0, max_retries=2)


def completion_options(
    provider: str,
    model: str,
    max_output_tokens: int,
) -> dict[str, int | float | str]:
    """Return output controls supported by the selected provider and model."""
    if provider == "OpenAI" and model.lower().startswith("gpt-5"):
        return {
            "max_completion_tokens": max_output_tokens,
            "reasoning_effort": "low",
        }
    return {"temperature": 0.0, "max_tokens": max_output_tokens}


def _response_usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    return {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _sum_usage(events: list[dict[str, Any]]) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        values = [(event.get("usage") or {}).get(name) for event in events]
        result[name] = (
            sum(int(value) for value in values if value is not None)
            if any(value is not None for value in values)
            else None
        )
    return result


def _provider_request(
    *,
    config: GradingModelConfig,
    messages: list[dict[str, str]],
    response_schema: dict[str, Any],
    max_completion_tokens: int,
) -> dict[str, Any]:
    if config.provider == "OpenAI":
        return {
            "model": config.model,
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": response_schema},
            "max_completion_tokens": max_completion_tokens,
            "reasoning_effort": config.reasoning_effort,
        }

    deepseek_messages = list(messages)
    schema_instruction = (
        "Return one valid json object only. Follow this exact JSON shape; do not add "
        "keys or Markdown:\n" + json.dumps(response_schema["schema"], ensure_ascii=False)
    )
    deepseek_messages[-1] = {
        "role": deepseek_messages[-1]["role"],
        "content": deepseek_messages[-1]["content"] + "\n\n" + schema_instruction,
    }
    request: dict[str, Any] = {
        "model": config.model,
        "messages": deepseek_messages,
        "response_format": {"type": "json_object"},
        "max_tokens": max_completion_tokens,
    }
    if config.reasoning_effort == "none":
        request["extra_body"] = {"thinking": {"type": "disabled"}}
    else:
        # DeepSeek V4 maps low/medium to high. Send the official effective
        # value explicitly so calibration metadata matches provider behavior.
        request["reasoning_effort"] = (
            "max" if config.reasoning_effort == "max" else "high"
        )
        request["extra_body"] = {"thinking": {"type": "enabled"}}
        request["timeout"] = 180.0
    return request


def _validated_completion(
    *,
    client: OpenAI,
    config: GradingModelConfig,
    stage: str,
    messages: list[dict[str, str]],
    response_schema: dict[str, Any],
    max_completion_tokens: int,
    validator: Callable[[dict[str, Any], int], dict[str, Any]],
    audit_hook: Callable[[dict[str, Any]], None] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return one validated decision; a semantic repair is the only retry."""
    events: list[dict[str, Any]] = []
    attempt_messages = list(messages)
    last_error: Exception | None = None
    for attempt in (1, 2):
        request = _provider_request(
            config=config,
            messages=attempt_messages,
            response_schema=response_schema,
            max_completion_tokens=max_completion_tokens,
        )
        started = time.perf_counter()
        response = client.chat.completions.create(**request)
        raw = response.choices[0].message.content or ""
        event = {
            "stage": stage,
            "attempt": attempt,
            "provider": config.provider,
            "model": config.model,
            "response_model": getattr(response, "model", None),
            "system_fingerprint": getattr(response, "system_fingerprint", None),
            "reasoning_effort": config.reasoning_effort,
            "messages": request["messages"],
            "raw_response": raw,
            "latency_seconds": round(time.perf_counter() - started, 3),
            "usage": _response_usage(response),
        }
        events.append(event)
        try:
            result = validator(json.loads(raw), attempt)
        except (json.JSONDecodeError, ValueError, RuntimeError) as exc:
            last_error = exc
            event["validation_error"] = f"{type(exc).__name__}: {exc}"
            if audit_hook is not None:
                audit_hook(event)
            if attempt == 2:
                raise
            attempt_messages = [
                *messages,
                {"role": "assistant", "content": raw or "{}"},
                {
                    "role": "user",
                    "content": (
                        "Repair the previous JSON response. Preserve any valid decisions, "
                        "but correct every validation failure listed below. Never reuse a "
                        "quotation explicitly identified as invalid; copy replacement text "
                        "directly from one contiguous span of the submitted essay. "
                        f"Validation failures: {type(exc).__name__}: {exc}. Return JSON only."
                    ),
                },
            ]
            continue
        if audit_hook is not None:
            audit_hook(event)
        return result, events
    raise RuntimeError("Structured response validation failed.") from last_error


def _grader_error(config: GradingModelConfig, exc: Exception) -> AIGraderError:
    _, api_key, base_url = get_provider_config(config.provider)
    return AIGraderError(
        provider=config.provider,
        model=config.model,
        base_url=base_url,
        api_key_loaded=bool(api_key),
        original_error=exc,
        status_code=exc.status_code if isinstance(exc, APIStatusError) else None,
    )


def grade_scoring_decision(
    *,
    task_type: str,
    topic: str,
    essay: str,
    audit_hook: Callable[[dict[str, Any]], None] | None = None,
    provider: str = PRODUCTION_SCORING_PROVIDER,
    model: str = PRODUCTION_SCORING_MODEL,
    reasoning_effort: str = "none",
) -> dict[str, object]:
    """Run only the blind score-locking stage for calibration or production."""
    if task_type != "Task 2":
        raise ValueError("EssayPilot V2 currently supports IELTS Writing Task 2 only.")
    if not load_skill_scoring_rules().strip():
        raise RuntimeError(
            "The installed IELTS scoring Skill could not be loaded. Grading was stopped."
        )
    config = GradingModelConfig(provider, model, reasoning_effort)
    client = build_client(config.provider)
    messages = [
        {
            "role": "system",
            "content": (
                "You are EssayPilot's IELTS Writing Task 2 scoring component. "
                "Use only the supplied descriptor material and essay evidence."
            ),
        },
        {"role": "user", "content": build_scoring_prompt(task_type, topic, essay)},
    ]
    try:
        scoring, events = _validated_completion(
            client=client,
            config=config,
            stage="scoring",
            messages=messages,
            response_schema=SCORING_DECISION_JSON_SCHEMA,
            max_completion_tokens=5000,
            validator=lambda payload, _attempt: validate_scoring_decision(payload, essay),
            audit_hook=audit_hook,
        )
    except (APIConnectionError, APIStatusError, OpenAIError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        raise _grader_error(config, exc) from exc

    last_event = events[-1]
    return {
        "provider": config.provider,
        "model": config.model,
        "response_model": last_event.get("response_model"),
        "system_fingerprint": last_event.get("system_fingerprint"),
        "reasoning_effort": config.reasoning_effort,
        "schema_version": SCHEMA_VERSION,
        "overall_calibration_version": OVERALL_CALIBRATION_VERSION,
        "overall_calibration_offset": OVERALL_CALIBRATION_OFFSET,
        "prompt_version": PROMPT_VERSION,
        "skill_version": SKILL_VERSION,
        "graded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structured": scoring,
        "scoring": scoring,
        "usage": _sum_usage(events),
    }


def grade_essay_package(
    *,
    task_type: str,
    topic: str,
    essay: str,
    audit_hook: Callable[[dict[str, Any]], None] | None = None,
    reasoning_effort: str = "none",
    scoring_provider: str = PRODUCTION_SCORING_PROVIDER,
    scoring_model: str = PRODUCTION_SCORING_MODEL,
    teaching_provider: str = PRODUCTION_TEACHING_PROVIDER,
    teaching_model: str = PRODUCTION_TEACHING_MODEL,
    teaching_reasoning_effort: str = "none",
    locked_scoring_package: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return the locked score plus teaching feedback without score mutation."""
    scoring_reused = locked_scoring_package is not None
    if locked_scoring_package is None:
        scoring_package = grade_scoring_decision(
            task_type=task_type,
            topic=topic,
            essay=essay,
            audit_hook=audit_hook,
            provider=scoring_provider,
            model=scoring_model,
            reasoning_effort=reasoning_effort,
        )
    else:
        scoring_package = dict(locked_scoring_package)
        if scoring_package.get("prompt_version") != SCORING_PROMPT_VERSION:
            raise ValueError("The cached scoring decision uses a different scoring prompt version.")
        if scoring_package.get("skill_version") != SCORING_SKILL_VERSION:
            raise ValueError("The cached scoring decision uses a different scoring Skill version.")
        cached_scoring = scoring_package.get("scoring")
        if not isinstance(cached_scoring, dict) or "criteria" not in cached_scoring or "uncertainty" not in cached_scoring:
            raise ValueError("The cached scoring decision is incomplete.")
        scoring_package.setdefault("usage", {})
    scoring = restore_score_evidence_roles(dict(scoring_package["scoring"]))
    teaching_config = GradingModelConfig(
        teaching_provider, teaching_model, teaching_reasoning_effort
    )
    teaching_client = build_client(teaching_config.provider)
    teaching_messages = [
        {
            "role": "system",
            "content": (
                "You are EssayPilot's IELTS writing coach. The supplied scoring "
                "decision is validated and locked. Generate teaching material only."
            ),
        },
        {
            "role": "user",
            "content": build_teaching_prompt(task_type, topic, essay, scoring),
        },
    ]
    sanitized_teaching_fields: list[str] = []

    def validate_teaching(teaching: dict[str, Any], attempt: int) -> dict[str, Any]:
        if "criteria" in teaching or "overall_band" in teaching:
            raise ValueError("The teaching stage attempted to modify locked scores.")
        if attempt == 2:
            teaching, removed = drop_unverified_optional_teaching_items(teaching, essay)
            sanitized_teaching_fields.extend(removed)
        merged = dict(teaching)
        merged["criteria"] = scoring["criteria"]
        return validate_examiner_result(merged, essay)

    try:
        structured, teaching_events = _validated_completion(
            client=teaching_client,
            config=teaching_config,
            stage="teaching",
            messages=teaching_messages,
            response_schema=TEACHING_FEEDBACK_JSON_SCHEMA,
            max_completion_tokens=14000,
            validator=validate_teaching,
            audit_hook=audit_hook,
        )
    except (APIConnectionError, APIStatusError, OpenAIError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        raise _grader_error(teaching_config, exc) from exc

    scoring_usage = dict(scoring_package.get("usage") or {})
    teaching_usage = _sum_usage(teaching_events)
    usage = {
        name: (
            (scoring_usage.get(name) or 0) + (teaching_usage.get(name) or 0)
            if scoring_usage.get(name) is not None or teaching_usage.get(name) is not None
            else None
        )
        for name in ("input_tokens", "output_tokens", "total_tokens")
    }
    band_range = estimated_band_range(scoring)
    structured["locked_scoring_decision"] = scoring
    structured["scoring_prompt_version"] = SCORING_PROMPT_VERSION
    structured["feedback_prompt_version"] = FEEDBACK_PROMPT_VERSION
    structured["feedback_skill_version"] = FEEDBACK_SKILL_VERSION
    return {
        "provider": scoring_package["provider"],
        "model": scoring_package["model"],
        "response_model": scoring_package.get("response_model"),
        "system_fingerprint": scoring_package.get("system_fingerprint"),
        "model_family": PRODUCTION_MODEL,
        "reasoning_effort": reasoning_effort,
        "teaching_provider": teaching_config.provider,
        "teaching_model": teaching_config.model,
        "teaching_reasoning_effort": teaching_config.reasoning_effort,
        "sanitized_teaching_fields": sorted(set(sanitized_teaching_fields)),
        "scoring_reused": scoring_reused,
        "schema_version": SCHEMA_VERSION,
        "overall_calibration_version": OVERALL_CALIBRATION_VERSION,
        "overall_calibration_offset": OVERALL_CALIBRATION_OFFSET,
        "prompt_version": REPORT_PROMPT_VERSION,
        "scoring_prompt_version": SCORING_PROMPT_VERSION,
        "feedback_prompt_version": FEEDBACK_PROMPT_VERSION,
        "skill_version": SCORING_SKILL_VERSION,
        "feedback_skill_version": FEEDBACK_SKILL_VERSION,
        "graded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structured": structured,
        "scoring": scoring,
        "estimated_band_range": list(band_range),
        "report": examiner_result_to_markdown(structured, estimated_range=band_range),
        "usage": usage,
        "stage_usage": {"scoring": scoring_usage, "teaching": teaching_usage},
    }


def grade_essay(
    provider: str = "OpenAI",
    task_type: str = "Task 2",
    topic: str = "",
    essay: str = "",
    model: str = PRODUCTION_MODEL,
) -> str:
    """Compatibility wrapper returning deterministic Markdown from structured data."""
    del provider, model
    return str(grade_essay_package(task_type=task_type, topic=topic, essay=essay)["report"])


EXPRESSION_PRACTICE_PROMPT_VERSION = "expression-sentence-zh-v1-2026-08-09"
EXPRESSION_PRACTICE_SCHEMA = {
    "name": "essaypilot_expression_practice",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["appropriate", "feedback_zh", "improved_sentence_en", "mastered"],
        "properties": {
            "appropriate": {"type": "boolean"},
            "feedback_zh": {"type": "string"},
            "improved_sentence_en": {"type": "string"},
            "mastered": {"type": "boolean"},
        },
    },
}


def review_expression_sentence(
    *, expression: str, meaning: str, usage_note: str, student_sentence: str
) -> dict[str, object]:
    """Review one active-use sentence with the fixed production model."""
    _, api_key, base_url = get_provider_config("OpenAI")
    client = build_client("OpenAI")
    prompt = f"""你是一名面向中国雅思考生的英文造句教练。

目标表达：{expression}
中文释义：{meaning}
使用提醒：{usage_note}
学生造句：{student_sentence}

只返回符合 JSON schema 的结果。feedback_zh 用简短中文说明表达含义、搭配、语法和语境是否自然；
improved_sentence_en 给出保留学生原意的自然英文优化句。只有表达含义使用准确、关键搭配正确、
语法基本正确且语境自然时，appropriate 和 mastered 才都为 true。不要因为句子复杂或使用生僻词而判定掌握。"""
    try:
        response = client.chat.completions.create(
            model=PRODUCTION_MODEL_SNAPSHOT,
            messages=[
                {"role": "system", "content": "You are EssayPilot's concise IELTS expression coach."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_schema", "json_schema": EXPRESSION_PRACTICE_SCHEMA},
            max_completion_tokens=1200,
            reasoning_effort="none",
        )
        result = json.loads(response.choices[0].message.content or "")
        if not isinstance(result, dict):
            raise ValueError("Expression review is not a JSON object.")
        return result
    except APIStatusError as exc:
        raise AIGraderError(
            provider="OpenAI", model=PRODUCTION_MODEL, base_url=base_url,
            api_key_loaded=bool(api_key), original_error=exc, status_code=exc.status_code,
        ) from exc
    except (APIConnectionError, OpenAIError, json.JSONDecodeError, ValueError) as exc:
        raise AIGraderError(
            provider="OpenAI", model=PRODUCTION_MODEL, base_url=base_url,
            api_key_loaded=bool(api_key), original_error=exc,
        ) from exc


def review_sentence_rewrite(
    provider: str,
    original_sentence: str,
    student_rewrite: str,
    model: str,
) -> str:
    """Review a student's rewritten sentence and return coaching feedback."""
    _, api_key, base_url = get_provider_config(provider)
    client = build_client(provider)

    visible_draft_1_scores = dict(draft_1_scores)
    visible_draft_2_scores = dict(draft_2_scores)
    visible_draft_1_scores["Practice Band Interval"] = format_practice_band_interval(
        visible_draft_1_scores.pop("Overall Band", None)
    )
    visible_draft_2_scores["Practice Band Interval"] = format_practice_band_interval(
        visible_draft_2_scores.pop("Overall Band", None)
    )
    prompt = f"""
You are an IELTS Writing sentence coach for a Chinese high school student.
Review the student's rewritten sentence against the original sentence.

Original sentence:
{original_sentence}

Student rewrite:
{student_rewrite}

Give concise feedback in Chinese. Use this exact Markdown structure:

### AI点评

**大概水平：** Band X.X-X.X

**做得好的地方：**
- ...

**还需要改的地方：**
- ...

**更自然的6.5-7分版本：**
"..."

**记住这个表达：**
- ...

Rules:
- Be encouraging but specific.
- Focus on grammar, vocabulary, naturalness, and IELTS suitability.
- Do not make the sentence overly advanced.
- Keep the improved version close to the student's meaning.
""".strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert IELTS Writing sentence coach.",
                },
                {"role": "user", "content": prompt},
            ],
            **completion_options(provider, model, 900),
        )
    except APIStatusError as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
            status_code=exc.status_code,
        ) from exc
    except (APIConnectionError, OpenAIError) as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
        ) from exc
    except Exception as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
        ) from exc

    return response.choices[0].message.content or ""
def review_logic_rewrite(
    provider: str,
    problem: str,
    original_fragment: str,
    student_rewrite: str,
    model: str,
) -> str:
    """Review a student's logic-level rewrite and return coaching feedback."""
    _, api_key, base_url = get_provider_config(provider)
    client = build_client(provider)

    prompt = f"""
You are an IELTS Writing logic and structure coach for a Chinese high school student.
Review the student's rewritten paragraph or key fragment.

Core problem:
{problem}

Original fragment:
{original_fragment}

Student rewrite:
{student_rewrite}

Give concise feedback in Chinese. Use this exact Markdown structure:

### 对比反馈

**大概水平：** Band X.X-X.X

**是否改善逻辑结构：**
- ...

**是否更清晰：**
- ...

**是否更接近Band 6.5+：**
- ...

**下一步修改建议：**
- ...

Rules:
- Be specific and evidence-based.
- Focus on argument clarity, explanation, example support, and paragraph development.
- Keep the advice practical for Band 6.5-7.0 improvement.
""".strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert IELTS Writing logic coach.",
                },
                {"role": "user", "content": prompt},
            ],
            **completion_options(provider, model, 1000),
        )
    except APIStatusError as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
            status_code=exc.status_code,
        ) from exc
    except (APIConnectionError, OpenAIError) as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
        ) from exc
    except Exception as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
        ) from exc

    return response.choices[0].message.content or ""

def compare_draft_progress(
    provider: str,
    task_question: str,
    draft_1_text: str,
    draft_1_scores: dict[str, float | None],
    draft_2_text: str,
    draft_2_scores: dict[str, float | None],
    model: str,
) -> str:
    """Compare two scored drafts and return concise Chinese coaching."""
    _, api_key, base_url = get_provider_config(provider)
    client = build_client(provider)
    prompt = f"""
You are an IELTS revision coach comparing two versions of the same essay.
Do not rescore either essay. Treat the supplied scores as final.
Compare only evidence visible in the two drafts and give concise Chinese feedback.

Essay question:
{task_question}

Draft 1 scores:
{visible_draft_1_scores}

Draft 1:
{draft_1_text}

Draft 2 scores:
{visible_draft_2_scores}

Draft 2:
{draft_2_text}

Return only Markdown with exactly these sections:

### \u5df2\u7ecf\u6539\u5584\u7684\u95ee\u9898
- Identify concrete improvements in argument, structure, vocabulary, or grammar.
- Name the criterion with the largest score improvement.

### \u4ecd\u7136\u5b58\u5728\u7684\u95ee\u9898
- Identify recurring weaknesses and criteria without clear improvement.
- Explain briefly why the next band has not yet been reached.

### \u4e0b\u4e00\u6b21\u8bad\u7ec3\u91cd\u70b9
- Give only one or two specific priorities.

Rules:
- Quote short evidence from both drafts when useful.
- Never invent changes that are not visible.
- Do not repeat the score table.
- Never state or infer a point Overall score; refer only to the supplied practice interval and four criterion scores.
- Keep the response practical and concise.
""".strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise IELTS revision comparison coach.",
                },
                {"role": "user", "content": prompt},
            ],
            **completion_options(provider, model, 1400),
        )
    except APIStatusError as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
            status_code=exc.status_code,
        ) from exc
    except (APIConnectionError, OpenAIError) as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
        ) from exc
    except Exception as exc:
        raise AIGraderError(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
        ) from exc

    return response.choices[0].message.content or ""

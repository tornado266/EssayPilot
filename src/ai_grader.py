"""AI provider client and IELTS grading request."""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
import streamlit as st

from src.prompts import build_scoring_prompt, build_teaching_prompt, load_skill_scoring_rules
from src.chinese_report import examiner_result_to_markdown
from src.report_schema import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SCORING_DECISION_JSON_SCHEMA,
    SKILL_VERSION,
    TEACHING_FEEDBACK_JSON_SCHEMA,
    drop_unverified_optional_teaching_items,
    estimated_band_range,
    validate_examiner_result,
    validate_scoring_decision,
)


DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
PRODUCTION_MODEL = "gpt-5.4-mini"
PRODUCTION_MODEL_SNAPSHOT = "gpt-5.4-mini-2026-03-17"


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


def grade_essay_package(
    *,
    task_type: str,
    topic: str,
    essay: str,
    audit_hook: Callable[[dict[str, Any]], None] | None = None,
    reasoning_effort: str = "none",
) -> dict[str, object]:
    """Return a validated, versioned Task 2 examiner package from the fixed model."""
    if task_type != "Task 2":
        raise ValueError("EssayPilot V2 currently supports IELTS Writing Task 2 only.")
    if reasoning_effort not in {"none", "low"}:
        raise ValueError("reasoning_effort must be 'none' or 'low'.")
    if not load_skill_scoring_rules().strip():
        raise RuntimeError(
            "The installed IELTS scoring Skill could not be loaded. Grading was stopped."
        )

    _, api_key, base_url = get_provider_config("OpenAI")
    client = build_client("OpenAI")
    scoring_prompt = build_scoring_prompt(task_type, topic, essay)
    observed_responses: list[Any] = []

    def response_usage(response: Any) -> dict[str, int | None]:
        usage = getattr(response, "usage", None)
        return {
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }

    def validated_completion(
        *,
        stage: str,
        messages: list[dict[str, str]],
        response_schema: dict[str, Any],
        max_completion_tokens: int,
        validator: Callable[[dict[str, Any], int], dict[str, Any]],
    ) -> dict[str, Any]:
        """Make one model decision, retrying once only for invalid structured output."""
        last_error: Exception | None = None
        for attempt in (1, 2):
            started = time.perf_counter()
            response = client.chat.completions.create(
                model=PRODUCTION_MODEL_SNAPSHOT,
                messages=messages,
                response_format={"type": "json_schema", "json_schema": response_schema},
                max_completion_tokens=max_completion_tokens,
                reasoning_effort=reasoning_effort,
            )
            observed_responses.append(response)
            raw = response.choices[0].message.content or ""
            event = {
                "stage": stage,
                "attempt": attempt,
                "model": PRODUCTION_MODEL_SNAPSHOT,
                "reasoning_effort": reasoning_effort,
                "messages": messages,
                "raw_response": raw,
                "latency_seconds": round(time.perf_counter() - started, 3),
                "usage": response_usage(response),
            }
            try:
                result = validator(json.loads(raw), attempt)
            except (json.JSONDecodeError, ValueError, RuntimeError) as exc:
                last_error = exc
                event["validation_error"] = f"{type(exc).__name__}: {exc}"
                if audit_hook is not None:
                    audit_hook(event)
                if attempt == 2:
                    raise
                continue
            if audit_hook is not None:
                audit_hook(event)
            return result
        raise RuntimeError("Structured response validation failed.") from last_error

    try:
        scoring_messages = [
            {
                "role": "system",
                "content": (
                    "You are EssayPilot's IELTS Writing Task 2 scoring component. "
                    "Use only the supplied official-descriptor reference and return "
                    "four independent, evidence-based criterion decisions."
                ),
            },
            {"role": "user", "content": scoring_prompt},
        ]
        scoring = validated_completion(
            stage="scoring",
            messages=scoring_messages,
            response_schema=SCORING_DECISION_JSON_SCHEMA,
            max_completion_tokens=5000,
            validator=lambda payload, _attempt: validate_scoring_decision(payload, essay),
        )

        teaching_prompt = build_teaching_prompt(task_type, topic, essay, scoring)
        teaching_messages = [
            {
                "role": "system",
                "content": (
                    "You are EssayPilot's IELTS writing coach. The supplied scoring "
                    "decision is validated and locked. Generate teaching material only."
                ),
            },
            {"role": "user", "content": teaching_prompt},
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

        structured = validated_completion(
            stage="teaching",
            messages=teaching_messages,
            response_schema=TEACHING_FEEDBACK_JSON_SCHEMA,
            max_completion_tokens=14000,
            validator=validate_teaching,
        )
    except APIStatusError as exc:
        raise AIGraderError(
            provider="OpenAI",
            model=PRODUCTION_MODEL,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
            status_code=exc.status_code,
        ) from exc
    except (APIConnectionError, OpenAIError) as exc:
        raise AIGraderError(
            provider="OpenAI",
            model=PRODUCTION_MODEL,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
        ) from exc
    except (json.JSONDecodeError, ValueError, RuntimeError) as exc:
        raise AIGraderError(
            provider="OpenAI",
            model=PRODUCTION_MODEL,
            base_url=base_url,
            api_key_loaded=bool(api_key),
            original_error=exc,
        ) from exc

    def combined_usage(name: str) -> int | None:
        values = [getattr(getattr(response, "usage", None), name, None) for response in observed_responses]
        return sum(int(value) for value in values if value is not None) if any(value is not None for value in values) else None

    band_range = estimated_band_range(scoring)
    return {
        "model": PRODUCTION_MODEL_SNAPSHOT,
        "model_family": PRODUCTION_MODEL,
        "reasoning_effort": reasoning_effort,
        "sanitized_teaching_fields": sorted(set(sanitized_teaching_fields)),
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "skill_version": SKILL_VERSION,
        "graded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structured": structured,
        "scoring": scoring,
        "estimated_band_range": list(band_range),
        "report": examiner_result_to_markdown(structured, estimated_range=band_range),
        "usage": {
            "input_tokens": combined_usage("prompt_tokens"),
            "output_tokens": combined_usage("completion_tokens"),
            "total_tokens": combined_usage("total_tokens"),
        },
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
{draft_1_scores}

Draft 1:
{draft_1_text}

Draft 2 scores:
{draft_2_scores}

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

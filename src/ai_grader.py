"""AI provider client and IELTS grading request."""

import json
import os
from datetime import datetime, timezone

from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
import streamlit as st

from src.prompts import build_grading_prompt, build_structured_grading_prompt, load_skill_scoring_rules
from src.report_schema import (
    EXAMINER_JSON_SCHEMA,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SKILL_VERSION,
    examiner_result_to_markdown,
    validate_examiner_result,
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


def _grade_essay_legacy(provider: str, task_type: str, topic: str, essay: str, model: str) -> str:
    """Send the IELTS essay to an AI provider and return a markdown correction report."""
    _, api_key, base_url = get_provider_config(provider)
    client = build_client(provider)

    prompt = build_grading_prompt(
        task_type=task_type,
        topic=topic,
        essay=essay,
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert IELTS Writing examiner.",
                },
                {"role": "user", "content": prompt},
            ],
            **completion_options(provider, model, 8000),
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

    report = response.choices[0].message.content or ""

    if "单句提分训练" not in report:
        fallback_prompt = f"""
The previous IELTS report missed its final training section.
Create only the missing final section below, based strictly on the student's essay.

Requirements:
- Choose several of the weakest sentences from the student's essay.
- Ask the student to rewrite these sentences.
- Do not provide reference rewrites.
- Return only this section in Markdown.
- Use exactly this structure:

## 11. 单句提分训练

【练习任务】
请改写下面这几句话，使其更符合雅思6.5-7分水平：

1. "（原句）"
2. "（原句）"
3. "（原句）"

IELTS task type:
{task_type}

Essay question:
{topic}

Student essay:
{essay}
""".strip()

        try:
            fallback_response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert IELTS Writing examiner.",
                    },
                    {"role": "user", "content": fallback_prompt},
                ],
                **completion_options(provider, model, 1600),
            )
            extra_section = fallback_response.choices[0].message.content or ""
            if extra_section.strip():
                report = f"{report.rstrip()}\n\n{extra_section.strip()}"
        except Exception:
            report = (
                f"{report.rstrip()}\n\n"
                "## 11. 单句提分训练\n\n"
                "【练习任务】\n"
                "请从你的作文中选择几句表达最简单或最不准确的句子，"
                "改写成更符合雅思6.5-7分水平的句子。\n\n"
                "本部分原句提取失败，请重新点击批改生成练习句子。"
            )

    if "写作提升验证" not in report:
        logic_prompt = f"""
The previous IELTS report missed the writing improvement validation section.
Create only the missing section below, based strictly on the student's essay.

Requirements:
- Choose 2-3 core logic or structure problems.
- Choose one original paragraph or key fragment for each task.
- Give one rewrite task for each fragment.
- Do not review the student's rewrite here.
- Return only this section in Markdown.
- Use exactly this structure:

## 12. 写作提升验证

【提升练习】
请根据刚才的问题，重写你文章中的一个关键部分：

### 任务 1
问题：论点不清 / 段落没有发展 / 例子不支持观点

任务：
改写/重写下面内容，使其逻辑更清晰、更符合雅思6.5水平：

"（原文片段）"

要求：
- 2-4句话
- 要有清晰论点 + 解释 + 例子

IELTS task type:
{task_type}

Essay question:
{topic}

Student essay:
{essay}
""".strip()

        try:
            logic_response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert IELTS Writing examiner.",
                    },
                    {"role": "user", "content": logic_prompt},
                ],
                **completion_options(provider, model, 1800),
            )
            logic_section = logic_response.choices[0].message.content or ""
            if logic_section.strip():
                report = f"{report.rstrip()}\n\n{logic_section.strip()}"
        except Exception:
            report = (
                f"{report.rstrip()}\n\n"
                "## 12. 写作提升验证\n\n"
                "【提升练习】\n"
                "请根据刚才的问题，重写你文章中的一个关键部分。\n\n"
                "本部分生成失败，请重新点击批改生成提升练习。"
            )

    return report


def grade_essay_package(
    *,
    task_type: str,
    topic: str,
    essay: str,
) -> dict[str, object]:
    """Return a validated, versioned Task 2 examiner package from the fixed model."""
    if task_type != "Task 2":
        raise ValueError("EssayPilot V2 currently supports IELTS Writing Task 2 only.")
    if not load_skill_scoring_rules().strip():
        raise RuntimeError(
            "The installed IELTS scoring Skill could not be loaded. Grading was stopped."
        )

    _, api_key, base_url = get_provider_config("OpenAI")
    client = build_client("OpenAI")
    prompt = build_structured_grading_prompt(task_type, topic, essay)
    try:
        response = client.chat.completions.create(
            model=PRODUCTION_MODEL_SNAPSHOT,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are EssayPilot's strict IELTS Writing Task 2 examiner. "
                        "Follow the supplied scoring Skill and JSON schema exactly."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_schema", "json_schema": EXAMINER_JSON_SCHEMA},
            max_completion_tokens=16000,
            reasoning_effort="none",
        )
        raw = response.choices[0].message.content or ""
        structured = validate_examiner_result(json.loads(raw), essay)
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

    usage = getattr(response, "usage", None)
    return {
        "model": PRODUCTION_MODEL_SNAPSHOT,
        "model_family": PRODUCTION_MODEL,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "skill_version": SKILL_VERSION,
        "graded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structured": structured,
        "report": examiner_result_to_markdown(structured),
        "usage": {
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
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


def _compare_draft_progress_legacy(
    provider: str,
    task_question: str,
    draft_1_text: str,
    draft_1_scores: dict[str, float | None],
    draft_2_text: str,
    draft_2_scores: dict[str, float | None],
    model: str,
) -> str:
    """Compare two drafts after both have been scored by the main examiner."""
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

### 已经改善的问题
- Identify concrete improvements in argument, structure, vocabulary, or grammar.
- Name the criterion with the largest score improvement.

### 仍然存在的问题
- Identify recurring weaknesses and criteria without clear improvement.
- Explain briefly why the next band has not yet been reached.

### 下一次训练重点
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

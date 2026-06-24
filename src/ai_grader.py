"""AI provider client and IELTS grading request."""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError

from src.prompts import build_grading_prompt


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"


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
    """Return environment variable name, API key, and base URL for a provider."""
    if provider == "DeepSeek":
        base_url = os.getenv("DEEPSEEK_BASE_URL", DEEPSEEK_DEFAULT_BASE_URL).strip()
        base_url = base_url.rstrip("/") or DEEPSEEK_DEFAULT_BASE_URL
        return (
            "DEEPSEEK_API_KEY",
            os.getenv("DEEPSEEK_API_KEY"),
            base_url,
        )

    return ("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"), "https://api.openai.com/v1")


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
            max_retries=0,
            timeout=60.0,
        )

    if not api_key:
        raise ValueError(
            f"{key_name} is missing. Please set it before running the app."
        )

    return OpenAI(api_key=api_key, max_retries=0, timeout=60.0)


def grade_essay(provider: str, task_type: str, topic: str, essay: str, model: str) -> str:
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
            temperature=0.2,
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

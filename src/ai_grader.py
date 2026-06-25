"""AI provider client and IELTS grading request."""

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    OpenAI,
    OpenAIError,
)

from src.prompts import build_grading_prompt


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


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

    def user_message(self) -> str:
        """Return a short message that is useful for non-technical users."""
        error_text = str(self.original_error)
        if self.status_code == 401:
            return "DeepSeek rejected the API key. Check that DEEPSEEK_API_KEY is correct."
        if self.status_code in {402, 429}:
            return "DeepSeek could not process the request. Check your account balance, quota, or rate limit."
        if self.status_code:
            return f"DeepSeek returned HTTP {self.status_code}. Please try again or check your API account."
        if "timed out" in error_text.lower() or "timeout" in error_text.lower():
            return "The request timed out. Please try again with a stable network connection."
        if "connection" in error_text.lower() or "winerror" in error_text.lower():
            return "The app could not connect to DeepSeek from this Streamlit session."
        return "The AI request failed. Please check the setup and try again."


def get_provider_config(provider: str) -> tuple[str, str | None, str]:
    """Return environment variable name, API key, and base URL for a provider."""
    if provider == "DeepSeek":
        base_url = os.getenv("DEEPSEEK_BASE_URL", DEEPSEEK_DEFAULT_BASE_URL).strip()
        base_url = base_url.rstrip("/") or DEEPSEEK_DEFAULT_BASE_URL
        if base_url == "https://api.deepseek.com":
            base_url = DEEPSEEK_DEFAULT_BASE_URL
        return (
            "DEEPSEEK_API_KEY",
            os.getenv("DEEPSEEK_API_KEY"),
            base_url,
        )

    return ("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"), "https://api.openai.com/v1")


def build_client(provider: str) -> OpenAI:
    """Create an API client for OpenAI-compatible providers."""
    key_name, api_key, base_url = get_provider_config(provider)

    if provider == "DeepSeek":
        if not api_key:
            raise ValueError(
                f"{key_name} is missing. Please set it before running the app."
            )

        return OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    if not api_key:
        raise ValueError(
            f"{key_name} is missing. Please set it before running the app."
        )

    return OpenAI(api_key=api_key)


def build_deepseek_http_error(response: requests.Response) -> RuntimeError:
    """Include DeepSeek status code and response body in request errors."""
    return RuntimeError(
        f"DeepSeek request failed.\n"
        f"Status code: {response.status_code}\n"
        f"Response text:\n{response.text}"
    )


def call_deepseek(messages: list[dict[str, str]]) -> str:
    """Send messages to DeepSeek with a direct HTTP request."""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    response = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "deepseek-chat",
            "messages": messages,
        },
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    return result["choices"][0]["message"]["content"]


def test_deepseek_connection() -> dict[str, object]:
    """Send a tiny DeepSeek request for the sidebar connection test."""
    try:
        started_at = time.perf_counter()
        reply = call_deepseek([{"role": "user", "content": "hello"}])
        latency_ms = round((time.perf_counter() - started_at) * 1000)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "reply": reply,
        }
    except requests.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else None
        error = (
            build_deepseek_http_error(response)
            if response is not None
            else RuntimeError(str(exc))
        )
        raise AIGraderError(
            provider="DeepSeek",
            model="deepseek-chat",
            base_url=DEEPSEEK_DEFAULT_BASE_URL,
            api_key_loaded=bool(os.getenv("DEEPSEEK_API_KEY")),
            original_error=error,
            status_code=status_code,
        ) from exc
    except Exception as exc:
        raise AIGraderError(
            provider="DeepSeek",
            model="deepseek-chat",
            base_url=DEEPSEEK_DEFAULT_BASE_URL,
            api_key_loaded=bool(os.getenv("DEEPSEEK_API_KEY")),
            original_error=exc,
        ) from exc


def grade_essay(provider: str, task_type: str, topic: str, essay: str, model: str) -> str:
    """Send the IELTS essay to an AI provider and return a markdown correction report."""
    _, api_key, base_url = get_provider_config(provider)

    prompt = build_grading_prompt(
        task_type=task_type,
        topic=topic,
        essay=essay,
    )

    messages = [
        {
            "role": "system",
            "content": "You are an expert IELTS Writing examiner.",
        },
        {"role": "user", "content": prompt},
    ]

    if provider == "DeepSeek":
        base_url = "https://api.deepseek.com/v1"
        model = "deepseek-chat"
        api_key = os.getenv("DEEPSEEK_API_KEY")

        try:
            result_text = call_deepseek(messages)
        except requests.HTTPError as exc:
            response = exc.response
            status_code = response.status_code if response is not None else None
            error = (
                build_deepseek_http_error(response)
                if response is not None
                else RuntimeError(str(exc))
            )
            raise AIGraderError(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key_loaded=bool(api_key),
                original_error=error,
                status_code=status_code,
            ) from exc
        except Exception as exc:
            raise AIGraderError(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key_loaded=bool(api_key),
                original_error=exc,
            ) from exc

        return result_text

    client = build_client(provider)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
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

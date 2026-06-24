import os

from openai import OpenAI

from src.prompts import build_grading_prompt


def build_client(provider: str) -> OpenAI:
    """Create an API client for DeepSeek or OpenAI."""
    if provider == "DeepSeek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is missing. Please set it before running the app."
            )

        return OpenAI(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is missing. Please set it before running the app."
        )

    return OpenAI(api_key=api_key)


def grade_essay(provider: str, task_type: str, topic: str, essay: str, model: str) -> str:
    """Send the IELTS essay to an AI provider and return a markdown correction report."""
    client = build_client(provider)

    prompt = build_grading_prompt(
        task_type=task_type,
        topic=topic,
        essay=essay,
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are an expert IELTS Writing examiner."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    return response.choices[0].message.content or ""

import os

from openai import OpenAI

from src.prompts import build_grading_prompt


def grade_essay(task_type: str, topic: str, essay: str, model: str) -> str:
    """Send the IELTS essay to OpenAI and return a markdown correction report."""
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is missing. Please set it before running the app.")

    client = OpenAI()

    prompt = build_grading_prompt(
        task_type=task_type,
        topic=topic,
        essay=essay,
    )

    response = client.responses.create(
        model=model,
        input=prompt,
        temperature=0.2,
    )

    return response.output_text

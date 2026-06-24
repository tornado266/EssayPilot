from datetime import datetime
from pathlib import Path
from textwrap import dedent


RECORDS_DIR = Path("records")


def save_markdown_record(
    task_type: str,
    topic: str,
    essay: str,
    report: str,
    word_count: int,
) -> Path:
    """Save one correction record as a local markdown file."""
    RECORDS_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = RECORDS_DIR / f"ielts_{task_type.lower().replace(' ', '_')}_{timestamp}.md"

    content = dedent(
        f"""
        # IELTS Writing Correction Record

        - Task Type: {task_type}
        - Word Count: {word_count}
        - Created At: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

        ## Essay Question

        {topic}

        ## Student Essay

        {essay}

        ---

        {report}
        """
    ).strip()

    file_path.write_text(content, encoding="utf-8")
    return file_path

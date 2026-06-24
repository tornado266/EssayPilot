from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from src.ai_grader import grade_essay
from src.storage import save_markdown_record


load_dotenv()

st.set_page_config(
    page_title="IELTS Writing Skill",
    page_icon=":memo:",
    layout="wide",
)


def show_markdown_file(path: Path) -> None:
    """Show a saved markdown file inside Streamlit."""
    with path.open("r", encoding="utf-8") as file:
        st.download_button(
            label="Download Markdown Record",
            data=file.read(),
            file_name=path.name,
            mime="text/markdown",
        )


st.title("IELTS Writing Correction Skill")
st.caption("A beginner-friendly IELTS essay checker powered by Python, Streamlit, and OpenAI.")

with st.sidebar:
    st.header("Settings")
    task_type = st.radio("IELTS task type", ["Task 2", "Task 1"], horizontal=True)
    model = st.text_input("OpenAI model", value="gpt-4.1-mini")
    st.info("Set your OPENAI_API_KEY before running the app.")

topic = st.text_area(
    "Essay question",
    height=120,
    placeholder="Paste the IELTS Writing question here.",
)

essay = st.text_area(
    "Your essay",
    height=320,
    placeholder="Paste your full essay here.",
)

submitted = st.button("Grade My Essay", type="primary")

if submitted:
    if not topic.strip() or not essay.strip():
        st.error("Please enter both the essay question and your essay.")
    else:
        with st.spinner("The IELTS Skill is reading, scoring, and rewriting your essay..."):
            try:
                report = grade_essay(
                    task_type=task_type,
                    topic=topic,
                    essay=essay,
                    model=model,
                )
                saved_path = save_markdown_record(
                    task_type=task_type,
                    topic=topic,
                    essay=essay,
                    report=report,
                )
            except Exception as exc:
                st.error(f"Something went wrong: {exc}")
            else:
                st.success("Correction complete.")

                left, right = st.columns([2, 1])

                with left:
                    st.markdown(report)

                with right:
                    st.subheader("Saved Record")
                    st.write(str(saved_path))
                    show_markdown_file(saved_path)

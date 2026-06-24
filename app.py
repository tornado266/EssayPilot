"""Streamlit app entry point for the IELTS Writing Correction Skill."""

import base64
import re
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.ai_grader import AIGraderError, grade_essay
from src.error_book import append_error_book
from src.storage import list_correction_history, save_markdown_record
from src.text_utils import count_words, word_count_warning


load_dotenv()

BASE_DIR = Path(__file__).parent
BACKGROUND_IMAGE = BASE_DIR / "assets" / "hawaii-background.png"
SCORE_PATTERN = re.compile(r"(?:Likely Score|Overall Band|likely score)[^\d]*(\d(?:\.\d)?)")


def image_to_base64(path: Path) -> str:
    """Convert a local image to base64 so Streamlit can use it in CSS."""
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def inject_page_style() -> None:
    """Add a calm ChatGPT/Claude/Notion-inspired visual style."""
    background = image_to_base64(BACKGROUND_IMAGE)
    st.markdown(
        f"""
        <style>
        [data-testid="stAppViewContainer"] {{
            background:
                linear-gradient(120deg, rgba(255, 255, 255, 0.86), rgba(244, 247, 246, 0.78)),
                url("data:image/png;base64,{background}");
            background-size: cover;
            background-position: center;
            background-attachment: fixed;
        }}

        [data-testid="stHeader"] {{
            background: transparent;
        }}

        [data-testid="stSidebar"] > div:first-child {{
            background:
                linear-gradient(
                    180deg,
                    rgba(255, 255, 255, 0.86),
                    rgba(223, 250, 255, 0.78)
                );
            backdrop-filter: blur(18px);
            border-right: 1px solid rgba(255, 255, 255, 0.55);
        }}

        [data-testid="stSidebar"] * {{
            color: #12343b;
        }}

        .block-container {{
            max-width: 1240px;
            padding-top: 2.2rem;
            padding-bottom: 3rem;
            background: rgba(255, 255, 255, 0.54);
            border: 1px solid rgba(255, 255, 255, 0.54);
            border-radius: 8px;
            backdrop-filter: blur(10px);
        }}

        h1 {{
            color: #16323a;
            font-weight: 800;
            letter-spacing: 0;
        }}

        h2, h3 {{
            color: #1d3f46;
            letter-spacing: 0;
        }}

        .stCaption, p, label, span, li {{
            color: #26474f;
        }}

        [data-baseweb="select"] > div {{
            background: rgba(255, 255, 255, 0.92);
            border: 1px solid rgba(14, 116, 144, 0.24);
            border-radius: 8px;
            color: #12343b;
            box-shadow: 0 12px 24px rgba(8, 51, 68, 0.08);
        }}

        [data-baseweb="select"] svg {{
            color: #0e7490;
        }}

        [data-testid="stVerticalBlock"] > [style*="flex-direction: column;"] {{
            gap: 1rem;
        }}

        div[data-testid="stTextArea"] textarea,
        div[data-testid="stTextInput"] input {{
            background: rgba(255, 255, 255, 0.9);
            border: 1px solid rgba(14, 116, 144, 0.22);
            border-radius: 8px;
            color: #12343b;
            box-shadow: 0 14px 30px rgba(8, 51, 68, 0.08);
        }}

        div[data-testid="stTextArea"] textarea::placeholder,
        div[data-testid="stTextInput"] input::placeholder {{
            color: #6b8790;
            opacity: 1;
        }}

        div[data-testid="stTextArea"] textarea:focus,
        div[data-testid="stTextInput"] input:focus {{
            border-color: #0e9f9a;
            box-shadow: 0 0 0 1px #0e9f9a, 0 16px 34px rgba(8, 51, 68, 0.1);
        }}

        div.stButton > button:first-child {{
            background: #1f6f78;
            color: #ffffff;
            border: 0;
            border-radius: 8px;
            min-height: 3rem;
            font-weight: 700;
            box-shadow: 0 16px 26px rgba(31, 111, 120, 0.25);
        }}

        div.stButton > button:first-child:hover {{
            color: #ffffff;
            border: 0;
            transform: translateY(-1px);
            box-shadow: 0 20px 32px rgba(31, 111, 120, 0.32);
        }}

        .score-card {{
            padding: 1rem;
            border: 1px solid rgba(31, 111, 120, 0.16);
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.86);
            box-shadow: 0 14px 28px rgba(8, 51, 68, 0.08);
        }}

        .score-label {{
            color: #5f7378;
            font-size: 0.82rem;
            margin-bottom: 0.2rem;
        }}

        .score-value {{
            color: #16323a;
            font-size: 1.75rem;
            font-weight: 800;
        }}

        .workspace-note {{
            padding: 1rem;
            border: 1px solid rgba(31, 111, 120, 0.14);
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.74);
        }}

        [data-testid="stMarkdownContainer"] table {{
            background: rgba(255, 255, 255, 0.88);
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 16px 30px rgba(8, 51, 68, 0.08);
        }}

        [data-testid="stAlert"] {{
            border-radius: 8px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="IELTS Writing Skill",
    page_icon=":memo:",
    layout="wide",
)

inject_page_style()


def show_markdown_file(path: Path) -> None:
    """Show a saved markdown file inside Streamlit."""
    with path.open("r", encoding="utf-8") as file:
        st.download_button(
            label="Download Markdown Record",
            data=file.read(),
            file_name=path.name,
            mime="text/markdown",
        )


def render_score_card(label: str, value: str, note: str = "") -> None:
    """Render a compact portfolio-style score card."""
    st.markdown(
        f"""
        <div class="score-card">
            <div class="score-label">{label}</div>
            <div class="score-value">{value}</div>
            <div class="score-label">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def extract_overall_score(markdown: str) -> float | None:
    """Extract the likely overall band score from the latest report text."""
    match = SCORE_PATTERN.search(markdown)
    if not match:
        return None

    return float(match.group(1))


def render_history() -> None:
    """Render local score history and trend chart."""
    history = list_correction_history()
    scored_history = [item for item in history if item["score"] is not None]

    st.subheader("History Trend")
    if not scored_history:
        st.info("No scored history yet. Complete a correction to build your trend chart.")
        return

    chart_data = pd.DataFrame(
        {
            "Practice": [item["created_at"] for item in scored_history[-10:]],
            "Band Score": [item["score"] for item in scored_history[-10:]],
        }
    )
    st.line_chart(chart_data, x="Practice", y="Band Score")
    st.caption("Showing the latest 10 saved correction records with extractable scores.")


st.title("IELTS Writing Correction Skill")
st.caption("A calm AI writing desk for IELTS feedback, revision, and progress tracking.")

with st.sidebar:
    st.header("Settings")
    task_type = st.radio("IELTS task type", ["Task 2", "Task 1"], horizontal=True)
    provider = st.selectbox("AI provider", ["DeepSeek", "OpenAI"])
    default_model = "deepseek-chat" if provider == "DeepSeek" else "gpt-4.1-mini"
    model = st.text_input("Model", value=default_model)
    st.info("For DeepSeek, set DEEPSEEK_API_KEY. For OpenAI, set OPENAI_API_KEY.")
    with st.expander("Examiner report includes", expanded=False):
        st.markdown(
            """
            1. Overall Band Score
            2. Four Criteria Scores
            3. Top 3 Score-Boosting Priorities
            4. Main Problems
            5. Sentence-level Corrections
            6. Paragraph-level Feedback
            7. Band 7.5 Rewrite
            8. Useful Expressions
            9. Seven-Day Training Plan
            10. Next Practice Task
            """
        )

input_col, result_col = st.columns([0.92, 1.28], gap="large")

with input_col:
    st.subheader("Writing Input")
    topic = st.text_area(
        "Essay question",
        height=120,
        placeholder="Paste the IELTS Writing question here.",
    )

    essay = st.text_area(
        "Your essay",
        height=360,
        placeholder="Paste your full essay here.",
    )

    word_count = count_words(essay)
    minimum_words = 150 if task_type == "Task 1" else 250
    count_label = f"Word count: {word_count} / {minimum_words}+"

    metric_a, metric_b = st.columns(2)
    with metric_a:
        render_score_card("Words", str(word_count), f"{task_type} target: {minimum_words}+")
    with metric_b:
        render_score_card("Provider", provider, model)

    if essay.strip():
        warning = word_count_warning(task_type, word_count)
        if warning:
            st.warning(warning)
        else:
            st.success(count_label)
    else:
        st.markdown(
            """
            <div class="workspace-note">
                Paste a question and essay, then run the examiner report.
            </div>
            """,
            unsafe_allow_html=True,
        )

    submitted = st.button("Grade My Essay", type="primary", use_container_width=True)

with result_col:
    st.subheader("Examiner Workspace")
    if "latest_report" not in st.session_state:
        st.session_state.latest_report = ""
        st.session_state.latest_saved_path = None
        st.session_state.latest_error_book_path = None

    if submitted:
        if not topic.strip() or not essay.strip():
            st.error("Please enter both the essay question and your essay.")
        else:
            with st.spinner("The examiner is scoring, diagnosing, and rewriting..."):
                try:
                    report = grade_essay(
                        provider=provider,
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
                        word_count=word_count,
                    )
                    error_book_path = append_error_book(
                        task_type=task_type,
                        topic=topic,
                        report=report,
                    )
                    st.session_state.latest_report = report
                    st.session_state.latest_saved_path = saved_path
                    st.session_state.latest_error_book_path = error_book_path
                except AIGraderError as exc:
                    st.error("The AI request failed. Full diagnostic details are below.")
                    st.code(str(exc), language="text")
                except Exception as exc:
                    st.error("Unexpected app error. Full diagnostic details are below.")
                    st.code(
                        f"Exception Type: {type(exc).__name__}\n\n"
                        f"{type(exc).__name__}:\n{exc}",
                        language="text",
                    )

    if st.session_state.latest_report:
        score = extract_overall_score(st.session_state.latest_report)
        score_text = f"{score:.1f}" if score is not None else "Pending"
        card_one, card_two, card_three = st.columns(3)
        with card_one:
            render_score_card("Overall", score_text, "estimated band")
        with card_two:
            render_score_card("Task", task_type, "IELTS mode")
        with card_three:
            render_score_card("Words", str(word_count), "current essay")

        tab_report, tab_files = st.tabs(["Report", "Saved Files"])
        with tab_report:
            st.markdown(st.session_state.latest_report)
        with tab_files:
            if st.session_state.latest_saved_path:
                st.write(str(st.session_state.latest_saved_path))
                show_markdown_file(st.session_state.latest_saved_path)
            if st.session_state.latest_error_book_path:
                st.write(str(st.session_state.latest_error_book_path))
    else:
        st.markdown(
            """
            <div class="workspace-note">
                Your score cards and examiner report will appear here after grading.
            </div>
            """,
            unsafe_allow_html=True,
        )

st.divider()
render_history()

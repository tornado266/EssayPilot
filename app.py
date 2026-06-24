import base64
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from src.ai_grader import grade_essay
from src.error_book import append_error_book
from src.storage import save_markdown_record
from src.text_utils import count_words, word_count_warning


load_dotenv()

BASE_DIR = Path(__file__).parent
BACKGROUND_IMAGE = BASE_DIR / "assets" / "hawaii-background.png"


def image_to_base64(path: Path) -> str:
    """Convert a local image to base64 so Streamlit can use it in CSS."""
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def inject_page_style() -> None:
    """Add a bright tropical visual style to the Streamlit page."""
    background = image_to_base64(BACKGROUND_IMAGE)
    st.markdown(
        f"""
        <style>
        [data-testid="stAppViewContainer"] {{
            background:
                linear-gradient(120deg, rgba(255, 255, 255, 0.78), rgba(236, 252, 255, 0.62)),
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
            max-width: 1120px;
            padding-top: 2.6rem;
            padding-bottom: 3rem;
            background: rgba(255, 255, 255, 0.38);
            border: 1px solid rgba(255, 255, 255, 0.54);
            border-radius: 8px;
            backdrop-filter: blur(10px);
        }}

        h1 {{
            color: #064e5a;
            font-weight: 800;
            letter-spacing: 0;
        }}

        .stCaption, p, label, span {{
            color: #134e5e;
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
            background: linear-gradient(135deg, #ff7f6e, #ffb86b);
            color: #ffffff;
            border: 0;
            border-radius: 8px;
            min-height: 3rem;
            font-weight: 700;
            box-shadow: 0 16px 26px rgba(255, 127, 110, 0.32);
        }}

        div.stButton > button:first-child:hover {{
            color: #ffffff;
            border: 0;
            transform: translateY(-1px);
            box-shadow: 0 20px 32px rgba(255, 127, 110, 0.38);
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


st.title("IELTS Writing Correction Skill")
st.caption(
    "A beginner-friendly IELTS essay checker powered by Python, "
    "Streamlit, and AI APIs."
)

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

word_count = count_words(essay)
minimum_words = 150 if task_type == "Task 1" else 250
count_label = f"Word count: {word_count} / {minimum_words}+"
if essay.strip():
    warning = word_count_warning(task_type, word_count)
    if warning:
        st.warning(warning)
    else:
        st.success(count_label)
else:
    st.caption(count_label)

submitted = st.button("Grade My Essay", type="primary")

if submitted:
    if not topic.strip() or not essay.strip():
        st.error("Please enter both the essay question and your essay.")
    else:
        with st.spinner("The IELTS Skill is reading, scoring, and rewriting your essay..."):
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
                    st.subheader("Error Book")
                    st.write(str(error_book_path))

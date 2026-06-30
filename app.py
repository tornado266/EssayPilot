"""Streamlit app entry point for the IELTS Writing Correction Skill."""

import base64
import hashlib
import re
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.ai_grader import (
    AIGraderError,
    grade_essay,
    review_logic_rewrite,
    review_sentence_rewrite,
)
from src.error_book import append_error_book
from src.storage import markdown_to_pdf, save_markdown_record
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
                linear-gradient(120deg, rgba(255, 255, 255, 0.42), rgba(244, 247, 246, 0.32)),
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
            background: rgba(255, 255, 255, 0.64);
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

        div.stButton > button:first-child,
        div.stDownloadButton > button:first-child {{
            background: #eaf7ff;
            color: #17465f;
            border: 1px solid #b8ddf2;
            border-radius: 8px;
            min-height: 3rem;
            font-weight: 700;
            box-shadow: 0 12px 24px rgba(72, 155, 196, 0.16);
        }}

        div.stButton > button:first-child *,
        div.stDownloadButton > button:first-child * {{
            color: #17465f;
        }}

        div.stButton > button:first-child:hover,
        div.stDownloadButton > button:first-child:hover {{
            background: #d9f0fc;
            color: #123f57;
            border-color: #8fc9e7;
            transform: translateY(-1px);
            box-shadow: 0 16px 28px rgba(72, 155, 196, 0.22);
        }}

        div.stButton > button:first-child:active,
        div.stDownloadButton > button:first-child:active {{
            background: #c8e8f8;
            transform: translateY(0);
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
    """Offer the complete record as Markdown and PDF downloads."""
    markdown = path.read_text(encoding="utf-8")
    pdf = markdown_to_pdf(markdown)
    markdown_column, pdf_column = st.columns(2)
    with markdown_column:
        st.download_button(
            label="Download Markdown",
            data=markdown,
            file_name=path.name,
            mime="text/markdown",
            use_container_width=True,
        )
    with pdf_column:
        st.download_button(
            label="Download PDF",
            data=pdf,
            file_name=f"{path.stem}.pdf",
            mime="application/pdf",
            use_container_width=True,
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


def get_band_color(score: float | None) -> tuple[str, str]:
    """Return display colors for the overall IELTS band score."""
    if score is None:
        return ("#607d8b", "#eef5f7")
    if score < 6:
        return ("#b42318", "#fff1f0")
    if score < 7:
        return ("#b54708", "#fff7ed")
    return ("#027a48", "#ecfdf3")


def extract_report_section(markdown: str, number: int) -> str:
    """Safely extract a numbered report section from Markdown."""
    heading = rf"#{1,3}\s*{number}\s*[.:、-]?\s+"
    next_heading = r"\n#{1,3}\s*\d+\s*[.:、-]?\s+"
    match = re.search(
        rf"{heading}.*?(?={next_heading}|\Z)",
        markdown,
        flags=re.DOTALL,
    )
    return match.group(0).strip() if match else ""


def clean_markdown_text(text: str) -> str:
    """Remove simple Markdown markers for compact card display."""
    cleaned = re.sub(r"^#{1,6}\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"^[-*]\s*", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def extract_bullets(section: str, limit: int = 4) -> list[str]:
    """Extract short bullet-like items from a report section."""
    if not section:
        return []

    items: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("|") or re.fullmatch(r"[-:| ]+", stripped):
            continue
        if re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            stripped = re.sub(r"^[-*]\s+|^\d+\.\s+", "", stripped)
            items.append(clean_markdown_text(stripped))
        elif any(prefix in stripped.lower() for prefix in ("problem", "priority", "why", "how")):
            items.append(clean_markdown_text(stripped))

    deduped: list[str] = []
    for item in items:
        if item and item not in deduped:
            deduped.append(item)

    return deduped[:limit]


def extract_criteria_scores(markdown: str) -> dict[str, str]:
    """Extract likely criterion scores from the four-criteria table."""
    section = extract_report_section(markdown, 2)
    scores = {
        "Task Response": "-",
        "Coherence": "-",
        "Lexical Resource": "-",
        "Grammar": "-",
    }

    for line in section.splitlines():
        if not line.strip().startswith("|") or "---" in line:
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue

        criterion = cells[0].lower()
        if criterion == "criterion":
            continue
        likely_score = cells[2] or cells[1] or "-"
        score_match = re.search(r"\d(?:\.\d)?", likely_score)
        likely_score = score_match.group(0) if score_match else likely_score
        if "task" in criterion:
            scores["Task Response"] = likely_score
        elif "coherence" in criterion:
            scores["Coherence"] = likely_score
        elif "lexical" in criterion:
            scores["Lexical Resource"] = likely_score
        elif "grammar" in criterion or "grammatical" in criterion:
            scores["Grammar"] = likely_score

    return scores


def extract_criteria_details(markdown: str) -> dict[str, dict[str, str]]:
    """Extract per-criterion comments for expandable score details."""
    section = extract_report_section(markdown, 2) or markdown
    details = {
        "Task Response": {"score": "-", "good": "", "problem": ""},
        "Coherence": {"score": "-", "good": "", "problem": ""},
        "Lexical Resource": {"score": "-", "good": "", "problem": ""},
        "Grammar": {"score": "-", "good": "", "problem": ""},
    }

    for line in section.splitlines():
        if not line.strip().startswith("|") or "---" in line:
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0].lower() == "criterion":
            continue

        criterion = cells[0].lower()
        score = cells[2] or cells[1] or "-"
        score_match = re.search(r"\d(?:\.\d)?", score)
        score = score_match.group(0) if score_match else score
        why = clean_markdown_text(cells[3])

        if "task" in criterion:
            key = "Task Response"
        elif "coherence" in criterion:
            key = "Coherence"
        elif "lexical" in criterion:
            key = "Lexical Resource"
        elif "grammar" in criterion or "grammatical" in criterion:
            key = "Grammar"
        else:
            continue

        detail_parts = re.split(r"(?<=[.!?])\s+|;\s+", why)
        positive_markers = (
            "clear",
            "relevant",
            "logical",
            "accurate",
            "appropriate",
            "effective",
            "good",
            "varied",
            "well",
        )
        positive_parts: list[str] = []
        problem_parts: list[str] = []
        for part in detail_parts:
            clauses = re.split(
                r"\s+(?:but|however|although|yet|while)\s+",
                part,
                maxsplit=1,
                flags=re.IGNORECASE,
            )
            positive_clause = clauses[0].strip()
            if any(marker in positive_clause.lower() for marker in positive_markers):
                positive_parts.append(positive_clause.rstrip(","))
            else:
                problem_parts.append(positive_clause)
            if len(clauses) > 1 and clauses[1].strip():
                problem_parts.append(clauses[1].strip())

        details[key]["score"] = score
        details[key]["good"] = positive_parts[0] if positive_parts else ""
        details[key]["problem"] = " ".join(problem_parts) or why

    return details


def extract_problem_evidence_by_criterion(markdown: str) -> dict[str, list[str]]:
    """Extract quoted problem evidence and map it loosely to IELTS criteria."""
    section = extract_report_section(markdown, 4)
    evidence = {
        "Task Response": [],
        "Coherence": [],
        "Lexical Resource": [],
        "Grammar": [],
    }

    problem_blocks = re.split(r"#{2,4}\s*Problem\s*\d+:", section)
    for block in problem_blocks[1:]:
        lower_block = block.lower()
        quotes = re.findall(r'["“]([^"”]+)["”]', block)
        original_match = re.search(r"\*\*Original sentence:\*\*\s*(.+)", block)
        if original_match:
            original = clean_markdown_text(original_match.group(1)).strip('"“”')
            if original:
                quotes.insert(0, original)

        if not quotes:
            continue

        if any(word in lower_block for word in ("grammar", "verb", "sentence structure")):
            key = "Grammar"
        elif any(word in lower_block for word in ("vocabulary", "lexical", "word", "phrase")):
            key = "Lexical Resource"
        elif any(word in lower_block for word in ("coherence", "paragraph", "logic", "progression")):
            key = "Coherence"
        else:
            key = "Task Response"

        for quote in quotes:
            cleaned = quote.strip()
            if cleaned and cleaned not in evidence[key]:
                evidence[key].append(cleaned)

    return evidence


def extract_paragraph_strengths(markdown: str) -> list[str]:
    """Extract positive paragraph-level observations from the existing report."""
    section = extract_report_section(markdown, 6) or markdown
    strengths: list[str] = []
    for match in re.finditer(
        r"(?:\*\*)?(?:What works|加分项)(?:\*\*)?\s*:\s*"
        r"(.+?)(?=\n\s*(?:[-*]\s*)?\*\*|\n#{1,4}|\Z)",
        section,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        item = clean_markdown_text(match.group(1)).lstrip("*- ")
        if item and item not in strengths:
            strengths.append(item)
    return strengths


def render_overall_band(score: float | None) -> None:
    """Render the hero-style overall IELTS band card."""
    color, background = get_band_color(score)
    score_text = f"{score:.1f}" if score is not None else "Pending"
    st.markdown(
        f"""
        <div style="
            text-align:center;
            padding:1.6rem 1rem;
            border-radius:8px;
            background:{background};
            border:1px solid {color}33;
            box-shadow:0 18px 34px rgba(8,51,68,0.08);
            margin-bottom:1rem;
        ">
            <div style="font-size:0.95rem;font-weight:700;color:#31545c;">
                Overall Band Score
            </div>
            <div style="font-size:4rem;line-height:1;font-weight:900;color:{color};">
                {score_text}
            </div>
            <div style="font-size:0.9rem;color:#5f7378;margin-top:0.35rem;">
                estimated IELTS writing performance
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_criteria_overview(markdown: str) -> None:
    """Render four expandable IELTS criterion score cards in a readable grid."""
    details = extract_criteria_details(markdown)
    problem_evidence = extract_problem_evidence_by_criterion(markdown)
    strengths = extract_paragraph_strengths(markdown)
    detail_items = list(details.items())

    for row_start in range(0, len(detail_items), 2):
        columns = st.columns(2)
        for index, (column, (label, detail)) in enumerate(
            zip(columns, detail_items[row_start : row_start + 2], strict=False),
            start=row_start,
        ):
            with column:
                render_score_card(label, detail.get("score", "-"), "tap to expand below")
                with st.expander("查看详情", expanded=False):
                    good_text = detail.get("good") or (
                        strengths[index % len(strengths)] if strengths else ""
                    )
                    problem_text = detail.get("problem") or "暂未提取到主要问题。"
                    evidence_items = problem_evidence.get(label, [])

                    st.success(f"加分项：{good_text or '暂未提取到明确加分项。'}")

                    if evidence_items:
                        for item in evidence_items[:2]:
                            st.error(f"主要问题依据：{item}")
                    else:
                        st.error(f"主要问题：{problem_text}")


def render_problem_cards(markdown: str) -> None:
    """Render main problems as warning cards."""
    st.subheader("Main Problems")
    problems = extract_bullets(extract_report_section(markdown, 4), limit=5)
    if not problems:
        st.info("No main problems were extracted from this report.")
        return

    for problem in problems:
        st.warning(problem)


def render_suggestion_cards(markdown: str) -> None:
    """Render improvement suggestions as success cards."""
    st.subheader("Improvement Suggestions")
    suggestions = extract_bullets(extract_report_section(markdown, 3), limit=5)
    if not suggestions:
        suggestions = extract_bullets(extract_report_section(markdown, 10), limit=3)
    if not suggestions:
        st.info("No improvement suggestions were extracted from this report.")
        return

    for suggestion in suggestions:
        st.success(suggestion)


def report_before_interactive_practice(markdown: str) -> str:
    """Return the static report content before interactive practice sections."""
    parts = re.split(
        r"\n#{1,3}\s*11\.\s*单句提分训练",
        markdown,
        maxsplit=1,
    )
    report = parts[0].rstrip()
    report = re.sub(
        r"\n#{1,3}\s*9\s*[.:、-]?\s+Seven-Day Training Plan.*?"
        r"(?=\n#{1,3}\s*10\s*[.:、-]?\s+|\Z)",
        "",
        report,
        flags=re.DOTALL | re.IGNORECASE,
    )
    report = re.sub(
        r"(#{1,3}\s*)10(\s*[.:、-]?\s+Next Practice Task)",
        r"\g<1>9\g<2>",
        report,
        count=1,
        flags=re.IGNORECASE,
    )
    report = re.sub(r"^#\s*IELTS Writing Examiner Report\s*", "", report).strip()
    return report


def extract_practice_sentences(markdown: str) -> list[str]:
    """Extract original sentences from the single-sentence practice section."""
    section_match = re.search(
        r"#{1,3}\s*11\.\s*单句提分训练(?P<section>.*)",
        markdown,
        flags=re.DOTALL,
    )
    if not section_match:
        section_match = re.search(
            r"单句提分训练(?P<section>.*)",
            markdown,
            flags=re.DOTALL,
        )
    if not section_match:
        section_match = re.search(
            r"【练习任务】(?P<section>.*)",
            markdown,
            flags=re.DOTALL,
        )
    if not section_match:
        return []

    section = section_match.group("section")
    section = re.split(r"#{1,3}\s*12\.\s*写作提升验证", section, maxsplit=1)[0]
    exercise_part = section.split("【参考改写】", 1)[0]
    candidates = re.findall(r'["“]([^"”]+)["”]', exercise_part)

    sentences: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip()
        if cleaned and cleaned not in {"（原句）", "(原句)"} and cleaned not in sentences:
            sentences.append(cleaned)

    return sentences[:5]


def extract_sentence_references(markdown: str) -> dict[str, str]:
    """Extract sentence-level reference rewrites from the existing correction table."""
    section = extract_report_section(markdown, 5)
    references: dict[str, str] = {}

    for line in section.splitlines():
        if not line.strip().startswith("|") or "---" in line:
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "original":
            continue

        original = clean_markdown_text(cells[0]).strip('"“”')
        improved = clean_markdown_text(cells[-1]).strip('"“”')
        if original and improved:
            references[original] = improved

    return references


def find_sentence_reference(sentence: str, references: dict[str, str]) -> str | None:
    """Find a compatible reference rewrite for a practice sentence."""
    normalized_sentence = re.sub(r"\s+", " ", sentence).strip().lower()
    for original, improved in references.items():
        normalized_original = re.sub(r"\s+", " ", original).strip().lower()
        if normalized_sentence == normalized_original:
            return improved
        if normalized_sentence in normalized_original or normalized_original in normalized_sentence:
            return improved
    return None


def render_sentence_practice(
    sentences: list[str],
    provider: str,
    model: str,
    references: dict[str, str] | None = None,
) -> None:
    """Render the interactive sentence rewrite practice."""
    st.subheader("单句提分训练")

    if not sentences:
        st.info("还没有识别到可练习的原句。请先重新生成一次报告。")
        return

    st.caption("先自己改写，再点击点评。AI 会根据你的版本给出具体建议。")
    references = references or {}

    for index, original_sentence in enumerate(sentences, start=1):
        sentence_id = hashlib.md5(original_sentence.encode("utf-8")).hexdigest()[:10]
        rewrite_key = f"sentence_rewrite_{sentence_id}"
        reference_key = f"sentence_reference_{sentence_id}"
        button_key = f"sentence_review_button_{sentence_id}"
        feedback_key = f"sentence_feedback_{sentence_id}"

        with st.container(border=True):
            st.markdown(f"**原句 {index}:** {original_sentence}")
            rewrite = st.text_area(
                "你的改写",
                key=rewrite_key,
                height=90,
                placeholder="在这里输入你改写后的完整句子。",
            )

            if st.button("显示参考答案", key=reference_key):
                reference = find_sentence_reference(original_sentence, references)
                if reference:
                    st.info(reference)
                else:
                    st.info("暂时没有匹配到参考答案。你提交改写后，AI 点评会给出更自然的版本。")

            if st.button("点评我的改写", key=button_key):
                if not rewrite.strip():
                    st.warning("请先输入你的改写句子。")
                else:
                    with st.spinner("AI 正在点评你的句子..."):
                        try:
                            st.session_state[feedback_key] = review_sentence_rewrite(
                                provider=provider,
                                original_sentence=original_sentence,
                                student_rewrite=rewrite,
                                model=model,
                            )
                        except AIGraderError as exc:
                            st.error("点评失败。完整诊断信息如下。")
                            st.code(str(exc), language="text")
                        except Exception as exc:
                            st.error("点评时出现意外错误。")
                            st.code(
                                f"Exception Type: {type(exc).__name__}\n\n{exc}",
                                language="text",
                            )

            if st.session_state.get(feedback_key):
                st.markdown(st.session_state[feedback_key])


def extract_logic_practice_tasks(markdown: str) -> list[dict[str, str]]:
    """Extract logic-level writing practice tasks from the report."""
    section_match = re.search(
        r"#{1,3}\s*12\.\s*写作提升验证(?P<section>.*)",
        markdown,
        flags=re.DOTALL,
    )
    if not section_match:
        section_match = re.search(
            r"【提升练习】(?P<section>.*)",
            markdown,
            flags=re.DOTALL,
        )
    if not section_match:
        return []

    section = section_match.group("section")
    blocks = re.split(r"#{2,4}\s*任务\s*\d+", section)
    tasks: list[dict[str, str]] = []

    for block in blocks[1:]:
        problem_match = re.search(r"问题：\s*(.+)", block)
        quotes = re.findall(r'["“]([^"”]+)["”]', block, flags=re.DOTALL)
        if not quotes:
            continue

        problem = problem_match.group(1).strip() if problem_match else "逻辑/结构问题"
        original_fragment = quotes[0].strip()
        if original_fragment:
            tasks.append(
                {
                    "problem": problem,
                    "original": original_fragment,
                }
            )

    return tasks[:3]


def render_logic_practice(
    tasks: list[dict[str, str]],
    provider: str,
    model: str,
) -> None:
    """Render interactive logic and structure rewrite practice."""
    st.subheader("写作提升验证")

    if not tasks:
        st.info("还没有识别到可练习的思路提升任务。请先重新生成一次报告。")
        return

    st.caption("重写一个关键片段，再让 AI 对比原文和你的版本。")

    for index, task in enumerate(tasks, start=1):
        logic_source = f"{task['problem']}|{task['original']}"
        logic_id = hashlib.md5(logic_source.encode("utf-8")).hexdigest()[:10]
        rewrite_key = f"logic_rewrite_{logic_id}"
        button_key = f"logic_review_button_{logic_id}"
        feedback_key = f"logic_feedback_{logic_id}"

        with st.container(border=True):
            st.markdown(f"**任务 {index}:** {task['problem']}")
            st.markdown("改写/重写下面内容，使其逻辑更清晰、更符合雅思6.5水平：")
            st.markdown(f"> {task['original']}")
            st.markdown("要求：2-4句话；要有清晰论点 + 解释 + 例子。")

            rewrite = st.text_area(
                "你的重写",
                key=rewrite_key,
                height=130,
                placeholder="在这里输入你的2-4句话重写版本。",
            )

            if st.button("点评我的思路重写", key=button_key):
                if not rewrite.strip():
                    st.warning("请先输入你的重写内容。")
                else:
                    with st.spinner("AI 正在对比你的逻辑结构..."):
                        try:
                            st.session_state[feedback_key] = review_logic_rewrite(
                                provider=provider,
                                problem=task["problem"],
                                original_fragment=task["original"],
                                student_rewrite=rewrite,
                                model=model,
                            )
                        except AIGraderError as exc:
                            st.error("点评失败。完整诊断信息如下。")
                            st.code(str(exc), language="text")
                        except Exception as exc:
                            st.error("点评时出现意外错误。")
                            st.code(
                                f"Exception Type: {type(exc).__name__}\n\n{exc}",
                                language="text",
                            )

            if st.session_state.get(feedback_key):
                st.markdown(st.session_state[feedback_key])


def list_correction_history() -> list[dict[str, object]]:
    """Read saved records for the dashboard trend chart."""
    records_dir = BASE_DIR / "records"
    if not records_dir.exists():
        return []

    history: list[dict[str, object]] = []
    for path in sorted(records_dir.glob("ielts_*.md")):
        markdown = path.read_text(encoding="utf-8")
        created_match = re.search(r"- Created At:\s*(.+)", markdown)
        task_match = re.search(r"- Task Type:\s*(.+)", markdown)
        words_match = re.search(r"- Word Count:\s*(\d+)", markdown)

        history.append(
            {
                "file": path.name,
                "path": path,
                "created_at": created_match.group(1) if created_match else path.stem,
                "task_type": task_match.group(1) if task_match else "Unknown",
                "word_count": int(words_match.group(1)) if words_match else None,
                "score": extract_overall_score(markdown),
            }
        )

    return history


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
    trend_chart = (
        alt.Chart(chart_data)
        .mark_line(point=alt.OverlayMarkDef(filled=True, size=85), strokeWidth=3)
        .encode(
            x=alt.X(
                "Practice:N",
                sort=None,
                title=None,
                axis=alt.Axis(labelAngle=-25, labelLimit=150),
            ),
            y=alt.Y(
                "Band Score:Q",
                title="Band Score",
                scale=alt.Scale(domain=[3, 9], clamp=True),
                axis=alt.Axis(values=[3, 4, 5, 6, 7, 8, 9]),
            ),
            tooltip=[
                alt.Tooltip("Practice:N", title="Practice"),
                alt.Tooltip("Band Score:Q", title="Band Score", format=".1f"),
            ],
        )
        .properties(height=300)
        .configure_view(strokeWidth=0)
        .configure_axis(gridColor="#d9e8ea", labelColor="#526d73", titleColor="#294e56")
        .configure_line(color="#287d86")
        .configure_point(color="#e87961")
    )
    st.altair_chart(trend_chart, width="stretch")
    st.caption("Showing the latest 10 saved correction records with extractable scores.")


st.title("IELTS Writing Correction Skill")
st.caption("A calm AI writing desk for IELTS feedback, revision, and progress tracking.")

with st.sidebar:
    st.header("Settings")
    task_type = st.radio("IELTS task type", ["Task 2", "Task 1"], horizontal=True)
    provider = st.selectbox("AI provider", ["DeepSeek", "OpenAI"])
    default_model = "deepseek-chat" if provider == "DeepSeek" else "gpt-4.1-mini"
    model = st.text_input("Model", value=default_model)
    st.info(
        "Add API keys in Streamlit Secrets for deployment, or use a local .env file."
    )
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
            9. Next Practice Task
            11. 单句提分训练
            12. 写作提升验证
            """
        )

with st.container():
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

st.divider()

with st.container():
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

        tab_report, tab_files = st.tabs(["Report", "Saved Files"])
        with tab_report:
            render_overall_band(score)
            render_criteria_overview(st.session_state.latest_report)

            st.divider()
            with st.expander("Full Examiner Report", expanded=False):
                st.markdown(report_before_interactive_practice(st.session_state.latest_report))

            st.divider()
            with st.expander("Practice Task", expanded=False):
                practice_sentences = extract_practice_sentences(st.session_state.latest_report)
                sentence_references = extract_sentence_references(st.session_state.latest_report)
                render_sentence_practice(
                    practice_sentences,
                    provider,
                    model,
                    references=sentence_references,
                )

            st.divider()
            with st.expander("Logic Check", expanded=False):
                logic_tasks = extract_logic_practice_tasks(st.session_state.latest_report)
                render_logic_practice(logic_tasks, provider, model)
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

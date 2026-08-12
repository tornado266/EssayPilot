"""Streamlit app entry point for the IELTS Writing Correction Skill."""

import base64
import hashlib
import html
import json
import re
import uuid
from collections import Counter
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.ai_grader import (
    AIGraderError,
    EXPRESSION_PRACTICE_PROMPT_VERSION,
    PRODUCTION_MODEL,
    compare_draft_progress,
    grade_essay_package,
    review_logic_rewrite,
    review_expression_sentence,
    review_sentence_rewrite,
)
from src.admin_dashboard import is_admin_request, render_admin_dashboard
from src.analytics import record_grading_event
from src.cloud_store import CloudStoreError, CloudUser, SupabaseStore
from src.draft_training import list_draft_training_history, save_draft_training_record
from src.error_book import append_error_book
from src.learning_assets import (
    CATEGORY_LABELS,
    build_learning_items,
    catalog_learning_item,
    criterion_for_problem,
)
from src.expression_catalog import FUNCTION_LABELS, TOPIC_LABELS, load_expression_catalog
from src.share_card import build_result_card_svg
from src.storage import markdown_to_pdf, save_markdown_record
from src.report_schema import (
    ExaminerResultError,
    REPORT_PROMPT_VERSION,
    SCORING_PROMPT_VERSION,
    SCORING_SKILL_VERSION,
    calculate_overall,
    format_overall_band,
    learner_safe_report_markdown,
    score_snapshot,
    submission_hash,
)
from src.text_utils import count_words, word_count_warning
from ui.alpine import (
    inject_alpine_theme,
    render_feature_bento,
    render_hero as render_alpine_hero,
    render_scoring_loader,
    render_text_diff,
    render_training_stepper,
)


load_dotenv()

BASE_DIR = Path(__file__).parent
DEMO_REPORT_PATH = BASE_DIR / "data" / "demo_report.md"
SCORE_PATTERN = re.compile(r"(?:最可能分数|Likely Score|Overall Band Score|Overall Band|Overall|总分|likely score)[^\d]*(\d(?:\.\d)?)")
CRITERION_DISPLAY_NAMES = {
    "Task Response": "任务回应（TR）",
    "Coherence and Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammatical Range and Accuracy": "语法多样性与准确性（GRA）",
}
CRITERION_COMPACT_NAMES = {
    "Task Response": "TR 任务回应",
    "Coherence and Cohesion": "CC 连贯衔接",
    "Lexical Resource": "LR 词汇资源",
    "Grammatical Range and Accuracy": "GRA 语法准确性",
}
SCORE_DISPLAY_NAMES = {
    "Overall Band": "总分",
    "Task Response": "任务回应（TR）",
    "Coherence & Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammar Range & Accuracy": "语法多样性与准确性（GRA）",
}
ALPINE_CHART_COLORS = ["#0E3B5F", "#1769AA", "#4D8DBD", "#79AFCF"]
SAMPLE_POPOVER_TITLE = "试用作文"
SAMPLE_TOPIC = (
    "Some people believe university students should only study their main subjects, "
    "while others think they should also study other subjects. "
    "Discuss both views and give your own opinion."
)
SAMPLE_ESSAY = """Nowadays, people have different opinions about whether university students should only study their major or also learn other subjects. Both ideas have some advantages, but I think there are more benefits if students focus mainly on their major.

On the one hand, studying only the main subject can help students learn better skills. University study is difficult and students already have a lot of work to do. For example, a medical student needs to spend a lot of time reading books and doing practice. If they also study other subjects, they may feel too busy and cannot understand their main subject well. Therefore, focusing on one subject can help students prepare for their future job.

On the other hand, learning other subjects can also be useful. Students can get more knowledge and become more interested in different areas. For example, a business student can learn some computer skills, which may help them in the future. However, not all students have enough time or energy to study many subjects at the same time.

In my opinion, students should mainly study their major because it is the most important part of university education. Other subjects can be optional, but they should not take too much time. This way, students can still focus on their main goal while learning some extra knowledge.

In conclusion, both views have some reasons, but I believe focusing on the major is more important for university students."""


def load_sample_essay() -> None:
    """Load the Band 6 sample into the writing fields."""
    st.session_state.topic_input = SAMPLE_TOPIC
    st.session_state.essay_input = SAMPLE_ESSAY


def show_workspace() -> None:
    """Return to the live grading workspace."""
    st.session_state.page_mode = "write"
    st.query_params["page"] = "write"
    st.session_state.scroll_target = "workspace-top"


def show_demo() -> None:
    """Open the zero-token walkthrough."""
    st.session_state.page_mode = "demo"
    st.session_state.scroll_target = "demo-top"


def load_sample_and_show_workspace() -> None:
    """Load the sample without running the grader and return to the workspace."""
    load_sample_essay()
    st.session_state.page_mode = "write"
    st.query_params["page"] = "write"
    st.session_state.scroll_target = "writing-input"


def session_cloud_user() -> CloudUser | None:
    data = st.session_state.get("cloud_user")
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return CloudUser(
        id=str(data.get("id", "")),
        email=str(data.get("email", "")),
        access_token=str(data.get("access_token", "")),
        refresh_token=str(data.get("refresh_token", "")),
    )


def render_login_page(store: SupabaseStore) -> None:
    """Render passwordless email-code authentication without exposing provider details."""
    render_alpine_hero(variant="login")
    email = st.text_input("邮箱", key="login_email", placeholder="name@example.com")
    send_col, demo_col = st.columns(2)
    with send_col:
        if st.button("发送验证码", type="primary", use_container_width=True):
            if "@" not in email:
                st.error("请输入有效邮箱地址。")
            else:
                try:
                    store.send_email_code(email.strip())
                    st.session_state.login_code_sent = True
                    st.success("验证码已发送，请检查邮箱。")
                except CloudStoreError as exc:
                    st.error(f"验证码发送失败：{exc}")
    with demo_col:
        st.button("先看零 Token 范文", on_click=show_demo, use_container_width=True)
    if st.session_state.get("login_code_sent"):
        code = st.text_input("请输入邮箱验证码", key="login_code")
        if st.button("登录并进入学习档案", use_container_width=True):
            try:
                user = store.verify_email_code(email.strip(), code.strip())
                st.session_state.cloud_user = user.__dict__
                st.session_state.user_id = user.id
                st.rerun()
            except CloudStoreError as exc:
                st.error(f"登录失败：{exc}")


def logout_cloud_user() -> None:
    st.session_state.pop("cloud_user", None)
    st.session_state.user_id = str(uuid.uuid4())
    st.session_state.page_mode = "home"
    st.query_params.clear()


def open_cloud_login() -> None:
    """Leave a guest-only route so the normal authentication gate can render."""
    st.session_state.page_mode = "home"
    st.session_state.pop("login_code_sent", None)
    st.query_params.clear()


def sync_learning_item_status(
    store: SupabaseStore,
    user: CloudUser,
    *,
    grading_run_id: str,
    source_text: str,
    mastered: bool,
) -> None:
    """Keep practice completion resilient when a SQL migration is still pending."""
    try:
        store.update_learning_item_for_practice(
            user,
            grading_run_id=grading_run_id,
            source_text=source_text,
            mastered=mastered,
        )
    except (CloudStoreError, AttributeError):
        st.session_state.learning_assets_sync_error = True


def apply_pending_scroll() -> None:
    """Move the parent page to the requested section after a Streamlit rerun."""
    target = st.session_state.pop("scroll_target", None)
    if not target:
        return
    st.html(
        f"""
        <script>
        requestAnimationFrame(() => {{
            const target = document.getElementById("{target}");
            if (target) target.scrollIntoView({{ behavior: "instant", block: "start" }});
        }});
        </script>
        """,
        unsafe_allow_javascript=True,
    )


def render_anchor(anchor_id: str) -> None:
    """Create a stable in-page scroll target."""
    st.markdown(f'<div id="{anchor_id}" class="scroll-anchor"></div>', unsafe_allow_html=True)


def render_bookmark_rail(items: list[tuple[str, str]]) -> None:
    """Render a compact floating table of contents."""
    links = "".join(
        f'<a href="#{anchor}">{label}</a>' for label, anchor in items
    )
    active_rules = "".join(
        f'body:has(#{anchor}:target) .bookmark-rail a[href="#{anchor}"]'
        "{background:var(--ep-surface);color:var(--ep-primary-hover);box-shadow:var(--ep-shadow-sm);}"
        for _, anchor in items
    )
    st.markdown(
        f'<style>{active_rules}</style><nav class="bookmark-rail"><span>快速索引</span>{links}</nav>',
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="EssayPilot 雅思写作训练",
    page_icon=":memo:",
    layout="wide",
)

if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())
if "page_mode" not in st.session_state:
    st.session_state.page_mode = "workspace"

user_id = st.session_state.user_id

inject_alpine_theme()

if is_admin_request():
    render_admin_dashboard()
    st.stop()


def show_markdown_file(path: Path) -> None:
    """Offer the complete record as Markdown and PDF downloads."""
    markdown = path.read_text(encoding="utf-8")
    pdf = markdown_to_pdf(markdown)
    markdown_column, pdf_column = st.columns(2)
    with markdown_column:
        st.download_button(
            label="下载 Markdown 报告",
            data=markdown,
            file_name=path.name,
            mime="text/markdown",
            use_container_width=True,
        )
    with pdf_column:
        st.download_button(
            label="下载 PDF 报告",
            data=pdf,
            file_name=f"{path.stem}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )


def render_score_card(label: str, value: str, note: str = "") -> None:
    """Render a compact portfolio-style score card."""
    safe_label = html.escape(str(label))
    safe_value = html.escape(str(value))
    safe_note = html.escape(str(note))
    st.markdown(
        f"""
        <div class="score-card">
            <div class="score-label">{safe_label}</div>
            <div class="score-value">{safe_value}</div>
            <div class="score-label">{safe_note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard_stats(items: list[tuple[str, object, str]], columns: int) -> None:
    """Render compact dashboard statistics that reflow cleanly on narrow screens."""
    cards = []
    for label, value, note in items:
        cards.append(
            '<div class="dashboard-stat-card">'
            f'<div class="dashboard-stat-label">{html.escape(str(label))}</div>'
            f'<div class="dashboard-stat-value">{html.escape(str(value))}</div>'
            f'<div class="dashboard-stat-note">{html.escape(str(note))}</div>'
            "</div>"
        )
    st.markdown(
        f'<div class="dashboard-stat-grid" style="--stat-columns: {max(1, columns)}">'
        + "".join(cards)
        + "</div>",
        unsafe_allow_html=True,
    )


def extract_overall_score(markdown: str) -> float | None:
    """Extract the likely overall band score from the latest report text."""
    match = SCORE_PATTERN.search(markdown)
    if not match:
        return None

    return float(match.group(1))


def extract_report_section(markdown: str, number: int) -> str:
    """Safely extract a numbered report section from Markdown."""
    heading = rf"#{{1,3}}\s*{number}\s*[.:、-]?\s+"
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
        if criterion in {"criterion", "评分项"}:
            continue
        likely_score = (cells[2] if len(cells) >= 4 else cells[1]) or "-"
        score_match = re.search(r"\d(?:\.\d)?", likely_score)
        likely_score = score_match.group(0) if score_match else likely_score
        if "task" in criterion or "任务回应" in criterion:
            scores["Task Response"] = likely_score
        elif "coherence" in criterion or "连贯与衔接" in criterion:
            scores["Coherence"] = likely_score
        elif "lexical" in criterion or "词汇资源" in criterion:
            scores["Lexical Resource"] = likely_score
        elif "grammar" in criterion or "grammatical" in criterion or "语法多样性" in criterion:
            scores["Grammar"] = likely_score

    return scores


def calculate_overall_band(markdown: str) -> float | None:
    """Read legacy Markdown but delegate rounding and validation to the schema authority."""
    criteria = extract_criteria_scores(markdown)
    criterion_rows: list[dict[str, int]] = []
    for value in criteria.values():
        match = re.search(r"\d(?:\.\d)?", value)
        if not match:
            return extract_overall_score(markdown)
        score = float(match.group(0))
        if not score.is_integer():
            return None
        criterion_rows.append({"score": int(score)})
    try:
        return calculate_overall(criterion_rows)
    except ExaminerResultError:
        return None


def draft_training_focus(scores: dict[str, float | None]) -> list[str]:
    """Choose one or two practical priorities from the lowest criteria."""
    guidance = {
        "Task Response": "加强论证深度，补充具体解释或例子，避免观点停留在概括层面。",
        "Coherence & Cohesion": "优化主题句、段落衔接和逻辑推进，让每段围绕一个中心展开。",
        "Lexical Resource": "减少重复词，优先使用准确、自然的学术表达和搭配。",
        "Grammar Range & Accuracy": "增加可控的句式变化，同时检查语法、标点和从句准确性。",
    }
    available = [
        (label, score)
        for label, score in scores.items()
        if label != "Overall Band" and score is not None
    ]
    available.sort(key=lambda item: (item[1], list(guidance).index(item[0])))
    return [f"**{label}：** {guidance[label]}" for label, _ in available[:2]]


def render_score_change(
    draft_1_scores: dict[str, float | None],
    draft_2_scores: dict[str, float | None],
) -> None:
    """Show compact Draft 1 to Draft 2 score changes."""
    st.subheader("Overall 与四项变化")
    for label in draft_1_scores:
        before = draft_1_scores.get(label)
        after = draft_2_scores.get(label)
        if label == "Overall Band":
            before_text = format_overall_band(before)
            after_text = format_overall_band(after)
        else:
            before_text = f"{before:.0f}" if before is not None else "-"
            after_text = f"{after:.0f}" if after is not None else "-"
        st.write(f"**{SCORE_DISPLAY_NAMES.get(label, label)}：** {before_text} → {after_text}")


def render_draft_2_training(
    *,
    provider: str,
    model: str,
    task_type: str,
    user_id: str,
    cloud_store: SupabaseStore | None = None,
    cloud_user: CloudUser | None = None,
) -> None:
    """Render and process the complete Draft 2 learning cycle."""
    draft_1 = st.session_state.get("draft_1_snapshot")
    if not isinstance(draft_1, dict):
        st.info("请先完成第一稿批改。")
        return

    st.subheader("第二稿训练")
    with st.expander("查看第一稿", expanded=False):
        st.write(draft_1["text"])

    st.markdown("#### 第一稿简要结果")
    score_columns = st.columns(5)
    short_labels = ["Overall", "TR", "CC", "LR", "GRA"]
    for column, short_label, score in zip(
        score_columns,
        short_labels,
        draft_1["scores"].values(),
        strict=False,
    ):
        value = (
            format_overall_band(score)
            if short_label == "Overall"
            else (f"{score:.0f}" if score is not None else "-")
        )
        column.metric(short_label, value)

    st.markdown("#### 本次重写重点")
    for focus in draft_training_focus(draft_1["scores"]):
        st.markdown(f"- {focus}")

    draft_2_text = st.text_area(
        "请根据上方反馈写第二稿",
        height=360,
        key="draft_2_text",
    )
    submit_draft_2 = st.button(
        "提交第二稿",
        type="primary",
        key="submit_draft_2",
        use_container_width=True,
    )

    if submit_draft_2:
        if not draft_2_text.strip():
            st.warning("请先完成第二稿。")
        elif draft_2_text.strip() == draft_1["text"].strip():
            st.warning("第二稿与第一稿完全相同，请根据反馈完成修改后再提交。")
        else:
            with st.spinner("正在评分第二稿并生成两稿对比报告..."):
                render_scoring_loader()
                try:
                    draft_2_package = grade_essay_package(
                        task_type=task_type,
                        topic=draft_1["topic"],
                        essay=draft_2_text,
                    )
                    draft_2_report = str(draft_2_package["report"])
                    draft_2_structured = dict(draft_2_package["structured"])
                    draft_2_scores = score_snapshot(draft_2_structured)
                    progress_report = compare_draft_progress(
                        provider=provider,
                        task_question=draft_1["topic"],
                        draft_1_text=draft_1["text"],
                        draft_1_scores=draft_1["scores"],
                        draft_2_text=draft_2_text,
                        draft_2_scores=draft_2_scores,
                        model=model,
                    )
                    save_markdown_record(
                        task_type=task_type,
                        topic=draft_1["topic"],
                        essay=draft_2_text,
                        report=draft_2_report,
                        word_count=count_words(draft_2_text),
                        user_id=user_id,
                        examiner_data=draft_2_structured,
                        grading_metadata={
                            "model": draft_2_package["model"],
                            "prompt_version": draft_2_package["prompt_version"],
                            "skill_version": draft_2_package["skill_version"],
                            "schema_version": draft_2_package["schema_version"],
                            "graded_at": draft_2_package["graded_at"],
                        },
                    )
                    training_path = save_draft_training_record(
                        user_id=user_id,
                        task_question=draft_1["topic"],
                        draft_1_text=draft_1["text"],
                        draft_1_scores=draft_1["scores"],
                        draft_1_feedback=draft_1["feedback"],
                        draft_2_text=draft_2_text,
                        draft_2_scores=draft_2_scores,
                        draft_2_feedback=draft_2_report,
                        progress_report=progress_report,
                    )
                    record_grading_event(
                        user_id=user_id,
                        overall_band=draft_2_scores["Overall Band"],
                        essay_word_count=count_words(draft_2_text),
                        model_name=model,
                    )
                    if cloud_store and cloud_user and draft_1.get("essay_id") and draft_1.get("grading_run_id"):
                        try:
                            cloud_store.save_draft_revision(
                                cloud_user,
                                essay_id=str(draft_1["essay_id"]),
                                grading_run_id=str(draft_1["grading_run_id"]),
                                content=draft_2_text,
                                scores=draft_2_scores,
                                report_json=draft_2_structured,
                                report_markdown=draft_2_report,
                                progress_report=progress_report,
                            )
                        except CloudStoreError as exc:
                            st.warning(f"第二稿已保存在本机，但云端同步失败：{exc}")
                    st.session_state.draft_2_result = {
                        "scores": draft_2_scores,
                        "report": draft_2_report,
                        "progress_report": progress_report,
                        "path": training_path,
                        "text": draft_2_text,
                    }
                except AIGraderError as exc:
                    st.error("第二稿评分失败。完整诊断信息如下。")
                    st.code(str(exc), language="text")
                except Exception as exc:
                    st.error("第二稿训练出现意外错误。")
                    st.code(f"{type(exc).__name__}: {exc}", language="text")

    result = st.session_state.get("draft_2_result")
    if isinstance(result, dict):
        st.divider()
        st.header("两稿对比进步报告")
        render_training_stepper(active=5)
        render_score_change(draft_1["scores"], result["scores"])
        revised_text = str(result.get("text") or "")
        if revised_text:
            st.subheader("真实文本变化")
            st.caption("删除内容以柔和红色标记，新增内容以青绿色标记；文本仍可直接复制。")
            render_text_diff(str(draft_1["text"]), revised_text)
        st.markdown(result["progress_report"])
        with st.expander("查看第二稿完整评分", expanded=False):
            st.markdown(result["report"])


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
    """Render the program-calculated point Overall for the learner."""
    score_text = html.escape(format_overall_band(score))
    st.markdown(
        f"""
        <div class="ep-overall-card">
            <div class="ep-overall-card__label">雅思写作练习 Overall</div>
            <div class="ep-overall-card__value">{score_text}</div>
            <div class="ep-overall-card__note">AI 练习估分，不是 IELTS 官方成绩；四项整数分可继续查看</div>
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
                render_score_card(label, detail.get("score", "-"), "点击下方查看详情")
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


def render_structured_criteria_overview(data: dict[str, object]) -> None:
    """直接使用结构化评分结果展示四项评分，不解析报告标题。"""
    criteria = [item for item in data.get("criteria", []) if isinstance(item, dict)]
    for row_start in range(0, len(criteria), 2):
        columns = st.columns(2)
        for column, item in zip(columns, criteria[row_start : row_start + 2], strict=False):
            label = CRITERION_DISPLAY_NAMES.get(str(item.get("criterion", "")), str(item.get("criterion", "")))
            with column:
                render_score_card(label, str(item.get("score", "-")), "点击下方查看评分依据")
                with st.expander("查看详情", expanded=False):
                    st.markdown(f"**评分说明：** {item.get('reason', '暂无说明。')}")
                    for evidence in item.get("evidence", [])[:2]:
                        st.info(f"原文依据：{evidence}")
                    st.warning(f"下一档限制：{item.get('next_band_limit', '暂无说明。')}")


def render_problem_cards(markdown: str) -> None:
    """Render main problems as warning cards."""
    st.subheader("主要问题")
    problems = extract_bullets(extract_report_section(markdown, 4), limit=5)
    if not problems:
        st.info("这份报告暂未提取到主要问题。")
        return

    for problem in problems:
        st.warning(problem)


def render_suggestion_cards(markdown: str) -> None:
    """Render improvement suggestions as success cards."""
    st.subheader("提分建议")
    suggestions = extract_bullets(extract_report_section(markdown, 3), limit=5)
    if not suggestions:
        suggestions = extract_bullets(extract_report_section(markdown, 10), limit=3)
    if not suggestions:
        st.info("这份报告暂未提取到提分建议。")
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
    report = re.sub(
        r"^#\s*(?:IELTS Writing Examiner Report|雅思写作批改报告|雅思写作练习估分与反馈)\s*",
        "",
        report,
    ).strip()
    return report


def render_grouped_examiner_report(markdown: str) -> None:
    """Render the static examiner report as focused learning sections."""
    report = report_before_interactive_practice(markdown)
    groups = [
        ("评分依据", "report-basis", (1, 2)),
        ("核心提分方向", "report-priorities", (3, 4)),
        ("逐句与段落批改", "report-corrections", (5, 6)),
        ("Band 7.5 示范改写", "report-rewrite", (7,)),
        ("表达积累与下一步", "report-next", (8, 9)),
    ]

    rendered_any = False
    for label, anchor_id, section_numbers in groups:
        sections = [
            extract_report_section(report, number)
            for number in section_numbers
        ]
        content = "\n\n".join(section for section in sections if section)
        if not content:
            continue
        rendered_any = True
        render_anchor(anchor_id)
        with st.expander(label, expanded=False):
            st.markdown(content)

    if not rendered_any:
        with st.expander("完整评分报告", expanded=False):
            st.markdown(report)


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
    cloud_store: SupabaseStore | None = None,
    cloud_user: CloudUser | None = None,
    grading_run_id: str = "",
    error_tags: list[str] | None = None,
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
        revision_key = f"sentence_revision_{sentence_id}"
        mastered_key = f"sentence_mastered_{sentence_id}"
        saved_key = f"sentence_saved_{sentence_id}"

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
                            if cloud_store and cloud_user and grading_run_id:
                                cloud_store.save_practice_attempt(
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    task_kind="sentence",
                                    task_index=index,
                                    original_text=original_sentence,
                                    submitted_text=rewrite,
                                    feedback=st.session_state[feedback_key],
                                    error_tags=error_tags,
                                )
                                sync_learning_item_status(
                                    cloud_store,
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    source_text=original_sentence,
                                    mastered=False,
                                )
                                st.session_state[saved_key] = True
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
                st.markdown("**再改一次：** 根据点评写出你的最终版本。")
                revision = st.text_area(
                    "第二次改写",
                    key=revision_key,
                    height=90,
                    placeholder="吸收点评后再写一次，完成后标记掌握。",
                )
                if st.button("标记为已掌握", key=mastered_key, use_container_width=True):
                    if not revision.strip():
                        st.warning("请先完成第二次改写。")
                    elif revision.strip() == rewrite.strip():
                        st.warning("第二次改写需要体现你根据点评做出的调整。")
                    else:
                        if cloud_store and cloud_user and grading_run_id:
                            try:
                                cloud_store.save_practice_attempt(
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    task_kind="sentence",
                                    task_index=index,
                                    original_text=original_sentence,
                                    submitted_text=rewrite,
                                    feedback=st.session_state[feedback_key],
                                    revision_text=revision,
                                    mastered=True,
                                    error_tags=error_tags,
                                )
                                sync_learning_item_status(
                                    cloud_store,
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    source_text=original_sentence,
                                    mastered=True,
                                )
                            except CloudStoreError as exc:
                                st.warning(f"已完成练习，但云端同步失败：{exc}")
                        st.success("已掌握。本次改写会计入你的学习档案。")


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
    cloud_store: SupabaseStore | None = None,
    cloud_user: CloudUser | None = None,
    grading_run_id: str = "",
    error_tags: list[str] | None = None,
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
        revision_key = f"logic_revision_{logic_id}"
        mastered_key = f"logic_mastered_{logic_id}"

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
                            if cloud_store and cloud_user and grading_run_id:
                                cloud_store.save_practice_attempt(
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    task_kind="logic",
                                    task_index=index,
                                    original_text=task["original"],
                                    submitted_text=rewrite,
                                    feedback=st.session_state[feedback_key],
                                    error_tags=error_tags,
                                )
                                sync_learning_item_status(
                                    cloud_store,
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    source_text=task["original"],
                                    mastered=False,
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
                st.markdown("**再写一次：** 把点评落实到完整的论点—解释—例子链条。")
                revision = st.text_area(
                    "第二次重写",
                    key=revision_key,
                    height=130,
                    placeholder="根据点评重写最终版本。",
                )
                if st.button("标记逻辑训练为已掌握", key=mastered_key, use_container_width=True):
                    if not revision.strip():
                        st.warning("请先完成第二次重写。")
                    elif revision.strip() == rewrite.strip():
                        st.warning("第二次重写需要体现点评后的调整。")
                    else:
                        if cloud_store and cloud_user and grading_run_id:
                            try:
                                cloud_store.save_practice_attempt(
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    task_kind="logic",
                                    task_index=index,
                                    original_text=task["original"],
                                    submitted_text=rewrite,
                                    feedback=st.session_state[feedback_key],
                                    revision_text=revision,
                                    mastered=True,
                                    error_tags=error_tags,
                                )
                                sync_learning_item_status(
                                    cloud_store,
                                    cloud_user,
                                    grading_run_id=grading_run_id,
                                    source_text=task["original"],
                                    mastered=True,
                                )
                            except CloudStoreError as exc:
                                st.warning(f"已完成练习，但云端同步失败：{exc}")
                        st.success("已掌握。这次逻辑重写已加入学习档案。")


def list_correction_history(user_id: str) -> list[dict[str, object]]:
    """Read saved records for the dashboard trend chart."""
    records_dir = BASE_DIR / "records" / user_id
    if not records_dir.exists():
        return []

    history: list[dict[str, object]] = []
    for path in sorted(records_dir.glob("ielts_*.md")):
        markdown = path.read_text(encoding="utf-8")
        created_match = re.search(r"- (?:创建时间|Created At):\s*(.+)", markdown)
        task_match = re.search(r"- (?:任务类型|Task Type):\s*(.+)", markdown)
        words_match = re.search(r"- Word Count:\s*(\d+)", markdown)

        score = calculate_overall_band(markdown)
        json_path = path.with_suffix(".json")
        if score is None and json_path.exists():
            try:
                metadata = json.loads(json_path.read_text(encoding="utf-8"))
                stored_score = metadata.get("overall_band") if isinstance(metadata, dict) else None
                score = float(stored_score) if isinstance(stored_score, (int, float)) else None
            except (OSError, ValueError, json.JSONDecodeError):
                score = None
        history.append(
            {
                "file": path.name,
                "path": path,
                "created_at": created_match.group(1) if created_match else path.stem,
                "task_type": task_match.group(1) if task_match else "未知",
                "word_count": int(words_match.group(1)) if words_match else None,
                "score": score,
            }
        )

    return history


def render_history(user_id: str) -> None:
    """Render local history with the point Overall estimate."""
    history = list_correction_history(user_id)
    scored_history = [item for item in history if item["score"] is not None]
    training_history = list_draft_training_history(user_id)

    st.subheader("历史练习估分")
    if not scored_history:
        st.info("还没有评分记录。完成一次批改后，这里会显示你的分数趋势。")
    else:
        for item in reversed(scored_history[-10:]):
            st.caption(
                f"{item['created_at']} · Overall "
                f"{format_overall_band(item['score'])}"
            )

    if training_history:
        st.subheader("第二稿训练历史")
        for record in reversed(training_history[-5:]):
            draft_1_score = record.get("draft_1_scores", {}).get("Overall Band")
            draft_2_score = record.get("draft_2_scores", {}).get("Overall Band")
            before = format_overall_band(draft_1_score) if isinstance(draft_1_score, (int, float)) else "-"
            after = format_overall_band(draft_2_score) if isinstance(draft_2_score, (int, float)) else "-"
            with st.expander(
                f"第一稿 → 第二稿 · Overall：{before} → {after}",
                expanded=False,
            ):
                st.caption(str(record.get("timestamp", "")))
                st.markdown(str(record.get("progress_report", "")))


def render_learning_dashboard(store: SupabaseStore, user: CloudUser) -> None:
    """Render cloud-backed continuity, priorities, and multidimensional progress."""
    try:
        runs = store.list_grading_runs(user)
        pending = store.list_pending_practice(user)
        revisions = store.list_draft_revisions(user)
    except CloudStoreError as exc:
        st.warning(f"云端学习档案暂时不可用：{exc}")
        return

    render_anchor("learning-dashboard")
    st.markdown('<div class="section-kicker">学习档案</div>', unsafe_allow_html=True)
    st.subheader("今天从最需要提高的地方继续")
    try:
        learning_items = store.list_learning_items(user)
    except (CloudStoreError, AttributeError):
        learning_items = []
    expression_items = [item for item in learning_items if item.get("item_type") == "expression"]
    mastered_expressions = [item for item in expression_items if item.get("status") == "mastered"]
    topic_counts = Counter(str(item.get("topic_category") or "society_family") for item in expression_items)
    focus_topic = TOPIC_LABELS.get(topic_counts.most_common(1)[0][0], "尚未形成") if topic_counts else "尚未形成"
    render_dashboard_stats(
        [
            ("已积累表达", len(expression_items), "来自批改与收藏"),
            ("已掌握表达", len(mastered_expressions), "已通过表达练习"),
            ("当前重点题材", focus_topic, "继续巩固高频表达"),
        ],
        columns=3,
    )
    if expression_items and st.button("继续表达练习", use_container_width=True):
        pending_expression = next(
            (item for item in expression_items if item.get("status") != "mastered"), expression_items[0]
        )
        st.session_state.expression_practice_item = _normalise_expression(pending_expression)
        st.session_state.expression_library_view = "表达练习"
        navigate("growth")
        st.rerun()
    if not runs:
        st.info("你可以先浏览 150 条题材表达；完成第一篇 Task 2 批改后，这里还会出现分数、薄弱项和待完成训练。")
        return

    latest = runs[0]
    previous = runs[1] if len(runs) > 1 else None
    latest_score = float(latest.get("overall_band") or 0)
    previous_score = float(previous.get("overall_band") or 0) if previous else None
    criteria = latest.get("criteria") if isinstance(latest.get("criteria"), list) else []
    ranked = sorted(
        [item for item in criteria if isinstance(item, dict) and isinstance(item.get("score"), (int, float))],
        key=lambda item: (float(item["score"]), str(item.get("criterion", ""))),
    )
    weakest = (
        CRITERION_COMPACT_NAMES.get(str(ranked[0].get("criterion")), str(ranked[0].get("criterion")))
        if ranked
        else "等待评分"
    )
    next_weakest = (
        CRITERION_COMPACT_NAMES.get(str(ranked[1].get("criterion")), str(ranked[1].get("criterion")))
        if len(ranked) > 1
        else ""
    )
    delta = latest_score - previous_score if previous_score is not None else None
    latest_revision_gain: float | None = None
    if revisions:
        revision_scores = revisions[0].get("score_snapshot") or {}
        first_run = revisions[0].get("grading_runs") or {}
        if isinstance(revision_scores, dict) and isinstance(first_run, dict):
            revised = revision_scores.get("Overall Band")
            original = first_run.get("overall_band")
            if isinstance(revised, (int, float)) and isinstance(original, (int, float)):
                latest_revision_gain = float(revised) - float(original)
    render_dashboard_stats(
        [
            ("最新 Overall", format_overall_band(latest_score), "IELTS Task 2"),
            ("当前薄弱项", weakest, f"下一优先：{next_weakest}" if next_weakest else "根据最新批改"),
            ("较上一次", "已有新记录" if delta is not None else "暂无对比", "这是首篇记录" if delta is None else "请结合四项分观察变化"),
            ("待完成训练", len(pending), "单句与逻辑任务"),
            (
                "最近第二稿提升",
                "已完成验证" if latest_revision_gain is not None else "暂无",
                "提交第二稿后显示" if latest_revision_gain is None else "与第一稿对比",
            ),
        ],
        columns=5,
    )

    essay_data = latest.get("essays") if isinstance(latest.get("essays"), dict) else {}
    latest_is_legacy = str(latest.get("prompt_version") or "") != REPORT_PROMPT_VERSION
    if latest_is_legacy:
        st.info("最近一份是旧版英文报告。它会继续保留，不会自动消耗 Token 重新生成。")
    if st.button("继续上一次训练", type="primary", use_container_width=True):
        st.session_state.latest_report = str(latest.get("report_markdown", ""))
        st.session_state.latest_structured = latest.get("report_json") or {}
        st.session_state.latest_prompt_version = str(latest.get("prompt_version") or "")
        st.session_state.latest_cloud_ids = {
            "essay_id": str(latest.get("essay_id", "")),
            "grading_run_id": str(latest.get("id", "")),
        }
        st.session_state.topic_input = str(essay_data.get("question", ""))
        st.session_state.essay_input = str(essay_data.get("content", ""))
        st.session_state.draft_1_snapshot = {
            "topic": st.session_state.topic_input,
            "text": st.session_state.essay_input,
            "feedback": st.session_state.latest_report,
            "scores": score_snapshot(st.session_state.latest_structured),
            "structured": st.session_state.latest_structured,
            "essay_id": str(latest.get("essay_id", "")),
            "grading_run_id": str(latest.get("id", "")),
        }
        navigate("training", str(latest.get("id", "")))
        st.rerun()

    if latest_is_legacy and st.button("将旧作文载入输入区，准备生成中文报告", use_container_width=True):
        st.session_state.topic_input = str(essay_data.get("question", ""))
        st.session_state.essay_input = str(essay_data.get("content", ""))
        st.session_state.latest_report = ""
        st.session_state.latest_structured = {}
        navigate("write")
        st.rerun()

    chart_rows: list[dict[str, object]] = []
    tag_counts: Counter[str] = Counter()
    for run in reversed(runs):
        created = str(run.get("created_at", ""))[:10]
        for item in run.get("criteria") or []:
            if isinstance(item, dict):
                chart_rows.append({
                    "练习日期": created,
                    "能力维度": CRITERION_DISPLAY_NAMES.get(str(item.get("criterion")), str(item.get("criterion"))),
                    "分数": item.get("score"),
                })
        report_json = run.get("report_json") or {}
        if isinstance(report_json, dict):
            tag_counts.update(str(tag) for tag in report_json.get("error_tags", []))
    if chart_rows:
        chart = (
            alt.Chart(pd.DataFrame(chart_rows))
            .mark_line(point=True)
            .encode(
                x=alt.X("练习日期:N", title="练习日期"),
                y=alt.Y("分数:Q", scale=alt.Scale(domain=[3, 9]), title="分数"),
                color=alt.Color(
                    "能力维度:N",
                    title="能力维度",
                    scale=alt.Scale(range=ALPINE_CHART_COLORS),
                ),
                tooltip=["练习日期", "能力维度", "分数"],
            )
            .properties(height=280)
        )
        st.altair_chart(chart, use_container_width=True)

    structured = latest.get("report_json") if isinstance(latest.get("report_json"), dict) else {}
    sentence_tasks = structured.get("sentence_training", []) if isinstance(structured, dict) else []
    logic_tasks = structured.get("logic_training", []) if isinstance(structured, dict) else []
    with st.expander("今日训练", expanded=True):
        for item in sentence_tasks[:2]:
            st.markdown(f"- **单句：** {item.get('goal', '改写薄弱句子')} — “{item.get('original', '')}”")
        for item in logic_tasks[:1]:
            st.markdown(f"- **逻辑：** {item.get('task', '完成段落重写')}")
    if tag_counts:
        common = " · ".join(f"{tag} × {count}" for tag, count in tag_counts.most_common(5))
        st.caption(f"近期常见问题：{common}")
    st.divider()


def render_product_hero(is_demo: bool = False) -> None:
    """Render the shared Alpine photographic hero."""
    render_alpine_hero(variant="demo" if is_demo else "home")


def render_demo_page() -> None:
    """Render a complete static walkthrough without any AI request."""
    try:
        report = DEMO_REPORT_PATH.read_text(encoding="utf-8")
    except OSError:
        st.error("示范报告文件缺失，请返回批改页。")
        return

    render_anchor("demo-top")
    apply_pending_scroll()
    render_bookmark_rail(
        [
            ("流程", "demo-flow"),
            ("原稿", "demo-input"),
            ("评分", "demo-score"),
            ("诊断", "demo-diagnosis"),
            ("改写", "demo-rewrite"),
            ("训练", "demo-practice"),
            ("二稿", "demo-draft2"),
        ]
    )
    render_product_hero(is_demo=True)

    back_column, fill_column, spacer_column = st.columns([1.15, 1.45, 3.4])
    with back_column:
        st.button(
            "← 返回批改页",
            on_click=show_workspace,
            use_container_width=True,
            key="demo_back_top",
        )
    with fill_column:
        st.button(
            "把范文填入输入区",
            on_click=load_sample_and_show_workspace,
            use_container_width=True,
            key="demo_fill_top",
        )

    st.caption("这是零 Token 静态范文。按正式产品的四个阶段浏览，不会调用模型。")
    input_tab, report_tab, training_tab, draft_tab = st.tabs(
        ["① 输入", "② 报告", "③ 训练", "④ 第二稿"]
    )
    with input_tab:
        st.subheader("题目与学生原稿")
        question_column, essay_column = st.columns([0.82, 1.38], gap="large")
        with question_column:
            with st.container(border=True):
                st.markdown("**英文作文题目**")
                st.write(SAMPLE_TOPIC)
                st.caption("Task 2 · 双边讨论并给出观点")
        with essay_column:
            with st.container(border=True):
                st.markdown("**学生原稿 · 239 词**")
                st.write(SAMPLE_ESSAY)
    with report_tab:
        st.subheader("评分、诊断与改写")
        score_columns = st.columns(5)
        demo_scores = [("Overall", "7.0"), ("TR", "7"), ("CC", "7"), ("LR", "6"), ("GRA", "7")]
        for column, (label, value) in zip(score_columns, demo_scores):
            with column:
                render_score_card(label, value, "静态示范")
        overview_tab, diagnosis_tab, rewrite_tab = st.tabs(["评分依据", "核心诊断", "逐句与范文"])
        with overview_tab:
            st.markdown(extract_report_section(report, 2))
        with diagnosis_tab:
            st.markdown(extract_report_section(report, 4))
        with rewrite_tab:
            st.markdown(extract_report_section(report, 5))
            st.markdown(extract_report_section(report, 7))
    with training_tab:
        st.subheader("从报告进入专项训练")
        sentence_tab, logic_tab, expression_tab = st.tabs(["单句训练", "逻辑训练", "表达积累"])
        with sentence_tab:
            st.markdown(extract_report_section(report, 11))
        with logic_tab:
            st.markdown(extract_report_section(report, 12))
        with expression_tab:
            st.markdown(extract_report_section(report, 8))
    with draft_tab:
        st.subheader("第二稿训练与前后对比")
        st.caption("先由学生独立修改，再用 Band 7.5 示范稿核对，不让模型替写。")
        compare_draft_1, compare_draft_2, compare_result = st.tabs(["第一稿", "第二稿示范", "两稿变化"])
        with compare_draft_1:
            st.markdown(SAMPLE_ESSAY)
        with compare_draft_2:
            st.markdown(extract_report_section(report, 7))
        with compare_result:
            st.markdown(
                """
                - **保留：** 原来的立场和四段核心结构。
                - **已改善：** 补足解释链，减少重复用词，让反方段落回到中心论点。
                - **下一步：** 将仍未稳定的 LR 问题收入错题本，继续完成单句训练。
                """
            )
    st.success("完整示范已按输入 → 报告 → 训练 → 第二稿拆分；浏览全过程不消耗 Token。")
    return

    render_anchor("demo-flow")
    st.markdown('<div class="section-kicker">完整学习流程</div>', unsafe_allow_html=True)
    st.subheader("一眼看懂完整批改流程")
    st.markdown(
        """
        <div class="feature-strip">
            <div class="feature-chip"><strong>01 · 诊断</strong>先看分数与证据，不先堆修改建议</div>
            <div class="feature-chip"><strong>02 · 对照</strong>用原句与改写对照，看见真实差距</div>
            <div class="feature-chip"><strong>03 · 练习</strong>把问题变成下一次可以完成的练习</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    render_anchor("demo-input")
    st.markdown('<div class="demo-step">第 1 步 · 查看范文输入</div>', unsafe_allow_html=True)
    st.subheader("先看原始题目与学生作文")
    question_column, essay_column = st.columns([0.82, 1.38], gap="large")
    with question_column:
        with st.container(border=True):
            st.markdown("**英文作文题目**")
            st.write(SAMPLE_TOPIC)
            st.caption("Task 2 · 双边讨论并给出观点")
    with essay_column:
        with st.container(border=True):
            st.markdown("**学生原稿 · 239 词**")
            st.write(SAMPLE_ESSAY)

    render_anchor("demo-score")
    st.divider()
    st.markdown('<div class="demo-step">第 2 步 · 结合证据评分</div>', unsafe_allow_html=True)
    st.subheader("分数先给结论，再给可核对的依据")
    render_overall_band(calculate_overall_band(report))
    st.markdown(extract_report_section(report, 2))

    render_anchor("demo-diagnosis")
    st.divider()
    st.markdown('<div class="demo-step">第 3 步 · 找出优先问题</div>', unsafe_allow_html=True)
    st.subheader("只抓最影响提分的问题")
    priorities_column, problems_column = st.columns(2, gap="large")
    with priorities_column:
        st.markdown(extract_report_section(report, 3))
    with problems_column:
        st.markdown(extract_report_section(report, 4))

    st.markdown('<div class="demo-step">第 4 步 · 逐句与段落批改</div>', unsafe_allow_html=True)
    st.subheader("从句子到段落，逐层看哪里出了问题")
    st.markdown(extract_report_section(report, 5))
    st.markdown(extract_report_section(report, 6))

    render_anchor("demo-rewrite")
    st.divider()
    st.markdown('<div class="demo-step">第 5 步 · 对照英文示范</div>', unsafe_allow_html=True)
    st.subheader("Band 7.5 示范改写")
    st.caption("保留学生原始立场，只升级论证、搭配和句型控制。")
    with st.container(border=True):
        st.markdown(extract_report_section(report, 7))

    render_anchor("demo-practice")
    st.divider()
    st.markdown('<div class="demo-step">第 6 步 · 把反馈变成训练</div>', unsafe_allow_html=True)
    st.subheader("表达积累与下一次训练")
    st.markdown(extract_report_section(report, 8))
    st.markdown(extract_report_section(report, 9))
    practice_column, logic_column = st.columns(2, gap="large")
    with practice_column:
        with st.container(border=True):
            st.markdown(extract_report_section(report, 11))
    with logic_column:
        with st.container(border=True):
            st.markdown(extract_report_section(report, 12))

    render_anchor("demo-draft2")
    st.divider()
    st.markdown('<div class="demo-step">第 7 步 · 完成第二稿闭环</div>', unsafe_allow_html=True)
    st.subheader("第二稿训练：把反馈真正写进自己的作文")
    st.caption("示范报告来自 gpt-5.4-mini；第二稿环节展示用户如何消化反馈，而不是让模型再代写一篇。")
    st.markdown(
        """
        <div class="feature-strip">
            <div class="feature-chip"><strong>第一稿基线 · 7.0</strong>先保留原文、四项分数和完整反馈</div>
            <div class="feature-chip"><strong>本轮重点 · LR 6</strong>减少 study / subjects / learn 重复，换成准确搭配</div>
            <div class="feature-chip"><strong>提交第二稿</strong>对比两稿分数、已改善问题、剩余问题和下一轮重点</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.container(border=True):
        st.markdown(
            """
            #### 完整训练顺序

            1. 展开第一稿，确认原始作文和四项分数。
            2. 按系统给出的两个最低项制定本轮修改重点。
            3. 学生独立重写整篇第二稿；第 7 部分的 Band 7.5 改写只作为完成后的参考。
            4. 提交第二稿后，系统逐项显示 **第一稿 → 第二稿** 的分数变化。
            5. 系统给出“已经改善 / 仍需修改 / 下一轮优先级”的进步报告，并保存训练记录。
            """
        )
    compare_draft_1, compare_draft_2, compare_result = st.tabs(
        ["第一稿", "第二稿示范", "两稿变化"]
    )
    with compare_draft_1:
        st.markdown(SAMPLE_ESSAY)
    with compare_draft_2:
        st.markdown(extract_report_section(report, 7))
    with compare_result:
        st.markdown(
            """
            - **保留：** 原来的立场和四段核心结构，不替学生更换观点。
            - **重点变化：** 补足解释链，减少 `study / subjects / learn` 重复，并让反方段落结尾回到中心论点。
            - **训练目标：** 学生先独立完成第二稿，再把示范稿当作核对材料；系统比较的是两次真实写作表现。
            """
        )
    st.info("正式使用时，需要先完成第一稿批改，再点击“开始第二稿训练”。示范页只展示流程，因此仍然是 0 Token。")

    st.success("这就是一次完整批改会经历的全部步骤。查看本页不会调用任何模型。")
    bottom_back, bottom_fill = st.columns(2)
    with bottom_back:
        st.button(
            "返回主页",
            on_click=show_workspace,
            use_container_width=True,
            key="demo_back_bottom",
        )
    with bottom_fill:
        st.button(
            "用这篇范文开始练习",
            on_click=load_sample_and_show_workspace,
            use_container_width=True,
            key="demo_fill_bottom",
        )


APP_ROUTES = {
    "home": "学习首页",
    "write": "写作批改",
    "report": "批改报告",
    "training": "专项训练",
    "growth": "错题本与成长",
}


def navigate(route: str, run_id: str = "") -> None:
    """Switch the visible product page and preserve a shareable run context."""
    route = route if route in APP_ROUTES else "home"
    st.session_state.page_mode = route
    st.query_params["page"] = route
    if run_id:
        st.session_state.active_run_id = run_id
        st.query_params["run_id"] = run_id
    elif route == "write":
        st.query_params.pop("run_id", None)


def hydrate_grading_run(run: dict[str, object]) -> None:
    """Make a cloud record the current cross-page learning context."""
    essay_data = run.get("essays") if isinstance(run.get("essays"), dict) else {}
    structured = run.get("report_json") if isinstance(run.get("report_json"), dict) else {}
    report = str(run.get("report_markdown") or "")
    run_id = str(run.get("id") or "")
    essay_id = str(run.get("essay_id") or "")
    topic = str(essay_data.get("question") or "")
    essay = str(essay_data.get("content") or "")
    st.session_state.latest_report = report
    st.session_state.latest_structured = structured
    st.session_state.latest_prompt_version = str(run.get("prompt_version") or "")
    st.session_state.latest_cloud_ids = {"essay_id": essay_id, "grading_run_id": run_id}
    st.session_state.active_run_id = run_id
    st.session_state.topic_input = topic
    st.session_state.essay_input = essay
    if structured:
        st.session_state.draft_1_snapshot = {
            "topic": topic,
            "text": essay,
            "feedback": report,
            "scores": score_snapshot(structured),
            "structured": structured,
            "essay_id": essay_id,
            "grading_run_id": run_id,
        }


def ensure_run_context(store: SupabaseStore, user: CloudUser | None) -> None:
    requested = str(st.query_params.get("run_id", "") or "")
    current = str(st.session_state.get("active_run_id", "") or "")
    if not requested or requested == current or user is None:
        return
    try:
        run = store.get_grading_run(user, requested)
    except CloudStoreError as exc:
        st.warning(f"暂时无法恢复这份批改记录：{exc}")
        return
    if run:
        hydrate_grading_run(run)


def ensure_learning_assets(store: SupabaseStore, user: CloudUser | None) -> None:
    structured = st.session_state.get("latest_structured")
    run_id = str(st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", ""))
    if user is None or not run_id or not isinstance(structured, dict) or not structured:
        return
    rows = build_learning_items(
        structured, user_id=user.id, grading_run_id=run_id,
        question=str(st.session_state.get("topic_input") or ""),
    )
    try:
        store.upsert_learning_items(user, rows)
        st.session_state.learning_assets_ready = True
    except (CloudStoreError, AttributeError):
        st.session_state.learning_assets_ready = False


def render_app_navigation(user: CloudUser | None, *, cloud_enabled: bool) -> None:
    """Render the persistent product-level navigation instead of a long-page index."""
    with st.sidebar:
        st.markdown("## EssayPilot")
        st.caption("Task 2 学习工作台")
        active = str(st.session_state.get("page_mode", "home"))
        for route, label in APP_ROUTES.items():
            prefix = "● " if route == active else ""
            st.button(
                f"{prefix}{label}",
                key=f"nav_{route}",
                use_container_width=True,
                on_click=navigate,
                args=(route,),
            )
        st.divider()
        st.caption(f"固定评分模型 · {PRODUCTION_MODEL}")
        if user is not None:
            st.caption(f"已登录：{user.email}")
            st.button("退出登录", on_click=logout_cloud_user, use_container_width=True)
        else:
            st.caption("本地开发模式")
    with st.container(key="mobile_account_bar", border=True):
        if user is not None:
            st.caption(f"已登录：{user.email}")
            st.button(
                "退出登录",
                key="mobile_logout",
                on_click=logout_cloud_user,
                use_container_width=True,
            )
        elif cloud_enabled:
            st.caption("当前为访客浏览；登录后可跨设备同步批改、训练和成长记录。")
            st.button(
                "登录并同步进度",
                key="mobile_login",
                type="primary",
                on_click=open_cloud_login,
                use_container_width=True,
            )
        else:
            st.caption("本地开发模式")
    active = str(st.session_state.get("page_mode", "home"))
    short_labels = {"home": "首页", "write": "写作", "report": "报告", "training": "训练", "growth": "成长"}
    run_id = str(st.session_state.get("active_run_id") or st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", ""))
    links: list[str] = []
    for route in APP_ROUTES:
        query = f"?page={route}" + (f"&run_id={html.escape(run_id)}" if run_id else "")
        active_class = " active" if route == active else ""
        links.append(f'<a class="{active_class.strip()}" href="{query}">{short_labels[route]}</a>')
    st.markdown(f'<nav class="mobile-product-nav">{"".join(links)}</nav>', unsafe_allow_html=True)


def _run_priority(structured: dict[str, object]) -> str:
    priorities = structured.get("priorities") if isinstance(structured, dict) else []
    if isinstance(priorities, list) and priorities and isinstance(priorities[0], dict):
        return str(priorities[0].get("title") or priorities[0].get("action") or "")
    return "继续完成本轮专项训练"


def render_home_page(store: SupabaseStore, user: CloudUser | None) -> None:
    render_product_hero()
    action_start, action_demo, _ = st.columns([1.2, 1.4, 3.2])
    with action_start:
        st.button("开始一篇新作文", type="primary", use_container_width=True, on_click=navigate, args=("write",))
    with action_demo:
        st.button("查看零 Token 范文", use_container_width=True, on_click=show_demo)
    render_feature_bento()
    if user is not None:
        render_learning_dashboard(store, user)


def grade_submission(
    store: SupabaseStore,
    user: CloudUser | None,
    *,
    topic: str,
    essay: str,
) -> None:
    """Run the existing fixed-model grading workflow and open its report page."""
    word_count = count_words(essay)
    fingerprint = submission_hash(topic, essay)
    grading_cache = st.session_state.setdefault("grading_cache", {})
    cached_entry = grading_cache.get(fingerprint)
    package: dict[str, object] | None = None
    locked_scoring_package: dict[str, object] | None = None
    cloud_ids: dict[str, str] = {}
    reused_result = False
    if isinstance(cached_entry, dict):
        candidate = dict(cached_entry.get("package") or {})
        if candidate.get("prompt_version") == REPORT_PROMPT_VERSION:
            package = candidate
            cloud_ids = dict(cached_entry.get("cloud_ids") or {})
            reused_result = bool(package)
        elif (
            candidate.get("scoring_prompt_version") == SCORING_PROMPT_VERSION
            and candidate.get("skill_version") == SCORING_SKILL_VERSION
            and isinstance(candidate.get("scoring"), dict)
        ):
            locked_scoring_package = {
                "provider": candidate.get("provider") or "OpenAI",
                "model": candidate.get("model") or PRODUCTION_MODEL,
                "response_model": candidate.get("response_model"),
                "system_fingerprint": candidate.get("system_fingerprint"),
                "reasoning_effort": candidate.get("reasoning_effort") or "none",
                "prompt_version": SCORING_PROMPT_VERSION,
                "skill_version": SCORING_SKILL_VERSION,
                "scoring": candidate["scoring"],
                "usage": {},
            }
    if package is None and user is not None:
        try:
            cached_cloud = store.find_cached_grading(user, fingerprint, REPORT_PROMPT_VERSION)
        except CloudStoreError:
            cached_cloud = None
            st.session_state.cloud_cache_warning = True
        if cached_cloud:
            structured_cloud = dict(cached_cloud.get("report_json") or {})
            package = {
                "model": str(cached_cloud.get("model") or PRODUCTION_MODEL),
                "schema_version": str(structured_cloud.get("schema_version") or "2.0"),
                "prompt_version": str(cached_cloud.get("prompt_version") or ""),
                "skill_version": str(cached_cloud.get("skill_version") or ""),
                "graded_at": str(cached_cloud.get("created_at") or ""),
                "structured": structured_cloud,
                "report": str(cached_cloud.get("report_markdown") or ""),
                "usage": {},
            }
            cloud_ids = {
                "essay_id": str(cached_cloud.get("essay_id") or ""),
                "grading_run_id": str(cached_cloud.get("id") or ""),
            }
            reused_result = True
        elif locked_scoring_package is None:
            try:
                cached_score = store.find_cached_scoring(user, fingerprint, SCORING_PROMPT_VERSION)
            except CloudStoreError:
                cached_score = None
            if cached_score:
                cached_json = cached_score.get("report_json") or {}
                if isinstance(cached_json, dict):
                    locked = cached_json.get("locked_scoring_decision")
                    if isinstance(locked, dict):
                        locked_scoring_package = {
                            "provider": "OpenAI",
                            "model": str(cached_score.get("model") or PRODUCTION_MODEL),
                            "prompt_version": SCORING_PROMPT_VERSION,
                            "skill_version": SCORING_SKILL_VERSION,
                            "scoring": locked,
                            "usage": {},
                        }
    if package is None:
        package = grade_essay_package(
            task_type="Task 2",
            topic=topic,
            essay=essay,
            locked_scoring_package=locked_scoring_package,
        )
    report = str(package["report"])
    structured = dict(package["structured"])
    scores = score_snapshot(structured)
    saved_path = save_markdown_record(
        task_type="Task 2",
        topic=topic,
        essay=essay,
        report=report,
        word_count=word_count,
        user_id=user.id if user is not None else st.session_state.user_id,
        parsed_result={"ok": True, "data": {"overall_band": structured["overall_band"], "criteria_scores": {k: v for k, v in scores.items() if k != "Overall Band"}}, "raw": report, "error": ""},
        examiner_data=structured,
        grading_metadata={
            "model": package["model"], "prompt_version": package["prompt_version"],
            "skill_version": package["skill_version"], "schema_version": package["schema_version"],
            "graded_at": package["graded_at"], "usage": package["usage"],
        },
        content_hash=fingerprint,
    )
    error_book_path = append_error_book(
        task_type="Task 2", topic=topic, report=report,
        user_id=user.id if user is not None else st.session_state.user_id,
    )
    if user is not None and not cloud_ids:
        try:
            cloud_ids = store.save_grading_cycle(
                user, question=topic, essay=essay, word_count=word_count,
                package=package, content_hash=fingerprint,
            )
        except CloudStoreError:
            st.session_state.cloud_save_warning = True
    grading_cache[fingerprint] = {"package": package, "cloud_ids": cloud_ids}
    st.session_state.latest_report = report
    st.session_state.latest_structured = structured
    st.session_state.latest_prompt_version = str(package["prompt_version"])
    st.session_state.latest_cloud_ids = cloud_ids
    st.session_state.latest_saved_path = saved_path
    st.session_state.latest_error_book_path = error_book_path
    st.session_state.draft_1_snapshot = {
        "topic": topic, "text": essay, "feedback": report, "scores": scores,
        "structured": structured, "essay_id": cloud_ids.get("essay_id", ""),
        "grading_run_id": cloud_ids.get("grading_run_id", ""),
    }
    st.session_state.draft_2_active = False
    st.session_state.draft_2_result = None
    st.session_state.grading_failed = False
    if reused_result:
        st.session_state.reused_result_notice = True
    record_grading_event(
        user_id=user.id if user is not None else st.session_state.user_id,
        overall_band=float(structured["overall_band"]),
        essay_word_count=word_count,
        model_name=PRODUCTION_MODEL,
    )
    ensure_learning_assets(store, user)
    navigate("report", str(cloud_ids.get("grading_run_id", "")))


def render_write_page(store: SupabaseStore, user: CloudUser | None) -> None:
    st.markdown('<div class="section-kicker">写作批改</div>', unsafe_allow_html=True)
    st.title("提交 IELTS Writing Task 2 作文")
    render_training_stepper(active=1)
    st.caption(f"评分固定使用 {PRODUCTION_MODEL}；失败后保留题目和正文，不切换模型。")
    if st.session_state.pop("cloud_cache_warning", False):
        st.warning("云端历史暂时无法读取，本次仍可继续批改。")
    if st.session_state.pop("cloud_save_warning", False):
        st.warning("报告已保存在当前设备，但云端同步暂时失败；请稍后重试。")
    with st.container(key="essay_editor"):
        st.markdown(
            '<div class="ep-editor-note"><span>Task 2 · 题目与正文保持原始段落</span>'
            '<span>唯一主操作：开始批改</span></div>',
            unsafe_allow_html=True,
        )
        topic = st.text_area(
            "英文作文题目",
            height=120,
            placeholder="请粘贴完整的 Task 2 英文题目。",
            key="topic_input",
        )
        essay = st.text_area(
            "你的英文作文",
            height=420,
            placeholder="请粘贴完整英文作文。",
            key="essay_input",
        )
        word_count = count_words(essay)
        col_words, col_model = st.columns(2)
        with col_words:
            render_score_card("当前词数", str(word_count), "Task 2 建议 250 词以上")
        with col_model:
            render_score_card("固定模型", PRODUCTION_MODEL, "评分标准保持一致")
        warning = word_count_warning("Task 2", word_count) if essay.strip() else ""
        if warning:
            st.warning(warning)
        label = (
            f"使用 {PRODUCTION_MODEL} 重新评分"
            if st.session_state.get("grading_failed")
            else "开始批改作文"
        )
        if st.button(label, type="primary", use_container_width=True):
            if not topic.strip() or not essay.strip():
                st.error("请同时填写英文作文题目和作文正文。")
                return
            with st.spinner("正在评分、核对原文证据并生成训练任务……"):
                render_scoring_loader()
                try:
                    grade_submission(store, user, topic=topic, essay=essay)
                    st.rerun()
                except AIGraderError as exc:
                    st.session_state.grading_failed = True
                    st.error("评分服务暂时不可用。题目和作文已经保留，可以直接重试。")
                    with st.expander("查看技术诊断"):
                        st.code(str(exc), language="text")
                except CloudStoreError as exc:
                    st.session_state.grading_failed = True
                    st.error(f"云端保存失败，题目和作文未清空：{exc}")
                except Exception as exc:
                    st.session_state.grading_failed = True
                    st.error("评分没有完成，没有产生半份记录。请稍后重试。")
                    with st.expander("查看技术诊断"):
                        st.code(f"{type(exc).__name__}: {exc}", language="text")


def _essay_with_issue_marks(essay: str, corrections: list[dict[str, object]]) -> str:
    spans: list[tuple[int, int, int]] = []
    for index, item in enumerate(corrections, start=1):
        original = str(item.get("original") or "").strip()
        if not original:
            continue
        match = re.search(re.escape(original), essay, flags=re.IGNORECASE)
        if match and not any(match.start() < end and match.end() > start for start, end, _ in spans):
            spans.append((match.start(), match.end(), index))
    spans.sort()
    parts: list[str] = []
    cursor = 0
    for start, end, index in spans:
        parts.append(html.escape(essay[cursor:start]))
        parts.append(f'<mark class="issue-mark">{html.escape(essay[start:end])}<sup>{index}</sup></mark>')
        cursor = end
    parts.append(html.escape(essay[cursor:]))
    return "".join(parts).replace("\n", "<br>")


def queue_correction_for_training(
    correction: dict[str, object],
    store: SupabaseStore,
    user: CloudUser | None,
) -> None:
    st.session_state.queued_sentence_training = {
        "original": str(correction.get("original") or ""),
        "reference": str(correction.get("improved") or ""),
    }
    if user is not None:
        ensure_learning_assets(store, user)
        try:
            for item in store.list_learning_items(user):
                if item.get("source_text") == correction.get("original"):
                    store.update_learning_item(user, str(item.get("id")), status="practicing")
                    break
        except (CloudStoreError, AttributeError):
            pass
    navigate("training", str(st.session_state.get("active_run_id", "")))


def render_report_downloads(report: str) -> None:
    markdown_col, pdf_col = st.columns(2)
    with markdown_col:
        st.download_button("下载 Markdown 报告", report, "essaypilot-report.md", "text/markdown", use_container_width=True)
    with pdf_col:
        st.download_button("下载 PDF 报告", markdown_to_pdf(report), "essaypilot-report.pdf", "application/pdf", use_container_width=True)


def render_report_page(store: SupabaseStore, user: CloudUser | None) -> None:
    report = str(st.session_state.get("latest_report") or "")
    structured = st.session_state.get("latest_structured")
    if not report or not isinstance(structured, dict) or not structured:
        st.info("还没有可显示的批改报告。")
        st.button("去提交作文", type="primary", on_click=navigate, args=("write",))
        return
    report = learner_safe_report_markdown(report, structured.get("overall_band"))
    ensure_learning_assets(store, user)
    st.markdown('<div class="section-kicker">批改报告</div>', unsafe_allow_html=True)
    st.title("先看最影响提分的问题")
    render_training_stepper(active=2)
    if st.session_state.pop("reused_result_notice", False):
        st.info("已复用相同作文的当前中文版评分结果，本次未消耗 Token。")
    priorities = [item for item in structured.get("priorities", []) if isinstance(item, dict)]
    render_overall_band(float(structured.get("overall_band") or 0))
    if priorities:
        summary = str(priorities[0].get("title") or priorities[0].get("why") or "")
        if summary:
            st.markdown(
                f'<div class="ep-result-summary"><strong>本轮最重要：</strong> '
                f'{html.escape(summary)}</div>',
                unsafe_allow_html=True,
            )
    render_structured_criteria_overview(structured)
    if priorities:
        st.subheader("本轮只优先解决这两项")
        cols = st.columns(min(2, len(priorities)))
        for index, item in enumerate(priorities[:2]):
            if not isinstance(item, dict):
                continue
            with cols[index]:
                with st.container(border=True):
                    st.markdown(f"### {item.get('title', '提分重点')}")
                    st.write(item.get("why", ""))
                    st.success(str(item.get("action", "")))
                    if item.get("success_check"):
                        st.caption(f"完成检查：{item['success_check']}")
    corrections = [item for item in structured.get("sentence_corrections", []) if isinstance(item, dict)]
    essay = str(st.session_state.get("essay_input") or "")
    overview_tab, correction_tab, full_tab = st.tabs(["重点诊断", "原文问题地图", "完整报告与下载"])
    with overview_tab:
        render_problem_cards(report)
        render_suggestion_cards(report)
    with correction_tab:
        st.caption("原文中的编号与下方修改卡一一对应。整理和跳转不会调用模型。")
        if essay:
            st.markdown(f'<div class="issue-map">{_essay_with_issue_marks(essay, corrections)}</div>', unsafe_allow_html=True)
        for index, correction in enumerate(corrections, start=1):
            with st.container(border=True):
                st.markdown(f"### {index}. {criterion_for_problem(str(correction.get('problem', '')))}")
                st.error(str(correction.get("original", "")))
                st.write(str(correction.get("problem", "")))
                st.success(str(correction.get("improved", "")))
                train_col, book_col = st.columns(2)
                with train_col:
                    st.button(
                        "加入单句训练", key=f"queue_correction_{index}", use_container_width=True,
                        on_click=queue_correction_for_training, args=(correction, store, user),
                    )
                with book_col:
                    if st.button("收入错题本", key=f"save_correction_{index}", use_container_width=True):
                        ensure_learning_assets(store, user)
                        st.success("已收入错题本，不会产生模型请求。")
    with full_tab:
        render_grouped_examiner_report(report)
        render_report_downloads(report)
    next_col, growth_col = st.columns(2)
    with next_col:
        st.button("开始专项训练", type="primary", use_container_width=True, on_click=navigate, args=("training", str(st.session_state.get("active_run_id", ""))))
    with growth_col:
        st.button("查看错题本与成长", use_container_width=True, on_click=navigate, args=("growth",))


def render_training_page(store: SupabaseStore, user: CloudUser | None) -> None:
    structured = st.session_state.get("latest_structured")
    if not isinstance(structured, dict) or not structured:
        st.info("请先完成一次作文批改，再开始专项训练。")
        st.button("去写作批改", type="primary", on_click=navigate, args=("write",))
        return
    st.markdown('<div class="section-kicker">专项训练</div>', unsafe_allow_html=True)
    st.title("把本轮问题真正练会")
    render_training_stepper(active=3)
    run_id = str(st.session_state.get("latest_cloud_ids", {}).get("grading_run_id", ""))
    if user is not None:
        try:
            pending = store.list_pending_practice(user)
        except CloudStoreError:
            pending = []
        if pending:
            st.info(f"你有 {len(pending)} 项未完成训练，本页已优先显示当前作文的任务。")
    sentence_data = [item for item in structured.get("sentence_training", []) if isinstance(item, dict)]
    sentences = [str(item.get("original") or "") for item in sentence_data]
    references = [str(item.get("reference") or "") for item in sentence_data]
    queued = st.session_state.get("queued_sentence_training")
    if isinstance(queued, dict) and queued.get("original"):
        if queued["original"] not in sentences:
            sentences.insert(0, str(queued["original"]))
            references.insert(0, str(queued.get("reference") or ""))
    sentence_tab, logic_tab, draft_tab = st.tabs(["单句训练", "逻辑训练", "第二稿验证"])
    with sentence_tab:
        render_sentence_practice(
            sentences, "OpenAI", PRODUCTION_MODEL, references=references,
            cloud_store=store if user is not None else None, cloud_user=user,
            grading_run_id=run_id, error_tags=list(structured.get("error_tags", [])),
        )
    with logic_tab:
        render_logic_practice(
            [item for item in structured.get("logic_training", []) if isinstance(item, dict)],
            "OpenAI", PRODUCTION_MODEL,
            cloud_store=store if user is not None else None, cloud_user=user,
            grading_run_id=run_id, error_tags=list(structured.get("error_tags", [])),
        )
    with draft_tab:
        st.session_state.draft_2_active = True
        render_draft_2_training(
            provider="OpenAI", model=PRODUCTION_MODEL, task_type="Task 2",
            user_id=user.id if user is not None else st.session_state.user_id,
            cloud_store=store if user is not None else None, cloud_user=user,
        )
    if st.session_state.pop("learning_assets_sync_error", False):
        st.caption("训练已保存；错题掌握状态将在数据库升级后自动联动。")


def _expression_status_label(status: object) -> str:
    return {"new": "待学习", "practicing": "练习中", "mastered": "已掌握"}.get(str(status), "待学习")


def _normalise_expression(item: dict[str, object]) -> dict[str, object]:
    """Give catalog and cloud expressions one display shape."""
    if item.get("catalog_id"):
        return dict(item)
    run = item.get("grading_runs") if isinstance(item.get("grading_runs"), dict) else {}
    essay = run.get("essays") if isinstance(run.get("essays"), dict) else {}
    return {
        "learning_item_id": item.get("id"),
        "item_key": item.get("item_key"),
        "origin": item.get("origin") or "report",
        "topic_category": item.get("topic_category") or "society_family",
        "function_category": item.get("function_category") or "core_collocation",
        "expression": item.get("source_text") or "",
        "meaning": item.get("explanation") or "",
        "usage_note": item.get("usage_note") or "",
        "example": item.get("target_text") or "",
        "favorite": bool(item.get("favorite")),
        "status": item.get("status") or "new",
        "created_at": item.get("created_at") or "",
        "source_question": essay.get("question") or "",
    }


def _persist_catalog_expression(
    store: SupabaseStore, user: CloudUser, item: dict[str, object]
) -> dict[str, object]:
    row = catalog_learning_item(item, user_id=user.id)
    saved = store.upsert_learning_item(user, row)
    return _normalise_expression(saved or row)


def _render_expression_card(
    item: dict[str, object], *, store: SupabaseStore, user: CloudUser | None, key: str
) -> None:
    expression = _normalise_expression(item)
    topic = TOPIC_LABELS.get(str(expression.get("topic_category")), "其他")
    function = FUNCTION_LABELS.get(str(expression.get("function_category")), "核心搭配")
    with st.container(border=True):
        st.markdown(f"### {expression.get('expression', '')}")
        st.caption(f"{topic} · {function} · {_expression_status_label(expression.get('status'))}")
        st.write(str(expression.get("meaning") or ""))
        if expression.get("usage_note"):
            st.info(str(expression.get("usage_note")))
        st.markdown(f"**例句：** {expression.get('example', '')}")
        favorite_col, practice_col = st.columns(2)
        with favorite_col:
            favorite = bool(expression.get("favorite"))
            if st.button("取消收藏" if favorite else "收藏", key=f"fav_{key}", use_container_width=True):
                if user is None:
                    st.warning("登录后即可收藏并跨设备同步。")
                else:
                    try:
                        if not expression.get("learning_item_id"):
                            expression = _persist_catalog_expression(store, user, expression)
                        store.update_learning_item(
                            user, str(expression.get("learning_item_id")), favorite=not favorite
                        )
                        st.rerun()
                    except (CloudStoreError, AttributeError) as exc:
                        st.warning(f"收藏暂时无法保存：{exc}")
        with practice_col:
            if st.button("开始造句", key=f"practice_{key}", type="primary", use_container_width=True):
                if user is not None and not expression.get("learning_item_id"):
                    try:
                        expression = _persist_catalog_expression(store, user, expression)
                    except (CloudStoreError, AttributeError) as exc:
                        st.warning(f"练习条目暂时无法保存：{exc}")
                        return
                st.session_state.expression_practice_item = expression
                st.session_state.expression_open_practice = True
                st.session_state.pop("expression_practice_result", None)
                st.session_state.pop("expression_student_sentence", None)
                if user is not None and expression.get("learning_item_id"):
                    try:
                        store.update_learning_item(
                            user, str(expression.get("learning_item_id")), status="practicing"
                        )
                    except (CloudStoreError, AttributeError):
                        pass
                st.rerun()


def render_expression_library(
    store: SupabaseStore, user: CloudUser | None, personal_items: list[dict[str, object]] | None = None
) -> None:
    """Render the static catalog, personal assets, and opt-in AI practice."""
    catalog = load_expression_catalog()
    personal_items = personal_items or []
    personal = [_normalise_expression(item) for item in personal_items if item.get("item_type") == "expression"]
    by_key = {str(item.get("item_key")): item for item in personal}
    for item in catalog:
        saved = by_key.get(f"catalog:{item['catalog_id']}")
        if saved:
            item.update({
                "learning_item_id": saved.get("learning_item_id"), "favorite": saved.get("favorite"),
                "status": saved.get("status"), "item_key": saved.get("item_key"),
            })

    if st.session_state.pop("expression_open_practice", False):
        st.session_state.expression_library_view = "表达练习"
    view = st.radio(
        "表达库视图", ["题材表达库", "我的表达", "表达练习"], horizontal=True,
        key="expression_library_view", label_visibility="collapsed",
    )
    if view == "题材表达库":
        st.caption("10 个 Task 2 高频题材，共 150 条人工整理表达；浏览、搜索和查看例句均为 0 Token。")
        mastered_topics = Counter(
            str(item.get("topic_category")) for item in personal if item.get("status") == "mastered"
        )
        st.markdown(
            '<div class="feature-strip">' + "".join(
                f'<div class="feature-chip"><strong>{html.escape(label)}</strong>'
                f'{mastered_topics.get(key, 0)}/15 已掌握</div>'
                for key, label in TOPIC_LABELS.items()
            ) + "</div>",
            unsafe_allow_html=True,
        )
        query = st.text_input("搜索表达或中文释义", placeholder="例如：public transport / 公共交通")
        filter_cols = st.columns(2)
        topic_label = filter_cols[0].selectbox("题材", ["全部题材", *TOPIC_LABELS.values()])
        function_label = filter_cols[1].selectbox("写作功能", ["全部功能", *FUNCTION_LABELS.values()])
        topic_key = next((key for key, label in TOPIC_LABELS.items() if label == topic_label), "")
        function_key = next((key for key, label in FUNCTION_LABELS.items() if label == function_label), "")
        filtered = [item for item in catalog if not topic_key or item["topic_category"] == topic_key]
        filtered = [item for item in filtered if not function_key or item["function_category"] == function_key]
        if user is not None:
            personal_filters = st.columns(2)
            favorite_only = personal_filters[0].checkbox("只看收藏", key="catalog_favorite_only")
            status_label = personal_filters[1].selectbox(
                "掌握状态", ["全部状态", "待学习", "练习中", "已掌握"], key="catalog_status"
            )
            status_key = {"待学习": "new", "练习中": "practicing", "已掌握": "mastered"}.get(status_label, "")
            filtered = [item for item in filtered if not favorite_only or item.get("favorite")]
            filtered = [item for item in filtered if not status_key or item.get("status", "new") == status_key]
        if query.strip():
            needle = query.strip().casefold()
            filtered = [
                item for item in filtered
                if needle in f"{item['expression']} {item['meaning']} {item['usage_note']} {item['example']}".casefold()
            ]
        st.write(f"共找到 {len(filtered)} 条")
        for item in filtered[:45]:
            _render_expression_card(item, store=store, user=user, key=str(item["catalog_id"]))
        if len(filtered) > 45:
            st.info("结果较多，请选择题材或继续搜索以缩小范围。")
    elif view == "我的表达":
        if user is None:
            st.info("登录后，收藏的题材表达和每次批改沉淀的 6–8 条个人表达会显示在这里。")
            return
        if not personal:
            st.info("收藏一条题材表达，或完成一次新版作文批改后，这里会形成你的个人表达库。")
            return
        filters = st.columns(3)
        topic_label = filters[0].selectbox("题材筛选", ["全部题材", *TOPIC_LABELS.values()], key="mine_topic")
        status_label = filters[1].selectbox("掌握状态", ["全部状态", "待学习", "练习中", "已掌握"])
        favorite_only = filters[2].checkbox("只看收藏")
        topic_key = next((key for key, label in TOPIC_LABELS.items() if label == topic_label), "")
        status_key = {"待学习": "new", "练习中": "practicing", "已掌握": "mastered"}.get(status_label, "")
        shown = [item for item in personal if not topic_key or item.get("topic_category") == topic_key]
        shown = [item for item in shown if not status_key or item.get("status") == status_key]
        shown = [item for item in shown if not favorite_only or item.get("favorite")]
        for index, item in enumerate(shown):
            if item.get("origin") == "report":
                source = str(item.get("source_question") or "作文题目未记录")
                st.caption(f"来自作文：{source[:100]} · {str(item.get('created_at') or '')[:10]}")
            _render_expression_card(item, store=store, user=user, key=f"mine_{index}_{item.get('learning_item_id')}")
    else:
        item = st.session_state.get("expression_practice_item")
        if not isinstance(item, dict) or not item:
            st.info("请先在题材表达库或我的表达中选择“开始造句”。")
            return
        expression = _normalise_expression(item)
        st.markdown(f"### 使用 `{expression.get('expression', '')}` 写一个英文句子")
        st.write(str(expression.get("meaning") or ""))
        st.caption(str(expression.get("usage_note") or ""))
        sentence = st.text_area("你的英文句子", key="expression_student_sentence", height=130)
        st.caption(f"只有点击下方按钮才会调用 {PRODUCTION_MODEL}；修改后可以再次提交。")
        if st.button("获取 AI 点评", type="primary", use_container_width=True):
            if not sentence.strip():
                st.warning("请先写一个包含目标表达的英文句子。")
            else:
                with st.spinner("正在检查表达含义、搭配、语法和语境……"):
                    try:
                        result = review_expression_sentence(
                            expression=str(expression.get("expression") or ""),
                            meaning=str(expression.get("meaning") or ""),
                            usage_note=str(expression.get("usage_note") or ""),
                            student_sentence=sentence,
                        )
                        st.session_state.expression_practice_result = result
                        if user is not None and expression.get("learning_item_id"):
                            store.save_expression_attempt(
                                user, learning_item_id=str(expression.get("learning_item_id")),
                                submitted_sentence=sentence, result=result, model=PRODUCTION_MODEL,
                                prompt_version=EXPRESSION_PRACTICE_PROMPT_VERSION,
                            )
                            store.update_learning_item(
                                user, str(expression.get("learning_item_id")),
                                status="mastered" if result.get("mastered") else "practicing",
                                review_count=int(expression.get("review_count") or 0) + 1,
                            )
                    except AIGraderError as exc:
                        st.error("AI 点评暂时不可用，你的句子已经保留，可以直接重试。")
                        with st.expander("查看技术诊断"):
                            st.code(str(exc), language="text")
                    except (CloudStoreError, AttributeError) as exc:
                        st.warning(f"点评已生成，但云端保存暂时失败：{exc}")
        result = st.session_state.get("expression_practice_result")
        if isinstance(result, dict):
            if result.get("mastered"):
                st.success("已掌握：表达使用准确、语法基本正确且语境自然。")
            else:
                st.warning("还未掌握：根据点评修改后再试一次。")
            st.write(str(result.get("feedback_zh") or ""))
            st.info(f"优化句：{result.get('improved_sentence_en', '')}")


def render_growth_page(store: SupabaseStore, user: CloudUser | None) -> None:
    st.markdown('<div class="section-kicker">错题本与成长</div>', unsafe_allow_html=True)
    st.title("把零散反馈变成可复习资产")
    if user is None:
        st.info("题材表达库可直接浏览；登录后可收藏、练习并跨设备同步进度。")
        render_expression_library(store, None, [])
        st.divider()
        render_history(st.session_state.user_id)
        return
    try:
        runs = store.list_grading_runs(user)
        revisions = store.list_draft_revisions(user)
    except CloudStoreError as exc:
        st.warning(f"历史与成长记录暂时无法读取：{exc}")
        runs, revisions = [], []
    try:
        items = store.list_learning_items(user)
    except (CloudStoreError, AttributeError):
        st.warning("学习资产模块正在升级，历史和成长趋势仍可正常查看。请稍后刷新页面。")
        items = []
    if runs and not items:
        latest = runs[0]
        hydrate_grading_run(latest)
        ensure_learning_assets(store, user)
        try:
            items = store.list_learning_items(user)
        except (CloudStoreError, AttributeError):
            items = []
    mastered = [item for item in items if item.get("status") == "mastered"]
    errors = [item for item in items if item.get("item_type") == "error"]
    expressions = [item for item in items if item.get("item_type") == "expression"]
    metrics = st.columns(4)
    metrics[0].metric("累计批改", len(runs))
    metrics[1].metric("待复习错误", len([item for item in errors if item.get("status") != "mastered"]))
    metrics[2].metric("已掌握", len(mastered))
    metrics[3].metric("第二稿", len(revisions))
    if runs:
        rows: list[dict[str, object]] = []
        for run in reversed(runs):
            date = str(run.get("created_at", ""))[:10]
            for criterion in run.get("criteria") or []:
                if isinstance(criterion, dict):
                    rows.append({"日期": date, "能力": CRITERION_DISPLAY_NAMES.get(str(criterion.get("criterion")), str(criterion.get("criterion"))), "分数": criterion.get("score")})
        st.altair_chart(
            alt.Chart(pd.DataFrame(rows)).mark_line(point=True).encode(
                x=alt.X("日期:N", title="练习日期"), y=alt.Y("分数:Q", scale=alt.Scale(domain=[3, 9])),
                color=alt.Color(
                    "能力:N",
                    scale=alt.Scale(range=ALPINE_CHART_COLORS),
                ),
                tooltip=["日期", "能力", "分数"],
            ).properties(height=300),
            use_container_width=True,
        )
    error_tab, expression_tab, draft_tab, share_tab = st.tabs(["个人错题本", "表达积累", "第二稿成长", "成果卡"])
    with error_tab:
        category_counts = Counter(str(item.get("category") or "grammar") for item in errors)
        if category_counts:
            st.caption("高频问题：" + " · ".join(f"{CATEGORY_LABELS.get(k, k)} × {v}" for k, v in category_counts.most_common()))
        for item in errors:
            with st.container(border=True):
                label = CATEGORY_LABELS.get(str(item.get("category")), str(item.get("category")))
                st.markdown(f"**{label} · {str(item.get('status', 'new')).replace('new', '待学习').replace('practicing', '练习中').replace('mastered', '已掌握')}**")
                st.error(str(item.get("source_text") or ""))
                st.write(str(item.get("explanation") or ""))
                st.success(str(item.get("target_text") or ""))
                if item.get("status") != "mastered" and st.button("标记为已掌握", key=f"master_asset_{item.get('id')}"):
                    try:
                        store.update_learning_item(user, str(item.get("id")), status="mastered", review_count=int(item.get("review_count") or 0) + 1)
                    except (CloudStoreError, AttributeError):
                        st.warning("云端学习资产仍在升级，请稍后重试。")
                    else:
                        st.rerun()
    with expression_tab:
        render_expression_library(store, user, expressions)
    with draft_tab:
        if not revisions:
            st.info("完成第二稿训练后，这里会显示第一稿与第二稿的变化。")
        for revision in revisions:
            original = revision.get("grading_runs") if isinstance(revision.get("grading_runs"), dict) else {}
            revised = revision.get("score_snapshot") if isinstance(revision.get("score_snapshot"), dict) else {}
            before = float(original.get("overall_band") or 0)
            after = float(revised.get("Overall Band") or 0)
            with st.expander(
                f"Overall {format_overall_band(before)} → "
                f"{format_overall_band(after)}"
            ):
                st.markdown(str(revision.get("progress_report") or ""))
    with share_tab:
        if not runs:
            st.info("完成第一篇批改后即可生成匿名成果卡。")
        else:
            latest = runs[0]
            latest_revision_gain = None
            if revisions:
                original = revisions[0].get("grading_runs") or {}
                revised = revisions[0].get("score_snapshot") or {}
                if isinstance(original, dict) and isinstance(revised, dict):
                    latest_revision_gain = float(revised.get("Overall Band") or 0) - float(original.get("overall_band") or 0)
            structured = latest.get("report_json") if isinstance(latest.get("report_json"), dict) else {}
            svg = build_result_card_svg(
                overall_band=float(latest.get("overall_band") or 0),
                criteria=[item for item in latest.get("criteria") or [] if isinstance(item, dict)],
                priority=_run_priority(structured),
                mastered_count=len(mastered),
                draft_gain=latest_revision_gain,
            )
            card_uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
            st.image(card_uri, use_container_width=True)
            st.download_button("下载匿名成果卡", svg, "essaypilot-result.svg", "image/svg+xml", use_container_width=True)


def render_product_route(store: SupabaseStore, user: CloudUser | None) -> None:
    route = str(st.session_state.get("page_mode", "home"))
    if route == "workspace":
        route = "home"
        st.session_state.page_mode = route
    render_app_navigation(user, cloud_enabled=store.enabled)
    if route == "home":
        render_home_page(store, user)
    elif route == "write":
        render_write_page(store, user)
    elif route == "report":
        render_report_page(store, user)
    elif route == "training":
        render_training_page(store, user)
    elif route == "growth":
        render_growth_page(store, user)


if st.session_state.page_mode == "demo":
    render_demo_page()
    st.stop()

cloud_store = SupabaseStore()
cloud_user = session_cloud_user()
requested_page = str(st.query_params.get("page", "") or "")
visitor_catalog = requested_page == "growth"
if cloud_store.enabled and cloud_user is None and not visitor_catalog:
    render_login_page(cloud_store)
    st.divider()
    if st.button("无需登录，先浏览 150 条题材表达", use_container_width=True):
        navigate("growth")
        st.rerun()
    st.stop()
if cloud_user is not None:
    user_id = cloud_user.id

if requested_page in APP_ROUTES:
    st.session_state.page_mode = requested_page
elif st.session_state.page_mode not in APP_ROUTES:
    st.session_state.page_mode = "home"
ensure_run_context(cloud_store, cloud_user)
render_product_route(cloud_store, cloud_user)
st.stop()

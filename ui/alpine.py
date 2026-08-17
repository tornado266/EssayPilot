"""Alpine / Summit presentation components for the Streamlit app.

This module only renders trusted, local UI chrome. User essays and model output are
escaped before they are inserted into custom HTML.
"""

from __future__ import annotations

import base64
import difflib
import html
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import streamlit as st


BASE_DIR = Path(__file__).resolve().parents[1]
CSS_PATH = BASE_DIR / "styles" / "essaypilot_alpine.css"
HERO_WEBP_PATH = BASE_DIR / "assets" / "alpine" / "hero-mountain.webp"
HERO_JPG_PATH = BASE_DIR / "assets" / "alpine" / "hero-mountain.jpg"


@dataclass(frozen=True)
class ParagraphChange:
    before: str
    after: str


def split_draft_paragraphs(text: str) -> list[str]:
    """Split CRLF, blank-line, and single-line drafts into stable paragraphs."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized:
        return []
    separator = r"\n[ \t]*\n+" if re.search(r"\n[ \t]*\n", normalized) else r"\n"
    return [part.strip() for part in re.split(separator, normalized) if part.strip()]


def align_draft_paragraphs(original: str, revised: str) -> list[ParagraphChange]:
    """Align paragraphs before applying word-level diff inside each pair."""
    before = split_draft_paragraphs(original)
    after = split_draft_paragraphs(revised)
    rows = len(before) + 1
    cols = len(after) + 1
    scores = [[0.0] * cols for _ in range(rows)]
    moves = [[""] * cols for _ in range(rows)]
    for i in range(1, rows):
        scores[i][0], moves[i][0] = -float(i), "delete"
    for j in range(1, cols):
        scores[0][j], moves[0][j] = -float(j), "insert"
    for i in range(1, rows):
        for j in range(1, cols):
            similarity = difflib.SequenceMatcher(None, before[i - 1], after[j - 1], autojunk=False).ratio()
            choices = (
                (scores[i - 1][j - 1] + (2 * similarity - 0.45), "pair"),
                (scores[i - 1][j] - 1, "delete"),
                (scores[i][j - 1] - 1, "insert"),
            )
            scores[i][j], moves[i][j] = max(choices, key=lambda item: item[0])
    aligned: list[ParagraphChange] = []
    i, j = len(before), len(after)
    while i or j:
        move = moves[i][j]
        if move == "pair":
            aligned.append(ParagraphChange(before[i - 1], after[j - 1]))
            i -= 1
            j -= 1
        elif move == "delete":
            aligned.append(ParagraphChange(before[i - 1], ""))
            i -= 1
        else:
            aligned.append(ParagraphChange("", after[j - 1]))
            j -= 1
    return list(reversed(aligned))


_DIFF_TOKEN = re.compile(r"\s+|[\w]+(?:['’-][\w]+)*|[^\w\s]", re.UNICODE)


def word_diff_html(original: str, revised: str) -> tuple[str, int, int]:
    """Return escaped inline diff plus non-whitespace added/deleted token counts."""
    before = _DIFF_TOKEN.findall(original)
    after = _DIFF_TOKEN.findall(revised)
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    fragments: list[str] = []
    added = deleted = 0
    for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
        old_text = html.escape("".join(before[i1:i2]))
        new_text = html.escape("".join(after[j1:j2]))
        if opcode == "equal":
            fragments.append(old_text)
        else:
            deleted += sum(not token.isspace() for token in before[i1:i2])
            added += sum(not token.isspace() for token in after[j1:j2])
            if old_text:
                fragments.append(f"<del>{old_text}</del>")
            if new_text:
                fragments.append(f"<ins>{new_text}</ins>")
    return "".join(fragments), added, deleted


def paragraph_diff_html(original: str, revised: str) -> str:
    cards: list[str] = []
    for index, change in enumerate(align_draft_paragraphs(original, revised), start=1):
        diff, added, deleted = word_diff_html(change.before, change.after)
        revised_html = html.escape(change.after) if change.after else '<span class="ep-diff__empty">本段已删除</span>'
        cards.append(
            f'<article class="ep-paragraph-diff"><header>第 {index} 段'
            f'<span>新增 {added} · 删除 {deleted}</span></header>'
            f'<div class="ep-paragraph-diff__after">{revised_html}</div>'
            f'<div class="ep-paragraph-diff__changes">{diff or "无变化"}</div></article>'
        )
    return '<section class="ep-paragraph-diffs">' + "".join(cards) + "</section>"


@lru_cache(maxsize=4)
def _image_data_uri(path_string: str) -> str:
    path = Path(path_string)
    mime = "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def inject_alpine_theme() -> None:
    """Load the Alpine theme and persistent official-site entry."""
    st.html(CSS_PATH)
    st.html(
        """
        <a class="ep-global-site-entry" href="https://essaypilot.cn/" target="_blank"
           rel="noopener noreferrer" aria-label="访问 EssayPilot 官网，内含新手教程">
            <strong><span>访问 EssayPilot </span>官网</strong>
            <small>内含新手教程</small>
        </a>
        """
    )


def render_hero(*, variant: str = "home") -> None:
    """Render the shared low-density photographic hero."""
    content = {
        "home": (
            "IELTS WRITING · DELIBERATE PRACTICE",
            "每次重写，都更接近清晰表达",
            "从四项评分和原文证据出发，把核心问题变成训练，再用第二稿验证是否真正改善。",
            "从诊断到第二稿，沿着一条清楚的路径前进。",
        ),
        "demo": (
            "零 TOKEN 完整示范",
            "看懂一篇作文，如何一步步改善",
            "浏览输入、评分、诊断、训练与第二稿的完整流程；本页不会调用模型，也不消耗 Token。",
            "静态示范展示真实产品流程，不制造虚假进度。",
        ),
        "login": (
            "ESSAYPILOT 学习档案",
            "让每一次修改，都留在成长路径里",
            "使用邮箱验证码登录，跨设备保存作文、训练进度和第二稿对比。",
            "安静记录每次练习，也保留你已经走过的路。",
        ),
    }
    eyebrow, title, description, image_note = content.get(variant, content["home"])
    webp = _image_data_uri(str(HERO_WEBP_PATH))
    jpg = _image_data_uri(str(HERO_JPG_PATH))
    st.markdown(
        f"""
        <section class="ep-hero ep-hero--{html.escape(variant)}">
            <div class="ep-hero__copy">
                <div class="ep-eyebrow">{html.escape(eyebrow)}</div>
                <h1>{html.escape(title)}</h1>
                <p>{html.escape(description)}</p>
                <div class="ep-hero__trail" aria-hidden="true">
                    <span></span><span></span><span></span><span></span>
                </div>
            </div>
            <figure class="ep-hero__media">
                <picture>
                    <source srcset="{webp}" type="image/webp">
                    <img src="{jpg}" alt="瑞士 Crans-Montana 的真实雪山景观" loading="eager">
                </picture>
                <figcaption>{html.escape(image_note)}</figcaption>
            </figure>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_feature_bento() -> None:
    """Render a four-card overview made only from existing product features."""
    st.markdown(
        """
        <section class="ep-bento" aria-label="EssayPilot 核心功能">
            <article class="ep-bento__card ep-bento__card--wide">
                <span class="ep-bento__index">01 · SCORE & EVIDENCE</span>
                <h3>评分分析</h3>
                <p>查看 TR、CC、LR、GRA 四项分数，并用连续原文证据核对判断。</p>
            </article>
            <article class="ep-bento__card">
                <span class="ep-bento__index">02 · FOCUSED PRACTICE</span>
                <h3>针对训练</h3>
                <p>把最重要的问题直接变成单句、逻辑与造句练习。</p>
            </article>
            <article class="ep-bento__card">
                <span class="ep-bento__index">03 · REVISION</span>
                <h3>二稿对比</h3>
                <p>保留第一稿基线，对照真实增删与四项能力变化。</p>
            </article>
            <article class="ep-bento__card ep-bento__card--wide">
                <span class="ep-bento__index">04 · LEARNING RECORD</span>
                <h3>错题与表达</h3>
                <p>保存批改中的典型错误和可迁移表达，通过复习与造句把反馈带到下一篇作文。</p>
                <a class="ep-bento__action" href="?page=growth&amp;mode=expressions">打开表达库</a>
            </article>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_training_stepper(*, active: int) -> None:
    """Render the fixed five-step learning path without changing navigation state."""
    labels = ("初稿", "分析", "训练", "二稿", "对比")
    items = []
    for index, label in enumerate(labels, start=1):
        state = "is-done" if index < active else "is-active" if index == active else ""
        current = ' aria-current="step"' if index == active else ""
        items.append(
            f'<li class="{state}"{current}><span>{index}</span><strong>{label}</strong></li>'
        )
    st.markdown(
        '<ol class="ep-stepper" aria-label="写作训练流程">' + "".join(items) + "</ol>",
        unsafe_allow_html=True,
    )


def render_scoring_loader() -> None:
    """Render an indeterminate local loader with truthful analysis stages."""
    st.markdown(
        """
        <section class="ep-loader-panel" role="status" aria-live="polite">
            <div class="ep-loader" aria-hidden="true"><i></i><i></i><i></i></div>
            <div>
                <strong>正在整理这篇作文</strong>
                <p>读取任务回应 · 分析结构与衔接 · 检查词汇与语法 · 生成训练建议</p>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_text_diff(original: str, revised: str) -> None:
    """Render the legacy full revision view, now aligned and split by paragraph."""
    fragments = [word_diff_html(item.before, item.after)[0] for item in align_draft_paragraphs(original, revised)]
    st.markdown(
        """
        <section class="ep-diff" aria-label="第一稿与第二稿文本差异">
            <div class="ep-diff__legend"><span class="is-removed">删除</span><span class="is-added">新增</span></div>
            <div class="ep-diff__text">"""
        + "<br><br>".join(fragments)
        + "</div></section>",
        unsafe_allow_html=True,
    )

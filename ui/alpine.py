"""Alpine / Summit presentation components for the Streamlit app.

This module only renders trusted, local UI chrome. User essays and model output are
escaped before they are inserted into custom HTML.
"""

from __future__ import annotations

import base64
import difflib
import html
from functools import lru_cache
from pathlib import Path

import streamlit as st


BASE_DIR = Path(__file__).resolve().parents[1]
CSS_PATH = BASE_DIR / "styles" / "essaypilot_alpine.css"
HERO_WEBP_PATH = BASE_DIR / "assets" / "alpine" / "hero-mountain.webp"
HERO_JPG_PATH = BASE_DIR / "assets" / "alpine" / "hero-mountain.jpg"


@lru_cache(maxsize=4)
def _image_data_uri(path_string: str) -> str:
    path = Path(path_string)
    mime = "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def inject_alpine_theme() -> None:
    """Load the local Alpine stylesheet once per Streamlit script run."""
    st.html(CSS_PATH)


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
    """Render a copyable word-level inline diff for two user-authored drafts."""
    before = original.split()
    after = revised.split()
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    fragments: list[str] = []
    for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
        old_text = html.escape(" ".join(before[i1:i2]))
        new_text = html.escape(" ".join(after[j1:j2]))
        if opcode == "equal":
            fragments.append(old_text)
        elif opcode == "delete":
            fragments.append(f"<del>{old_text}</del>")
        elif opcode == "insert":
            fragments.append(f"<ins>{new_text}</ins>")
        else:
            fragments.append(f"<del>{old_text}</del><ins>{new_text}</ins>")
    st.markdown(
        """
        <section class="ep-diff" aria-label="第一稿与第二稿文本差异">
            <div class="ep-diff__legend"><span class="is-removed">删除</span><span class="is-added">新增</span></div>
            <div class="ep-diff__text">"""
        + " ".join(fragments)
        + "</div></section>",
        unsafe_allow_html=True,
    )

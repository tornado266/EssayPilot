"""Storage helpers for IELTS correction records."""

import json
import re
import uuid
from io import BytesIO
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.result_parser import parse_band


RECORDS_DIR = Path("records")
PDF_FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansSC-Regular.ttf"
SCORE_PATTERN = re.compile(r"(?:最可能分数|Likely Score|Overall Band Score|Overall Band|likely score)[^\d]*(\d(?:\.\d)?)")

INK = colors.HexColor("#173B45")
TEAL = colors.HexColor("#287D86")
CORAL = colors.HexColor("#E87961")
PALE_BLUE = colors.HexColor("#EAF6F7")
PALE_CORAL = colors.HexColor("#FFF1EC")
WARM_WHITE = colors.HexColor("#FCFBF8")
MUTED = colors.HexColor("#62777D")
RULE = colors.HexColor("#CFE1E3")


def get_user_records_dir(user_id: str | None = None) -> Path:
    """Return a safe per-user records directory while preserving legacy callers."""
    if user_id is None:
        return RECORDS_DIR
    try:
        normalized_user_id = str(uuid.UUID(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("user_id must be a valid UUID") from exc
    return RECORDS_DIR / normalized_user_id


def build_markdown_record(
    task_type: str,
    topic: str,
    essay: str,
    report: str,
    word_count: int,
    created_at: datetime | None = None,
    overall_band: float | None = None,
    user_id: str | None = None,
) -> str:
    """Build a complete downloadable record without writing it to disk."""
    created_at = created_at or datetime.now()
    return dedent(
        f"""
        # 雅思写作批改记录

        - 任务类型: {task_type}
        - 词数: {word_count}
        - 创建时间: {created_at.strftime("%Y-%m-%d %H:%M:%S")}
        - 总分: {overall_band if overall_band is not None else "暂无"}
        - 匿名用户编号: {user_id if user_id is not None else "旧版记录"}

        ## 英文作文题目

        {topic}

        ## 学生原稿

        {essay}

        ---

        {report}
        """
    ).strip()


def markdown_to_pdf(markdown: str) -> bytes:
    """Render a polished, portable IELTS report with an embedded CJK font."""
    if "NotoSansSC" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("NotoSansSC", str(PDF_FONT_PATH)))
        pdfmetrics.registerFontFamily(
            "NotoSansSC",
            normal="NotoSansSC",
            bold="NotoSansSC",
            italic="NotoSansSC",
            boldItalic="NotoSansSC",
        )

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title="雅思写作批改报告",
        author="EssayPilot",
    )

    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "ReportBody",
        parent=base["BodyText"],
        fontName="NotoSansSC",
        textColor=INK,
        fontSize=9.2,
        leading=15.5,
        spaceAfter=6,
        splitLongWords=True,
    )
    cover_title = ParagraphStyle(
        "CoverTitle",
        parent=body,
        fontSize=24,
        leading=31,
        alignment=TA_CENTER,
        textColor=INK,
        spaceAfter=4,
    )
    cover_kicker = ParagraphStyle(
        "CoverKicker",
        parent=body,
        fontSize=9,
        leading=13,
        alignment=TA_CENTER,
        textColor=TEAL,
        spaceAfter=12,
    )
    heading = ParagraphStyle(
        "ReportHeading",
        parent=body,
        fontSize=15,
        leading=21,
        textColor=INK,
        spaceBefore=13,
        spaceAfter=8,
        keepWithNext=True,
    )
    subheading = ParagraphStyle(
        "ReportSubheading",
        parent=body,
        fontSize=11.2,
        leading=17,
        textColor=TEAL,
        spaceBefore=9,
        spaceAfter=5,
        keepWithNext=True,
    )
    meta = ParagraphStyle(
        "Meta",
        parent=body,
        fontSize=8.2,
        leading=12,
        textColor=MUTED,
        alignment=TA_CENTER,
    )
    score_style = ParagraphStyle(
        "Score",
        parent=body,
        fontSize=30,
        leading=34,
        textColor=CORAL,
        alignment=TA_CENTER,
    )
    score_label = ParagraphStyle(
        "ScoreLabel",
        parent=meta,
        fontSize=8.5,
        textColor=INK,
        spaceAfter=3,
    )
    question_style = ParagraphStyle(
        "Question",
        parent=body,
        fontSize=10.5,
        leading=17,
        textColor=INK,
        alignment=TA_LEFT,
    )
    essay_style = ParagraphStyle(
        "Essay",
        parent=body,
        fontSize=9.5,
        leading=16.5,
        leftIndent=8,
        rightIndent=8,
        borderPadding=10,
        borderColor=RULE,
        borderWidth=0.7,
        borderRadius=4,
        backColor=WARM_WHITE,
        spaceAfter=8,
    )
    quote_style = ParagraphStyle(
        "Quote",
        parent=body,
        leftIndent=10,
        rightIndent=6,
        borderPadding=(7, 9, 7, 10),
        borderColor=TEAL,
        borderWidth=0,
        borderLeftWidth=2,
        backColor=PALE_BLUE,
        textColor=INK,
        spaceBefore=4,
        spaceAfter=8,
    )
    table_cell = ParagraphStyle(
        "TableCell",
        parent=body,
        fontSize=7.4,
        leading=11.5,
        spaceAfter=0,
    )
    table_header = ParagraphStyle(
        "TableHeader",
        parent=table_cell,
        textColor=colors.white,
    )

    record_parts = re.split(r"\n\s*---\s*\n", markdown, maxsplit=1)
    record_header = record_parts[0]
    report = record_parts[1] if len(record_parts) > 1 else markdown

    def field(pattern: str, source: str = record_header, default: str = "暂无") -> str:
        match = re.search(pattern, source, flags=re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else default

    task_type = field(r"^- (?:任务类型|Task Type):\s*(.+)$")
    word_count = field(r"^- (?:词数|Word Count):\s*(.+)$")
    created_at = field(r"^- (?:创建时间|Created At):\s*(.+)$")
    overall_band = field(r"^- (?:总分|Overall Band):\s*(.+)$")
    if overall_band in {"暂无", "N/A"}:
        score_match = SCORE_PATTERN.search(report)
        overall_band = score_match.group(1) if score_match else "暂无"

    question_match = re.search(
        r"## (?:英文作文题目|Essay Question)\s*(.*?)\s*## (?:学生原稿|Student Essay)",
        record_header,
        flags=re.DOTALL | re.IGNORECASE,
    )
    essay_match = re.search(
        r"## (?:学生原稿|Student Essay)\s*(.*)$",
        record_header,
        flags=re.DOTALL | re.IGNORECASE,
    )
    question = question_match.group(1).strip() if question_match else "未记录作文题目。"
    essay = essay_match.group(1).strip() if essay_match else "未记录学生原稿。"

    def inline_markup(text: str) -> str:
        marked = escape(text.strip())
        marked = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", marked)
        marked = re.sub(r"`(.+?)`", r"<font color='#287D86'>\1</font>", marked)
        return marked

    story = [
        Spacer(1, 16 * mm),
        Paragraph("IELTS 写作", cover_kicker),
        Paragraph("雅思写作批改报告", cover_title),
        Spacer(1, 5 * mm),
    ]

    score_card = Table(
        [[Paragraph("总分", score_label), Paragraph(overall_band, score_style)]],
        colWidths=[55 * mm, 40 * mm],
        rowHeights=[22 * mm],
        hAlign="CENTER",
    )
    score_card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE_CORAL),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#F3C8BB")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.extend([score_card, Spacer(1, 8 * mm)])

    metadata = Table(
        [[
            Paragraph(f"任务<br/><b>{inline_markup(task_type)}</b>", meta),
            Paragraph(f"词数<br/><b>{inline_markup(word_count)}</b>", meta),
            Paragraph(f"创建时间<br/><b>{inline_markup(created_at)}</b>", meta),
        ]],
        colWidths=[50 * mm, 40 * mm, 60 * mm],
        hAlign="CENTER",
    )
    metadata.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, RULE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.extend(
        [
            metadata,
            Spacer(1, 11 * mm),
            Paragraph("英文作文题目", subheading),
            HRFlowable(width="100%", thickness=1.2, color=CORAL, spaceAfter=7),
            Paragraph(inline_markup(question), question_style),
            PageBreak(),
            Paragraph("学生原稿", heading),
            HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=9),
        ]
    )
    for paragraph in re.split(r"\n\s*\n", essay):
        if paragraph.strip():
            story.append(Paragraph(inline_markup(paragraph), essay_style))
    story.extend([PageBreak(), Paragraph("批改与训练报告", heading)])

    lines = report.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line == "---":
            story.append(HRFlowable(width="100%", thickness=0.7, color=RULE, spaceBefore=5, spaceAfter=8))
            index += 1
            continue
        if line.startswith("|"):
            table_lines = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            rows = [
                [cell.strip() for cell in row.strip("|").split("|")]
                for row in table_lines
                if not re.fullmatch(r"\|?[\s:|-]+\|?", row)
            ]
            if rows:
                column_count = max(len(row) for row in rows)
                wrapped = []
                for row_index, row in enumerate(rows):
                    padded = row + [""] * (column_count - len(row))
                    cell_style = table_header if row_index == 0 else table_cell
                    wrapped.append(
                        [Paragraph(inline_markup(cell), cell_style) for cell in padded]
                    )
                table = Table(
                    wrapped,
                    colWidths=[document.width / column_count] * column_count,
                    repeatRows=1,
                    hAlign="LEFT",
                )
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), TEAL),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("BACKGROUND", (0, 1), (-1, -1), WARM_WHITE),
                    ("GRID", (0, 0), (-1, -1), 0.45, RULE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                story.extend([table, Spacer(1, 3 * mm)])
            continue
        if line.startswith("### "):
            story.append(Paragraph(inline_markup(line[4:]), subheading))
        elif line.startswith("## "):
            story.extend([
                Paragraph(inline_markup(line[3:]), heading),
                HRFlowable(width="100%", thickness=1, color=CORAL, spaceAfter=7),
            ])
        elif line.startswith("# "):
            if not any(title in line for title in ("IELTS Writing Examiner Report", "雅思写作批改报告")):
                story.append(Paragraph(inline_markup(line[2:]), heading))
        elif line.startswith(">"):
            story.append(Paragraph(inline_markup(line.lstrip("> ")), quote_style))
        elif re.match(r"^[-*]\s+", line):
            story.append(Paragraph(f"<font color='#E87961'>●</font>&nbsp;&nbsp;{inline_markup(line[2:])}", body))
        else:
            story.append(Paragraph(inline_markup(line), body))
        index += 1

    def draw_page(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("NotoSansSC", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 11 * mm, "EssayPilot 雅思写作学习报告")
        canvas.drawRightString(A4[0] - 20 * mm, 11 * mm, f"第 {doc.page} 页")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(20 * mm, 15 * mm, A4[0] - 20 * mm, 15 * mm)
        canvas.restoreState()

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return buffer.getvalue()


def save_markdown_record(
    task_type: str,
    topic: str,
    essay: str,
    report: str,
    word_count: int,
    parsed_result: dict[str, Any] | None = None,
    user_id: str | None = None,
    examiner_data: dict[str, Any] | None = None,
    grading_metadata: dict[str, Any] | None = None,
    content_hash: str = "",
) -> Path:
    """Save one correction record as Markdown plus structured JSON metadata."""
    records_dir = get_user_records_dir(user_id)
    records_dir.mkdir(parents=True, exist_ok=True)

    created_at = datetime.now()
    timestamp = created_at.strftime("%Y%m%d_%H%M%S")
    file_stem = f"ielts_{task_type.lower().replace(' ', '_')}_{timestamp}"
    file_path = records_dir / f"{file_stem}.md"
    json_path = records_dir / f"{file_stem}.json"

    parsed_result = parsed_result or {}
    parsed_data = parsed_result.get("data", {}) if parsed_result.get("ok") else {}
    overall_band = parse_band(parsed_data.get("overall_band"))
    if overall_band is None:
        score_match = SCORE_PATTERN.search(report)
        overall_band = parse_band(score_match.group(1)) if score_match else None
    criteria_scores = parsed_data.get("criteria_scores", {})
    if not isinstance(criteria_scores, dict):
        criteria_scores = {}

    metadata = {
        "timestamp": created_at.isoformat(timespec="seconds"),
        "question": topic,
        "essay": essay,
        "overall_band": overall_band,
        "criteria_scores": criteria_scores,
        "raw_result": parsed_result.get("raw", report),
        "structured_parse_ok": bool(parsed_result.get("ok")),
        "parse_error": parsed_result.get("error", ""),
        "task_type": task_type,
        "word_count": word_count,
        "markdown_file": file_path.name,
        "user_id": user_id,
        "examiner_data": examiner_data or {},
        "grading_metadata": grading_metadata or {},
        "content_hash": content_hash,
    }

    content = build_markdown_record(
        task_type=task_type,
        topic=topic,
        essay=essay,
        report=report,
        word_count=word_count,
        created_at=created_at,
        overall_band=overall_band,
        user_id=user_id,
    )

    file_path.write_text(content, encoding="utf-8")
    json_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return file_path


def extract_overall_score(markdown: str) -> float | None:
    """Extract the most likely overall band score from a correction report."""
    match = SCORE_PATTERN.search(markdown)
    if not match:
        return None

    return parse_band(match.group(1))


def _read_json_record(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
    except (OSError, json.JSONDecodeError):
        return None

    score = parse_band(data.get("overall_band"))
    created_at = str(data.get("timestamp") or path.stem)
    markdown_file = str(data.get("markdown_file") or f"{path.stem}.md")
    return {
        "file": markdown_file,
        "path": path.parent / markdown_file,
        "created_at": created_at,
        "task_type": str(data.get("task_type") or "Task 2"),
        "word_count": data.get("word_count"),
        "score": score,
        "criteria_scores": data.get("criteria_scores")
        if isinstance(data.get("criteria_scores"), dict)
        else {},
    }


def _read_markdown_record(path: Path) -> dict[str, object] | None:
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError:
        return None

    created_match = re.search(r"- (?:创建时间|Created At):\s*(.+)", markdown)
    task_match = re.search(r"- (?:任务类型|Task Type):\s*(.+)", markdown)
    words_match = re.search(r"- (?:词数|Word Count):\s*(\d+)", markdown)

    try:
        word_count = int(words_match.group(1)) if words_match else None
    except ValueError:
        word_count = None

    return {
        "file": path.name,
        "path": path,
        "created_at": created_match.group(1) if created_match else path.stem,
        "task_type": task_match.group(1) if task_match else "未知",
        "word_count": word_count,
        "score": extract_overall_score(markdown),
        "criteria_scores": {},
    }


def list_correction_history(user_id: str | None = None) -> list[dict[str, object]]:
    """Read saved correction records and return lightweight history data."""
    records_dir = get_user_records_dir(user_id)
    if not records_dir.exists():
        return []

    history: list[dict[str, object]] = []
    json_stems = set()

    for path in sorted(records_dir.glob("ielts_*.json")):
        record = _read_json_record(path)
        if record:
            history.append(record)
            json_stems.add(path.stem)

    for path in sorted(records_dir.glob("ielts_*.md")):
        if path.stem in json_stems:
            continue
        record = _read_markdown_record(path)
        if record:
            history.append(record)

    return history

"""以中文讲解为主、保留英文学习材料的报告渲染器。"""

from __future__ import annotations

from typing import Any

from src.expression_catalog import FUNCTION_LABELS

CRITERION_DISPLAY_NAMES = {
    "Task Response": "任务回应（TR）",
    "Coherence and Cohesion": "连贯与衔接（CC）",
    "Lexical Resource": "词汇资源（LR）",
    "Grammatical Range and Accuracy": "语法多样性与准确性（GRA）",
}


def examiner_result_to_markdown(
    data: dict[str, Any],
    *,
    estimated_range: tuple[float, float] | None = None,
) -> str:
    """把结构化评分结果转换为可下载、可兼容旧工具的中文 Markdown。"""
    overall = float(data["overall_band"])
    lower, upper = estimated_range or (overall, overall)
    criteria_rows: list[str] = []
    for item in data["criteria"]:
        evidence = "；".join(f'“{str(quote).strip().strip(chr(34))}”' for quote in item["evidence"][:2])
        explanation = (
            f"**当前表现：** {item['reason']} "
            f"**原文依据：** {evidence} "
            f"**为什么还没到下一档：** {item['next_band_limit']}"
        )
        label = CRITERION_DISPLAY_NAMES.get(item["criterion"], item["criterion"])
        criteria_rows.append(f"| {label} | {item['score']} | {explanation} |")

    def coaching(items: list[dict[str, Any]]) -> str:
        return "\n\n".join(
            f"{index}. **{item['title']}**\n"
            f"   - **原文依据：** “{item['evidence']}”\n"
            f"   - **为什么重要：** {item['why']}\n"
            f"   - **具体行动：** {item['action']}"
            for index, item in enumerate(items, 1)
        )

    corrections = "\n".join(
        f"| {item['original'].replace('|', '/')} | {item['problem'].replace('|', '/')} | {item['improved'].replace('|', '/')} |"
        for item in data["sentence_corrections"]
    )
    paragraphs = "\n\n".join(
        f"### 第 {item['paragraph']} 段\n**做得好的地方：** {item['strength']}\n\n"
        f"**限制分数的问题：** {item['limitation']}\n\n"
        f"**一个具体改法：** {item['improvement']}"
        for item in data["paragraph_feedback"]
    )
    expressions = "\n".join(
        f"| {item['expression'].replace('|', '/')} | {item['meaning'].replace('|', '/')} | "
        f"{FUNCTION_LABELS.get(item.get('function_category', ''), '核心搭配')} | "
        f"{str(item.get('usage_note', '')).replace('|', '/')} | {item['example'].replace('|', '/')} |"
        for item in data["useful_expressions"]
    )
    sentence_training = "\n".join(
        f'{index}. “{item["original"]}”\n   - 训练目标：{item["goal"]}\n   - 英文参考：{item["reference"]}'
        for index, item in enumerate(data["sentence_training"], 1)
    )
    logic_training = "\n\n".join(
        f"### 任务 {index}\n**问题：** {item['problem']}\n\n**训练任务：** {item['task']}\n\n"
        f'**原文：** “{item["original"]}”\n\n**要求：**\n'
        + "\n".join(f"- {rule}" for rule in item["requirements"])
        for index, item in enumerate(data["logic_training"], 1)
    )
    next_practice = data["next_practice"]
    return f"""# 雅思写作练习估分与反馈

> 本报告提供 estimated practice band（练习估分），不是 IELTS 官方成绩。

## 1. 总分

**预估分数区间：{lower:.1f}–{upper:.1f}**

**最可能分数：{overall:.1f}**

{data['summary']}

## 2. 四项评分

| 评分项 | 分数 | 评分理由与依据 |
|---|---:|---|
{chr(10).join(criteria_rows)}

## 3. 下一步训练行动

{coaching(data['priorities'])}

## 4. 主要问题

{coaching(data['problems'])}

## 5. 逐句批改

| 原句 | 问题 | 英文改写 |
|---|---|---|
{corrections}

## 6. 段落反馈

{paragraphs}

## 7. Band 7.5 英文示范改写

{data['band_75_rewrite']}

## 8. 表达积累

| 英文表达 | 中文含义 | 写作功能 | 使用提醒 | 英文例句 |
|---|---|---|---|---|
{expressions}

## 9. 下一次练习

**英文练习题：** {next_practice['task']}

- **建议练习的英文句型：** {next_practice['sentence_pattern']}
- **下次需要避免：** {next_practice['warning']}

## 11. 单句提分训练

请先独立改写，再查看英文参考并提交点评。

{sentence_training}

## 12. 写作提升验证

围绕本轮最低评分项完成段落级重写。

{logic_training}
""".strip()

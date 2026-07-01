"""IELTS Writing examiner prompt templates."""


def build_grading_prompt(task_type: str, topic: str, essay: str) -> str:
    """Build the prompt used by the IELTS correction Skill."""
    if task_type == "Task 1":
        task_focus = """
Task 1 scoring focus:
- Task Achievement: clear overview, accurate key features, relevant comparisons, no invented data
- Coherence and Cohesion: logical grouping of data, clear paragraphing, controlled linking
- Lexical Resource: precise trend/comparison language, no memorised phrases that hide meaning
- Grammatical Range and Accuracy: accurate data sentences, varied comparison structures
"""
    else:
        task_focus = """
Task 2 scoring focus:
- Task Response: clear position, fully answered question, developed ideas, relevant examples
- Coherence and Cohesion: logical progression, focused paragraphs, clear referencing and linking
- Lexical Resource: topic-specific but natural vocabulary, accurate collocations
- Grammatical Range and Accuracy: controlled complex sentences, accurate clauses and punctuation
"""

    calibration_rules = """
Official Task 2 Band 6 versus Band 7 calibration:
- Task Response 6: the main parts are addressed and the position is relevant, but some
  conclusions may be unclear or repetitive and some main ideas may be insufficiently
  developed or supported. Award 7 only when all parts are addressed, the position is
  clear and developed, and the main ideas are extended and supported throughout.
- Coherence and Cohesion 6: there is clear overall progression, but cohesion may be
  mechanical or faulty and paragraph focus or referencing may not always be logical or
  clear. Award 7 only when ideas are logically organised with clear progression
  throughout and each paragraph has a clear central topic.
- Lexical Resource 6: vocabulary is adequate and meaning is generally clear, but less
  common vocabulary may be inaccurate and spelling, word-formation, or collocation
  errors occur. Award 7 only when vocabulary shows sufficient range, flexibility, and
  precision, with some controlled less-common items and only occasional errors.
- Grammatical Range and Accuracy 6: both simple and complex forms are used, but
  flexibility is limited and errors remain noticeable. Award 7 only when a variety of
  complex structures is used with good control, frequent error-free sentences, and few
  errors that do not impede communication.
""" if task_type == "Task 2" else """
Official calibration rule: award a band only when the response fits the positive
features of that descriptor. Negative descriptor features limit the rating.
"""

    return f"""
You are a strict IELTS examiner.
Complete the scoring decision before giving any coaching or improvement advice.

Your grading must be based on IELTS Writing Band Descriptors:
- Task Response for Task 2, or Task Achievement for Task 1
- Coherence and Cohesion
- Lexical Resource
- Grammatical Range and Accuracy

Core examiner rules:
- Be balanced, realistic, and evidence-based. Be neither generous nor systematically harsh.
- Minor issues must be recorded, but isolated minor issues must not cause a full-band drop.
- If evidence genuinely falls between two descriptor levels, use the intermediate half band.
- Simulate test-day scoring, not classroom encouragement or punitive marking.
- Use the full Band 0-9 scale. Do not assume the student is near Band 6 or compress
  uncertain responses into the Band 6-7 range.
- Score each criterion independently before deciding the overall score.
- Evaluate the full descriptor scale and distinguish Band 6, 6.5, and 7 rather than
  defaulting uncertain but competent writing to Band 6.
- A polished introduction, standard paragraph structure, length, or mostly correct
  grammar must not by itself raise Task Response, Coherence, or Lexical Resource.
- Do not infer development, precision, or grammatical control that is not visible in the essay.
- Fluency cannot cancel weak task coverage, shallow development, repetitive vocabulary,
  mechanical cohesion, or language errors. However, genuine overall fluency, clarity,
  and control are positive evidence and must not be ignored.
- Do not ignore small grammar, punctuation, spelling, word-choice, or collocation errors.
  Consider their frequency, recurrence, and cumulative effect even when meaning remains clear.
- Do not use vague praise such as "good", "nice", "well-written", "strong", or
  "overall effective" as scoring evidence. Every score claim must name an observable
  feature and quote or precisely locate evidence from the essay.
- Before finalising, perform a silent two-sided check: identify both the feature limiting
  the score and the feature supporting the awarded band. Confirm that neither isolated
  strengths nor isolated weaknesses dominate the decision.
- Criterion scores may use .5 when the evidence clearly lies between adjacent official
  descriptors. Calculate the task score as the equal-weighted average of the four
  criteria and round normally to the nearest half band.
- A single grammar, punctuation, spelling, or word-choice error may affect the relevant
  criterion but must never reduce it by more than 0.5 on its own. A full-band reduction
  requires a recurring pattern, high error density, impaired clarity, or a major task issue.
- Do not reduce an otherwise Band 7 performance to Band 6 merely because expression is
  not perfect. Band 7 explicitly permits occasional errors that do not impede communication.
- Do not only praise the essay. Identify the problems that most clearly limit the band score.
- Do not invent content that the student did not write.
- Every problem you mention must quote the student's exact original sentence or phrase.
- If a problem is about a missing idea, quote the closest relevant sentence and explain what is missing.
- Do not rewrite the essay in an overly advanced native-speaker style.
- The Band 7.5 rewrite must remain learnable for a high school student.
- Prefer clear academic English over rare vocabulary.
- Focus on practical improvement from Band 6.0 to Band 7.5.
- If the task is Task 1, judge data selection, overview, comparisons, and accuracy.
- If the task is Task 2, judge position, idea development, relevance, and examples.
- Return only clean Markdown. Do not add sections outside the required structure.

Mandatory scoring sequence (perform in this exact order before assigning any score):
1. Task-fit check: identify every part of the prompt, then check for off-topic,
   partially addressed, misunderstood, or missing requirements and whether the position
   remains clear and relevant throughout.
2. Logic and structure check: inspect progression across the whole response, paragraph
   focus, idea extension, support, referencing, and whether linking is natural or mechanical.
3. Vocabulary check: inspect range, precision, repetition of basic words, memorised or
   formulaic language, collocation, word formation, and spelling. Record recurring patterns.
4. Grammar check: inspect the range of sentence forms and estimate error density across
   the whole essay. Count recurring errors and distinguish isolated slips from systematic
   problems; minor errors still count.
5. Provisional anchor: place the whole essay in the closest broad performance zone below.
6. Score decision: assign TR/TA, CC, LR, and GRA independently, using .5 when performance
   is genuinely between adjacent descriptors. Adjust the provisional anchor by at most
   0.5 unless a major task failure or a clearly different descriptor level justifies more.
   Only then calculate Overall Band.

{task_focus}

{calibration_rules}

Performance anchor mechanism:
- Band 5: serious task, organisation, vocabulary, or grammar problems regularly limit
  clarity; ideas are inadequately developed and language control is weak.
- Band 6: the response is basically clear and relevant, but noticeable weaknesses remain
  in development, progression, vocabulary control, or grammar accuracy.
- Band 6.5: communication is stable and generally well controlled. The response is
  complete, the position is clear, and there is no major task failure, while a limited
  number of noticeable issues prevent consistent Band 7 performance.
- Band 7: the response is clear, logically organised, and sufficiently developed;
  vocabulary shows variety and precision; complex grammar has good control; errors are
  occasional rather than systematic and do not reduce overall clarity.
- Band 7.5+: performance is consistently above Band 7 across most criteria, with strong
  precision, flexibility, development, and language control. It need not be native-like.

Anchor safeguards:
- A complete introduction-body-conclusion structure, a clear position, and no serious
  off-topic content make the response eligible for 6.5, but do not guarantee it; verify
  idea development and language control.
- Clear progression, varied and appropriate vocabulary, and only occasional grammar
  errors are positive evidence for 7 and must not be discounted because of minor flaws.
- Do not let one imperfect sentence erase sustained performance across the essay.
- Do not use the anchor as a substitute for the four official criteria. It is a
  calibration check to prevent score compression, followed by criterion-level scoring.

Fixed output structure:

# IELTS Writing Examiner Report

## 1. Overall Band Score

Give an estimated band range, such as 5.5-6.0 or 6.0-6.5.
Then give one likely score inside that range.
Explain in 2-4 sentences why this range is fair, using the four criterion decisions.
The likely score must equal the calculated result from the four criterion scores.

## 2. Four Criteria Scores

| Criterion | Band Range | Likely Score | Why |
|---|---:|---:|---|
| Task Response / Task Achievement |  |  |  |
| Coherence and Cohesion |  |  |  |
| Lexical Resource |  |  |  |
| Grammatical Range and Accuracy |  |  |  |

For each row, cite concrete evidence from the submitted essay and name the descriptor
feature that prevents the next higher band. Do not award Band 7 merely because the
response is understandable or well organised at a general level.

## 3. Top 3 Score-Boosting Priorities

List exactly three priorities that would most quickly move this student toward Band 7.5.
For each priority, include:
- Priority
- Original sentence or phrase as evidence
- Why it matters
- What to practise

## 4. Main Problems

List the 3-5 biggest problems holding the essay back from Band 7.5.
For each problem, include:
- Problem
- Original sentence or phrase
- Why it lowers the score
- How to improve

## 5. Sentence-level Corrections

Correct 6-10 important sentences or phrases.
Use this format:

| Original | Problem | Improved version |
|---|---|---|

## 6. Paragraph-level Feedback

Give feedback paragraph by paragraph.
For each paragraph, explain:
- What works
- What weakens the band score
- One concrete improvement

## 7. Band 7.5 Rewrite

Rewrite the full essay in a realistic Band 7.5 style.
Keep the ideas close to the student's original argument.
Do not add complex ideas that the student did not attempt.
Use vocabulary and sentence structures that a strong high school student can learn.

## 8. Useful Expressions

Give 8-12 expressions from the rewrite.
For each expression, include:
- Expression
- Meaning
- When to use it
- One short example sentence

## 9. Next Practice Task

Give one specific next IELTS Writing task.
Also give:
- One main skill to focus on
- One sentence pattern to practise
- One warning about what to avoid next time

## 11. 单句提分训练

Choose several of the weakest sentences from the student's essay.
Ask the student to rewrite these sentences.
Do not provide reference rewrites in the report.
The app will review the student's own rewritten sentences separately.
Use exactly this format:

【练习任务】
请改写下面这几句话，使其更符合雅思6.5-7分水平：

1. "（原句）"
2. "（原句）"
3. "（原句）"

## 12. 写作提升验证

Choose 2-3 core logic or structure problems from the student's essay, such as:
- unclear argument
- underdeveloped paragraph
- example does not support the point
- weak explanation
- unclear paragraph focus

For each task, choose one original paragraph or key fragment from the student's essay.
Give a practical rewrite task that requires the student to write 2-4 sentences.
Do not give comparison feedback in the report; the app will review the student's own rewrite separately.
Use exactly this format:

【提升练习】
请根据刚才的问题，重写你文章中的一个关键部分：

### 任务 1
问题：论点不清 / 段落没有发展 / 例子不支持观点

任务：
改写/重写下面内容，使其逻辑更清晰、更符合雅思6.5水平：

"（原文片段）"

要求：
- 2-4句话
- 要有清晰论点 + 解释 + 例子

IELTS task type:
{task_type}

Essay question:
{topic}

Student essay:
{essay}
""".strip()

"""IELTS Writing examiner prompt templates."""

import json
import re
from collections import Counter
from pathlib import Path


SAMPLE_ANCHORS_PATH = Path(__file__).resolve().parents[1] / "data" / "band_samples.json"
IELTS_WRITING_SKILL_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "ielts-writing" / "SKILL.md"
)


def load_band_sample_anchors() -> str:
    """Load concise local calibration anchors for repeatable scoring."""
    try:
        anchors = json.loads(SAMPLE_ANCHORS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return "\n".join(f"- Band {band}: {description}" for band, description in anchors.items())


def load_skill_scoring_rules() -> str:
    """Load only the four-criterion scoring section from the installed skill."""
    try:
        skill_text = IELTS_WRITING_SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""

    start_marker = "### Phase 2：四维评分"
    end_marker = "### Phase 3：句子级标注"
    start = skill_text.find(start_marker)
    end = skill_text.find(end_marker, start + len(start_marker))
    if start == -1 or end == -1:
        return ""
    return skill_text[start:end].strip()


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

    sample_anchors = load_band_sample_anchors()
    installed_skill_scoring_rules = load_skill_scoring_rules()

    return f"""
You are a strict and stable IELTS examiner for EssayPilot.
Complete the scoring decision before giving any coaching or improvement advice.

Your grading must be based on IELTS Writing Band Descriptors:
- Task Response for Task 2, or Task Achievement for Task 1
- Coherence and Cohesion
- Lexical Resource
- Grammatical Range and Accuracy

Installed fixed scoring logic (authoritative for the four scoring criteria):
{installed_skill_scoring_rules}

Use the installed section only to decide scores. Keep EssayPilot's existing report
structure, coaching sections, training tasks, and UI-facing wording unchanged.

Core examiner rules:
- Reproducibility is more important than stylistic variety in your feedback. Apply the
  same descriptor interpretation and decision order every time.
- Be balanced, realistic, and evidence-based. Be neither generous nor systematically harsh.
- Minor issues must be recorded, but isolated minor issues must not cause a full-band drop.
- Criterion ratings use whole bands, as in examiner descriptor decisions. Represent
  mixed performance through the four-criterion average, not half-band criterion ratings.
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
- Criterion scores must be whole bands. Calculate the task score as the equal-weighted
  average of the four criteria and round normally to the nearest half band. Overall 6.5
  should normally result from a genuine mix of Band 6 and Band 7 criterion ratings.
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
5. Mandatory classification: choose exactly one provisional level from
   5, 5.5, 6, 6.5, 7, or 7.5. Make this decision silently and do not output a separate
   classification explanation. If evidence is balanced between two levels, choose the
   lower level. Do not skip directly from 6 to 7 without testing the Band 6.5 anchor.
6. Score decision: assign whole-band TR/TA, CC, LR, and GRA ratings independently.
   Adjust the provisional overall anchor only when the criterion evidence justifies it.
   The calculated Overall Band may differ from the provisional classification by at
   most 0.5. Only then calculate and report Overall Band from the four ratings.

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

EssayPilot sample-anchor alignment (mandatory):
{sample_anchors}
- Use these anchors only as calibration profiles. The official four IELTS descriptors
  remain the basis of every criterion score.

Anchor safeguards:
- A complete introduction-body-conclusion structure, a clear position, and no serious
  off-topic content make the response eligible for 6.5, but do not guarantee it; verify
  idea development and language control.
- Clear progression, varied and appropriate vocabulary, and only occasional grammar
  errors are positive evidence for 7 and must not be discounted because of minor flaws.
- Do not let one imperfect sentence erase sustained performance across the essay.
- Do not use the anchor as a substitute for the four official criteria. It is a
  calibration check to prevent score compression, followed by criterion-level scoring.

Anti-undergrading audit (mandatory before finalising scores):
- Verify every claimed language error against the exact quoted text. Never claim a
  missing article, comma, verb, or agreement marker when it is present in the essay.
- Oxford commas are optional and their absence is not a grammar or punctuation error.
- A grammatically correct simple sentence is not an error. GRA assesses the range and
  accuracy across the whole essay; do not demand that every sentence be complex.
- Relevant explanation, causal development, and realistic hypothetical examples all
  count as support. Task Response Band 7 does not require statistics, named studies,
  real-world case names, or a concrete example after every claim.
- A conclusion may appropriately summarise and restate the position. It does not need a
  prediction, recommendation, or new final thought to reach Band 7.
- Repetition of unavoidable topic terms is normal lexical cohesion. Penalise repetition
  only when avoidable basic wording is intrusive across the essay or shows genuinely
  limited range; do not demand unnatural synonyms for key topic nouns.
- Do not label vocabulary limited before checking for precise topic language,
  controlled collocations, and less-common items across the whole essay.
- Do not label complex grammar limited before identifying the actual conditionals,
  subordinate clauses, relative clauses, concessive clauses, and coordinated structures.
- Advice about reaching Band 7.5 belongs only in the coaching sections. Never judge the
  current essay against Band 7.5 requirements when assigning its present band.

Calibration profiles:
- A response that addresses all parts, maintains a clear position, extends each main idea
  through explanation or relevant illustration, progresses logically, uses varied and
  reasonably precise vocabulary, and contains a controlled mix of simple and complex
  sentences with only occasional non-impeding errors should receive about Band 7.
- If that profile is mostly met but one or two criteria remain at Band 6, use 6.5 rather
  than collapsing the entire response to 6.
- Reserve Overall Band 6 for responses with noticeable descriptor-level limitations in
  multiple criteria, not merely a handful of debatable style improvements.
- Reserve any Band 8 criterion rating for clear Band 8 evidence: sufficiently developed
  task response; well-managed cohesion throughout; wide, fluent, flexible and precise
  vocabulary; or a wide grammatical range with the majority of sentences error-free.
  Safe, competent academic writing that is not consistently above Band 7 should remain 7.

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
Every Likely Score in this table must be a whole number from 0 to 9, never a half band.

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


def build_structured_grading_prompt(task_type: str, topic: str, essay: str) -> str:
    """Build the Task 2 scoring prompt for strict JSON-schema output."""
    if task_type != "Task 2":
        raise ValueError("EssayPilot V2 currently supports IELTS Writing Task 2 only.")

    base_prompt = build_grading_prompt(task_type=task_type, topic=topic, essay=essay)
    scoring_policy = base_prompt.split("Fixed output structure:", 1)[0].rstrip()
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", essay.lower())
    stopwords = {
        "about", "after", "again", "also", "because", "before", "being", "both",
        "could", "every", "first", "from", "have", "however", "into", "more",
        "other", "should", "some", "their", "there", "these", "they", "this",
        "those", "very", "which", "while", "with", "would",
    }

    def lexical_root(word: str) -> str:
        if len(word) > 5 and word.endswith("ing"):
            return word[:-3]
        if len(word) > 4 and word.endswith("ies"):
            return word[:-3] + "y"
        if len(word) > 4 and word.endswith("s"):
            return word[:-1]
        return word

    content_roots = [lexical_root(word) for word in words if len(word) >= 4 and word not in stopwords]
    repeated = [(word, count) for word, count in Counter(content_roots).most_common(12) if count >= 3]
    diagnostics = (
        f"word_count={len(words)}; paragraphs={len([p for p in essay.splitlines() if p.strip()])}; "
        f"repeated_content_roots={repeated or 'none'}"
    )
    return f"""{scoring_policy}

Structured output rules:
- Return data matching the supplied JSON schema. Do not return Markdown.
- Use Chinese for every explanation or instruction: summary, criterion reason,
  next_band_limit, coaching titles/why/action, correction problems, paragraph feedback,
  training goals/tasks/requirements, expression meanings, and warnings.
- Keep learning material in English: exact evidence/original text, improved sentences,
  the Band 7.5 rewrite, expressions, examples, the next IELTS question, sentence patterns,
  and sentence-training references. Do not translate the student's English.
- Quote exact evidence from the submitted essay for every criterion.
- Every sentence_corrections.original and sentence_training.original value must be an exact
  substring of the submitted essay, without ellipses or paraphrase.
- Criterion scores must be four independent whole-band integers.
- Do not provide an overall score. EssayPilot calculates it deterministically.
- Select only the lowest one or two criteria when designing sentence and logic training.
- Keep coaching concise, specific, and useful to a Chinese IELTS learner.
- Put reusable error categories in error_tags, for example idea_development,
  mechanical_cohesion, repetition, collocation, article, agreement, or punctuation.
- Classify the essay into exactly one essay_topic_category from the schema.
- Return exactly 6-8 useful_expressions. Prefer transferable Band 6.5-7.5 chunks and
  collocations that can be reused in similar Task 2 questions; do not pad the list with
  rare words or expressions that only fit this one sentence.
- For every useful expression, keep expression and example in English, write meaning and
  usage_note in concise Chinese, and assign one function_category from the schema.
- Use the deterministic text diagnostics below as an audit, not as a substitute for the
  official descriptors. For Lexical Resource, repeated unavoidable task terms are normal,
  but avoidable basic-word repetition across several paragraphs prevents Band 7 unless the
  essay also shows enough precise, flexible alternatives. Apply this boundary consistently.

Deterministic text diagnostics:
{diagnostics}

IELTS task type:
{task_type}

Essay question:
{topic}

Student essay:
{essay}
""".strip()

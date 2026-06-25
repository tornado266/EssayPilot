# IELTS Writing AI Grader

A focused IELTS writing feedback workspace that turns one essay into a score, a revision plan, and a reusable mistake record.

This project is designed like a small learning SaaS: simple input, clear AI feedback, local progress tracking, and a clean Streamlit interface.

## What It Does

- Scores IELTS Writing essays with the four official-style criteria.
- Gives concrete feedback with quoted student sentences.
- Produces a Band 7.5-style rewrite that stays learnable for high-school students.
- Saves every correction locally as a Markdown record.
- Tracks score history so learners can see progress over time.

## Screenshot

Place screenshots here before publishing the project:

```text
screenshots/
  dashboard.png
  report.png
  history.png
```

Suggested first screenshot: the main Streamlit workspace after a completed essay correction.

## How It Works

```text
Essay Question + Student Essay
        |
        v
Streamlit Input Form
        |
        v
IELTS Examiner Prompt
        |
        v
DeepSeek API request with requests.post
        |
        v
Score Cards + Feedback Report + Local Markdown History
```

## Features

- Task 1 and Task 2 support
- Overall band score and four criteria scores
- Word count warning for IELTS minimum requirements
- Main problems with quoted original sentences
- Sentence-level corrections
- Paragraph-level feedback
- Band 7.5 rewrite
- Useful expressions for review
- Seven-day practice plan
- Local history trend chart
- Error book saved to `records/error_book.md`
- Sidebar DeepSeek connection test with latency

## Example Input

Essay question:

```text
Some people believe that university students should study whatever they like.
Others believe they should only study subjects that will be useful in the future,
such as science and technology.

Discuss both views and give your own opinion.
```

Student essay:

```text
Some people think students should choose any subject they enjoy, while others
believe they should study useful subjects. I think students should consider both
their interest and future job opportunities.
```

## Example Output

The app returns a structured report like this:

```text
Overall Band Score: 6.0-6.5

Four Criteria Scores:
- Task Response: 6.0
- Coherence and Cohesion: 6.0
- Lexical Resource: 6.5
- Grammatical Range and Accuracy: 6.0

Top Priorities:
1. Develop both views with clearer examples.
2. Improve paragraph progression.
3. Use more precise academic vocabulary.

Band 7.5 Rewrite:
Some students should be free to follow their interests, but universities also
need to help them prepare for realistic career paths...
```

Actual output depends on the essay length, quality, and the AI model response.

## Setup

### 1. Clone and enter the project

```bash
git clone https://github.com/tornado266/-7.5.git
cd ielts-writing-skill
```

If you downloaded the project as a ZIP file, just open the extracted project folder.

### 2. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS or Linux:

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Add your DeepSeek API key

Create a `.env` file in the project root:

```text
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

The DeepSeek grading request currently uses:

```text
POST https://api.deepseek.com/v1/chat/completions
```

through `requests.post`.

### 5. Run the app

```bash
streamlit run app.py
```

Open the local URL shown in the terminal, usually:

```text
http://localhost:8501
```

## Recommended Demo Flow

1. Start Streamlit.
2. Click `Test DeepSeek Connection` in the sidebar.
3. Paste an IELTS question and essay.
4. Click `Grade My Essay`.
5. Review the score cards, feedback report, saved Markdown record, and history trend.

## Project Structure

```text
ielts-writing-skill/
  app.py
  requirements.txt
  README.md
  src/
    ai_grader.py
    error_book.py
    prompts.py
    storage.py
    text_utils.py
  records/
  screenshots/
```

## FAQ

### Why does the API key not work?

Check that `.env` is in the project root and contains `DEEPSEEK_API_KEY`. After editing `.env`, restart Streamlit so the app reloads the key.

### Why does the request fail even though the page opens?

The page can load even if the AI API request fails. Use `Test DeepSeek Connection` in the sidebar. Also check your network, DeepSeek account balance, and whether port `8501` is already occupied by another Streamlit process.

### Why is the score not always exactly the same?

AI scoring is probabilistic. The report should be treated as guided practice feedback, not an official IELTS result. For more stable practice, compare trends across several essays instead of relying on one score.

### Why does the page look strange in the browser?

Browser translation plugins can modify Streamlit text and layout. Turn off translation for `localhost` if buttons or labels behave unexpectedly.

## Roadmap

- Add sample essays for quick demos
- Add export to PDF
- Add filters for grammar, vocabulary, logic, and structure issues
- Add a small screenshot gallery for portfolio presentation
- Add tests for word count and score extraction

## Tech Stack

- Python
- Streamlit
- DeepSeek API
- `requests.post` for DeepSeek grading
- OpenAI Python SDK for optional OpenAI provider
- Local Markdown files for history

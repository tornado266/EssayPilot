# IELTS Writing Correction Skill

This is a beginner-friendly IELTS Writing correction project.
It supports DeepSeek by default and can also use OpenAI.

## Project Highlights

- Portfolio-ready Streamlit web app with a bright tropical UI
- DeepSeek-first API design for lower-cost IELTS writing practice
- IELTS Task 1 and Task 2 scoring prompts with different examiner focus
- Word count warnings for IELTS minimum word requirements
- Markdown correction history saved locally
- Error book that collects grammar, vocabulary, logic, and structure issues
- Band range scoring, Band 7.5 rewrite, useful expressions, and a 7-day plan

It works like a simple "Skill":

1. You enter an IELTS Writing question.
2. You paste your essay.
3. You choose Task 1 or Task 2.
4. The app uses an AI API to grade and rewrite the essay.
5. Each correction is saved as a local Markdown file.

## What The App Can Do

- Score IELTS Writing using the four IELTS criteria
- Give an overall band score and four sub-scores
- Quote original sentences with problems
- Give paragraph-by-paragraph advice
- Rewrite the essay in a Band 7.5 style
- Generate high-scoring expressions to memorize
- Warn when Task 1 is under 150 words or Task 2 is under 250 words
- Save common mistakes to `records/error_book.md`
- Give the next practice task
- Save each correction in the `records` folder

## Screenshots

Place portfolio screenshots in this folder:

```text
screenshots/
```

Suggested screenshots:

- Home page with the Hawaii-style background
- A completed correction report
- The saved Markdown record
- The generated error book

## Project Structure

```text
ielts-writing-skill/
  app.py
  requirements.txt
  README.md
  .env.example
  screenshots/
  src/
    __init__.py
    ai_grader.py
    error_book.py
    prompts.py
    storage.py
    text_utils.py
  records/
    .gitkeep
```

## File Guide

`app.py`

The Streamlit web app. This file creates the page, text boxes, button, and result area.

`src/ai_grader.py`

This file calls the AI API.

`src/prompts.py`

This file stores the IELTS correction prompt.

`src/storage.py`

This file saves every correction as a Markdown file.

`records/`

This folder stores your correction history.

## Setup

### 1. Open the project folder

```bash
cd ielts-writing-skill
```

### 2. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
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

### 4. Set your DeepSeek API key

Windows PowerShell:

```powershell
$env:DEEPSEEK_API_KEY="your_deepseek_api_key_here"
```

macOS or Linux:

```bash
export DEEPSEEK_API_KEY="your_deepseek_api_key_here"
```

You can also copy `.env.example` to `.env` and put your API key there:

```text
DEEPSEEK_API_KEY=your_deepseek_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

### 5. Run the app

```bash
streamlit run app.py
```

Then open the local web link shown in the terminal.

## Recommended First Test

Use a short IELTS Task 2 essay first.

Example question:

```text
Some people believe that university students should study whatever they like.
Others believe they should only study subjects that will be useful in the future,
such as science and technology.

Discuss both views and give your own opinion.
```

## Learning Path For This Project

If you are new to programming, study the files in this order:

1. `README.md`
2. `app.py`
3. `src/prompts.py`
4. `src/ai_grader.py`
5. `src/storage.py`

The most important idea is separation of responsibilities:

- The web page collects user input.
- The prompt explains the IELTS correction task.
- The API module talks to DeepSeek or OpenAI.
- The storage module saves the result.

## Notes About The AI API

This project uses the official OpenAI Python SDK because DeepSeek provides an OpenAI-compatible API.
The default DeepSeek model is `deepseek-chat`.
The model name is editable in the sidebar. If a model is not available in your account, replace it with another text model you can access.

## Future Development Roadmap

- Add a history page
- Add score trend charts
- Add filters for grammar, vocabulary, logic, and structure mistakes
- Export correction records as PDF
- Add a screenshot gallery for portfolio presentation
- Add sample essays for quick demo mode
- Add tests for word count and error-book extraction
- Track band score progress over time

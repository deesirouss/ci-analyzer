# CI Failure Analyzer — Day 06 Lab 2 (Real GitHub Actions)

This repo demonstrates **Day 06 Lab 2** of the GenAI for DevOps course — the same 5-tool Gemini agent you built locally, now running as a real GitHub Actions CI pipeline.

## What Happens When You Push to Main

```
Push to main
    │
    ▼
[Job 1: build]
    npm install         ← FAILS: react@17 + react-dom@18 peer conflict
    exit code 1
    │
    ▼ (if: failure())
[Job 2: analyze-failure]
    1. read_log_file          ← downloads the failed build logs via GitHub API
    2. search_similar_failures ← checks embedded knowledge base for past fixes
    3. analyze_root_cause      ← Gemini returns structured JSON
    4. draft_pr_description    ← Gemini writes the PR body in Markdown
    5. create_github_pr        ← opens a real PR with the AI-written fix
    │
    ▼
Human reviews PR → merges when satisfied
AI never auto-merges.  ← guardrail
```

## The Intentional Failure

`package.json` pins `react@17.0.2` and `react-dom@18.0.0`. These are incompatible — `react-dom@18` requires `react@18` as a peer dependency. `npm install` fails with `ERESOLVE`.

This gives Gemini a real, specific error to analyze and a concrete fix to propose.

## The 5-Tool Agent (mirrors Day 06 Lab 2)

| # | Tool | What it does |
|---|------|-------------|
| 1 | `read_log_file` | Reads the downloaded CI log (last 6000 chars) |
| 2 | `search_similar_failures` | Checks embedded knowledge base — mirrors Day 05 RAG |
| 3 | `analyze_root_cause` | Gemini → structured JSON: error_type, root_cause, fix_command, severity |
| 4 | `draft_pr_description` | Gemini → Markdown PR body with Problem/Root Cause/Fix/Verify |
| 5 | `create_github_pr` | Creates a real branch + commit + PR via `gh pr create` |

## Setup

**One secret required** — set it in your repo → Settings → Secrets → Actions:

| Secret | Value |
|--------|-------|
| `GEMINI_API_KEY` | Your Gemini API key from [aistudio.google.com](https://aistudio.google.com) |

`GITHUB_TOKEN` is provided automatically by GitHub Actions. No setup needed.

## Tech Stack

| Component | Version |
|-----------|---------|
| Python | 3.14 |
| google-genai | 2.20.0 |
| Gemini model | gemini-2.0-flash |
| Node.js | 24 LTS |
| actions/checkout | v7.0.1 |
| actions/setup-python | v7.0.0 |
| actions/setup-node | v7.0.0 |

## Guardrail

The AI **proposes** the fix. A human **reviews and merges** the PR. The agent never calls `git push --force`, never merges its own PR, and every PR description ends with:

> ⚠️ This fix was proposed by AI analysis. Review before merging.

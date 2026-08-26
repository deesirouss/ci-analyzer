"""
Day 06 — Lab 2 (GitHub Actions version)
CI Failure Analyzer: Gemini cloud agent — 5-tool agentic loop

Principles applied from each day:
  Day 02 — Token economics: smart log extraction, not blind truncation
  Day 03 — Structured output: every tool returns JSON where possible
  Day 04 — Tool calling: 5-tool Gemini AFC agent
  Day 05 — RAG: embedded knowledge base mirrors ChromaDB search_similar_failures

Tools (same order as Day 06 Lab 2):
  1. read_log_file          — smart extraction: error lines only, not full log
  2. search_similar_failures — embedded knowledge base, returns structured JSON
  3. analyze_root_cause      — Gemini structured JSON (response_mime_type=application/json)
  4. draft_pr_description    — Gemini Markdown PR body
  5. create_github_pr        — gh pr create (human approval guardrail)

Guardrail: AI proposes. Human reviews and merges. AI never auto-merges.
"""

import json
import os
import random
import re
import subprocess
import sys
import time

from google import genai
from google.genai import types

print("=== Day 06 — CI Failure Analyzer (Cloud: gemini-3.6-flash) ===")
print()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RUN_ID = os.environ.get("RUN_ID", "unknown")
REPO = os.environ.get("REPO", "")
GITHUB_ACTOR = os.environ.get("GITHUB_ACTOR", "ci-bot")
MODEL = "gemini-3.6-flash"
LOG_FILE = "ci_failure.log"

# Token estimate: ~4 chars per token (GPT-family heuristic, close enough for Gemini)
CHARS_PER_TOKEN = 4

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY is not set. Add it as a GitHub Actions secret.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)


# ─── Retry wrapper (free tier: ~15 RPM) ──────────────────────────────────────

def with_retry(fn, max_retries=5):
    """Exponential backoff retry for Gemini rate limit errors (429 / RESOURCE_EXHAUSTED)."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            is_rate_limit = (
                "429" in err
                or "RESOURCE_EXHAUSTED" in err
                or "quota" in err.lower()
            )
            if is_rate_limit and attempt < max_retries - 1:
                wait = min(60.0, (2 ** attempt) + random.uniform(0, 1))
                print(f"  Rate limited — waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Gemini call failed after {max_retries} retries")


# ─── Embedded knowledge base ─────────────────────────────────────────────────
# Mirrors Day 05 search_similar_failures (ChromaDB + nomic-embed-text).
# In CI: keyword scoring replaces cosine similarity — no Ollama or vector DB needed.

KNOWN_FAILURES = [
    {
        "keywords": ["eresolve", "peer", "dependency", "npm", "react"],
        "error": "npm ERESOLVE peer dependency conflict",
        "category": "nodejs",
        "past_fix": (
            "Align both packages to the same major version. "
            "Example: react@17 + react-dom@18 → fix both to react@18 + react-dom@18. "
            "Alternatively use --legacy-peer-deps as a temporary workaround."
        ),
    },
    {
        "keywords": ["ecr", "cannotpull", "ecs", "private subnet", "nat"],
        "error": "ECS task failed to pull container image from ECR",
        "category": "aws-ecs",
        "past_fix": (
            "Add a NAT gateway to the private subnet route table "
            "or move the ECS task to a public subnet with auto-assign public IP."
        ),
    },
    {
        "keywords": ["modulenotfounderror", "pytest", "venv", "activate"],
        "error": "pytest ModuleNotFoundError — venv not activated in CI",
        "category": "python",
        "past_fix": (
            "Run pytest via .venv/bin/pytest or add "
            "`source .venv/bin/activate` before the test step in the pipeline YAML."
        ),
    },
    {
        "keywords": ["403", "resource not accessible", "pull-requests", "github_token"],
        "error": "GitHub Actions 403 — GITHUB_TOKEN missing pull-requests write permission",
        "category": "github-actions",
        "past_fix": (
            "Add a permissions block to the workflow job: "
            "permissions: contents: write, pull-requests: write"
        ),
    },
    {
        "keywords": ["postgres", "connection refused", "5432", "service container"],
        "error": "Integration tests fail — postgres service container not ready",
        "category": "database",
        "past_fix": (
            "Add a healthcheck to the postgres service container and "
            "add a wait step before running the test step."
        ),
    },
]

# Error signal patterns — used by read_log_file for smart extraction
ERROR_PATTERNS = re.compile(
    r"(error|err |failed|failure|exception|traceback|exit code [^0]|"
    r"cannot|fatal|warn deprecated|eresolve|enoent|permission denied)",
    re.IGNORECASE,
)


# ─── Tool 1: Smart log extraction (Day 02 Token Economics) ───────────────────

def read_log_file(filepath: str) -> str:
    """
    Read a CI failure log and extract only error-relevant lines.
    Day 02 principle: send the minimum tokens needed for accurate analysis.
    Strategy: keep every line that contains an error signal + 1 line of context before it.
    Returns a JSON string with the extracted content and token economics stats.
    """
    try:
        with open(filepath, "r", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return json.dumps({
            "status": "error",
            "message": f"Log file not found at '{filepath}'",
            "content": "",
        })

    full_chars = sum(len(l) for l in lines)
    full_tokens = full_chars // CHARS_PER_TOKEN

    # Extract error-relevant lines + 1 line of context before each hit
    kept = set()
    for i, line in enumerate(lines):
        if ERROR_PATTERNS.search(line):
            if i > 0:
                kept.add(i - 1)
            kept.add(i)

    extracted_lines = [lines[i] for i in sorted(kept)]

    # Hard cap: if extraction is still large, take the last 80 error lines
    # (tail bias: the final error is always the most useful)
    if len(extracted_lines) > 80:
        extracted_lines = extracted_lines[-80:]

    extracted = "".join(extracted_lines)
    extracted_chars = len(extracted)
    extracted_tokens = extracted_chars // CHARS_PER_TOKEN
    saved_tokens = full_tokens - extracted_tokens

    print(f"  [read_log_file] Full log   : {len(lines)} lines | {full_chars} chars | ~{full_tokens} tokens")
    print(f"  [read_log_file] Extracted  : {len(extracted_lines)} lines | {extracted_chars} chars | ~{extracted_tokens} tokens")
    print(f"  [read_log_file] Token saved: ~{saved_tokens} tokens ({100 * saved_tokens // max(full_tokens, 1)}% reduction)")

    return json.dumps({
        "status": "ok",
        "token_economics": {
            "full_log_tokens": full_tokens,
            "extracted_tokens": extracted_tokens,
            "tokens_saved": saved_tokens,
            "reduction_pct": 100 * saved_tokens // max(full_tokens, 1),
        },
        "content": extracted,
    })


# ─── Tool 2: Knowledge base search (Day 05 RAG) ──────────────────────────────

def search_similar_failures(query: str) -> str:
    """
    Search the CI failure knowledge base for similar past failures and proven fixes.
    Call this BEFORE analyze_root_cause. Pass a short description of the current error.
    Mirrors Day 05 search_similar_failures (ChromaDB cosine similarity → keyword scoring here).
    Returns structured JSON: matched, score, error, category, past_fix.
    """
    print(f"  [search_similar_failures] query: {query}")
    query_lower = query.lower()

    best_match = None
    best_score = 0
    for entry in KNOWN_FAILURES:
        score = sum(1 for k in entry["keywords"] if k in query_lower)
        if score > best_score:
            best_score = score
            best_match = entry

    if best_match and best_score >= 1:
        max_possible = len(best_match["keywords"])
        similarity = round(best_score / max_possible, 2)
        print(
            f"  [search_similar_failures] match: '{best_match['error']}' "
            f"score={best_score}/{max_possible} similarity={similarity}"
        )
        return json.dumps({
            "matched": True,
            "similarity": similarity,
            "error": best_match["error"],
            "category": best_match["category"],
            "past_fix": best_match["past_fix"],
        })

    print("  [search_similar_failures] No match found")
    return json.dumps({"matched": False, "similarity": 0.0, "error": None, "past_fix": None})


# ─── Tool 3: Root cause analysis (Day 03 Structured Output) ──────────────────

def analyze_root_cause(log_content: str, past_failures: str = "") -> str:
    """
    Analyze a CI failure log and return structured JSON with root cause and fix.
    Day 03 principle: use response_mime_type=application/json for guaranteed JSON output.
    Pass the extracted log content and the past_failures JSON from search_similar_failures.
    Returns JSON: error_type, root_cause, affected_file, fix_command, severity, confidence.
    """
    print("  [analyze_root_cause] Calling Gemini for structured JSON analysis...")

    # Parse past_failures if it's a JSON string so we can include it cleanly
    past_context = ""
    if past_failures:
        try:
            pf = json.loads(past_failures)
            if pf.get("matched"):
                past_context = (
                    f"\nKnowledge base match (similarity: {pf['similarity']}):\n"
                    f"Past failure: {pf['error']} [{pf['category']}]\n"
                    f"Proven fix: {pf['past_fix']}"
                )
        except json.JSONDecodeError:
            past_context = f"\nPast failures context:\n{past_failures}"

    # Parse log_content if it's a JSON string from read_log_file
    log_text = log_content
    try:
        lc = json.loads(log_content)
        if isinstance(lc, dict) and "content" in lc:
            log_text = lc["content"]
    except (json.JSONDecodeError, TypeError):
        pass

    def call():
        return client.models.generate_content(
            model=MODEL,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are a senior DevOps engineer. "
                    "Return ONLY a valid JSON object. No markdown fences, no text outside the JSON."
                ),
                response_mime_type="application/json",
            ),
            contents=f"""Analyze this CI failure log. Return JSON with exactly these keys:
{{
  "error_type": "short label e.g. npm-dependency, docker-build, test-failure, github-actions",
  "root_cause": "one clear sentence explaining what caused the failure",
  "affected_file": "the exact file or config that needs to change",
  "fix_command": "the exact command or minimal code change that fixes it",
  "severity": "low or medium or high",
  "confidence": "high or medium or low — how confident you are in this analysis"
}}

CI failure log:
{log_text}
{past_context}""",
        )

    response = with_retry(call)
    raw = response.text.strip()

    # Strip code fences defensively (Gemini sometimes ignores response_mime_type)
    if raw.startswith("```"):
        lines = raw.split("\n")
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        raw = "\n".join(inner).strip()

    try:
        parsed = json.loads(raw)
        print(f"  [analyze_root_cause] error_type : {parsed.get('error_type')}")
        print(f"  [analyze_root_cause] severity   : {parsed.get('severity')} | confidence: {parsed.get('confidence')}")
        print(f"  [analyze_root_cause] fix_command: {str(parsed.get('fix_command', ''))[:120]}")
    except json.JSONDecodeError:
        print("  [analyze_root_cause] Warning: invalid JSON — returning raw text")

    return raw


# ─── Tool 4: PR description (Markdown structured output) ─────────────────────

def draft_pr_description(analysis_json: str) -> str:
    """
    Write a GitHub PR description in Markdown from the root cause analysis JSON.
    Structure: Problem, Root Cause, Proposed Fix, How to Verify.
    Ends with the AI guardrail notice. Keep under 250 words.
    """
    print("  [draft_pr_description] Calling Gemini...")

    def call():
        return client.models.generate_content(
            model=MODEL,
            contents=f"""Write a GitHub PR description in Markdown for this CI failure fix.

Use exactly this structure:

## Problem
(what failed and where — one short paragraph)

## Root Cause
(one sentence — use the root_cause from the analysis)

## Proposed Fix
(the exact fix_command and which file to change — use a code block)

## How to Verify
(2–3 bullet points to confirm the fix works)

If the analysis shows confidence=low, add a warning section:
## ⚠️ Low Confidence
(note that manual review is especially important)

Reference past similar failures from the knowledge base if the analysis mentions them.
Keep the entire description under 250 words.
End with exactly this line on its own:

⚠️ This fix was proposed by AI analysis. Review before merging.

Analysis JSON:
{analysis_json}""",
        )

    response = with_retry(call)
    result = response.text.strip()
    print(f"  [draft_pr_description] {len(result)} chars | ~{len(result) // CHARS_PER_TOKEN} tokens")
    return result


# ─── Tool 5: Create GitHub PR (human approval guardrail) ─────────────────────

def create_github_pr(pr_title: str, pr_description: str) -> str:
    """
    Create a GitHub PR with the AI-generated analysis as the description.
    GUARDRAIL: AI proposes the fix. Human reviews and merges. AI never auto-merges.
    Steps: configure git → create branch → commit analysis file → push → gh pr create.
    Returns structured JSON: status, pr_url or error.
    """
    print(f"  [create_github_pr] branch: fix/ai-analysis-{RUN_ID}")
    branch = f"fix/ai-analysis-{RUN_ID}"

    # Configure git identity
    subprocess.run(
        ["git", "config", "user.email", f"{GITHUB_ACTOR}@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(["git", "config", "user.name", GITHUB_ACTOR], check=True)

    # Create branch — handle re-runs where branch already exists
    result = subprocess.run(["git", "checkout", "-b", branch], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [create_github_pr] Branch already exists — checking out")
        subprocess.run(["git", "checkout", branch], check=True)

    # Write the AI analysis as a committed file so the PR has a real diff
    analysis_path = "ci_analysis.md"
    with open(analysis_path, "w") as f:
        f.write(f"# CI Failure Analysis — Run {RUN_ID}\n\n")
        f.write(pr_description)

    subprocess.run(["git", "add", analysis_path], check=True)

    # Skip empty commit on re-runs
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
    if diff.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", f"ci: AI analysis for failed run {RUN_ID}"],
            check=True,
        )

    subprocess.run(["git", "push", "origin", branch, "--force-with-lease"], check=True)

    # Check if PR already exists for this branch
    existing = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--json", "number,url", "--jq", ".[0]"],
        capture_output=True,
        text=True,
    )
    if existing.stdout.strip() and existing.stdout.strip() != "null":
        try:
            ex = json.loads(existing.stdout.strip())
            print(f"  [create_github_pr] PR #{ex['number']} already exists — {ex['url']}")
            return json.dumps({"status": "exists", "pr_url": ex["url"], "pr_number": ex["number"]})
        except (json.JSONDecodeError, KeyError):
            pass

    # Create the PR
    pr_result = subprocess.run(
        [
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_description,
            "--base", "main",
            "--head", branch,
        ],
        capture_output=True,
        text=True,
    )

    if pr_result.returncode == 0:
        pr_url = pr_result.stdout.strip()
        print(f"  [create_github_pr] ✅ PR created: {pr_url}")
        return json.dumps({"status": "created", "pr_url": pr_url, "branch": branch})

    error = pr_result.stderr.strip()
    print(f"  [create_github_pr] ❌ Failed: {error}")
    return json.dumps({"status": "failed", "error": error})


# ─── Gemini Chat AFC loop ─────────────────────────────────────────────────────
# Uses client.chats.create() + chat.send_message() — recommended AFC pattern.
# Fixes the "Direct use of AFC in Models.generate_content is not recommended" warning.

SYSTEM_INSTRUCTION = (
    "You are a CI failure analyzer agent running inside GitHub Actions. "
    "Apply Day 02 token economics: read_log_file returns structured JSON with a 'content' field — use that field when calling analyze_root_cause. "
    "Apply Day 03 structured output: analyze_root_cause returns JSON — pass the raw JSON string to draft_pr_description. "
    "Apply Day 05 RAG: search_similar_failures returns a similarity score and past fix — pass the full JSON string as past_failures to analyze_root_cause. "
    "Use tools in this exact order — never skip a step: "
    "1. read_log_file — filepath='ci_failure.log', "
    "2. search_similar_failures — pass a short error description as query (e.g. 'npm peer dependency conflict'), "
    "3. analyze_root_cause — pass log_content=<content field from step 1>, past_failures=<full JSON string from step 2>, "
    "4. draft_pr_description — pass analysis_json=<full JSON string from step 3>, "
    "5. create_github_pr — pr_title must start with 'fix:' and be under 72 chars, pr_description=<string from step 4>. "
    "Never auto-merge. Never skip create_github_pr."
)

config = types.GenerateContentConfig(
    system_instruction=SYSTEM_INSTRUCTION,
    tools=[
        read_log_file,
        search_similar_failures,
        analyze_root_cause,
        draft_pr_description,
        create_github_pr,
    ],
)

print("Starting Gemini agentic loop (Chat AFC — recommended pattern)...\n")

chat = client.chats.create(model=MODEL, config=config)

response = with_retry(
    lambda: chat.send_message(
        "Read ci_failure.log, search our knowledge base for similar past failures, "
        "analyze the root cause using that context, "
        "draft a PR description, then create a GitHub PR with the fix."
    )
)

print(f"\n✅ Agent run complete.")
if response.text:
    print(f"Final response:\n{response.text[:400]}")

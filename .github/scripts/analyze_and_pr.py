"""
Day 06 — Lab 2 (GitHub Actions version)
CI Failure Analyzer: Gemini cloud agent — 5-tool agentic loop

Tools (same order as Day 06 Lab 2):
  1. read_log_file          — read downloaded CI failure log
  2. search_similar_failures — embedded knowledge base (mirrors Day 05 RAG)
  3. analyze_root_cause      — Gemini structured JSON analysis
  4. draft_pr_description    — Gemini PR body writer
  5. create_github_pr        — gh pr create (human approval guardrail)

Guardrail: AI proposes. Human reviews and merges. AI never auto-merges.
"""

import json
import os
import subprocess
import sys

from google import genai
from google.genai import types

print("=== Day 06 — CI Failure Analyzer (Cloud: gemini-2.0-flash) ===")
print()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RUN_ID = os.environ.get("RUN_ID", "unknown")
REPO = os.environ.get("REPO", "")
GITHUB_ACTOR = os.environ.get("GITHUB_ACTOR", "ci-bot")
MODEL = "gemini-2.0-flash"

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY not set")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

# ─── Embedded knowledge base ─────────────────────────────────────────────────
# Mirrors Day 05 RAG search_similar_failures. In production this is ChromaDB.
# In CI we ship a compact lookup dict so no vector DB setup is needed.
KNOWN_FAILURES = [
    {
        "error": "npm ERESOLVE peer dependency conflict",
        "past_fix": "Pin both packages to the same major version or use --legacy-peer-deps",
        "example": "react@17 + react-dom@18 → fix: align both to react@18 + react-dom@18",
    },
    {
        "error": "Docker build COPY file not found",
        "past_fix": "Check .dockerignore is not excluding the required file",
        "example": "COPY dist/ /app/dist/ fails when dist/ is in .dockerignore",
    },
    {
        "error": "Jest test timeout exceeded",
        "past_fix": "Increase testTimeout in jest.config.js or mock slow network calls",
        "example": "Integration tests calling real APIs timeout in CI (no VPN)",
    },
]


# ─── Tool 1 ──────────────────────────────────────────────────────────────────

def read_log_file(filepath: str) -> str:
    """Read a CI failure log file from disk. Returns the last 6000 characters."""
    try:
        with open(filepath, "r") as f:
            content = f.read()
        tail = content[-6000:]
        print(f"  [read_log_file] {len(content)} bytes read, using last {len(tail)} chars")
        return tail
    except FileNotFoundError:
        return f"Error: file not found at {filepath}"


# ─── Tool 2 ──────────────────────────────────────────────────────────────────

def search_similar_failures(query: str) -> str:
    """
    Search the CI failure knowledge base for similar past failures and their fixes.
    Call this BEFORE analyze_root_cause. Pass a short description of the current error.
    Returns the closest matching past failure with its fix.
    """
    print(f"  [search_similar_failures] query: {query}")
    query_lower = query.lower()
    for entry in KNOWN_FAILURES:
        keywords = entry["error"].lower().split()
        if any(k in query_lower for k in keywords):
            result = (
                f"Past failure: {entry['error']}\n"
                f"Known fix: {entry['past_fix']}\n"
                f"Example: {entry['example']}"
            )
            print(f"  [search_similar_failures] match found: {entry['error']}")
            return result
    return "No similar past failure found in knowledge base."


# ─── Tool 3 ──────────────────────────────────────────────────────────────────

def analyze_root_cause(log_content: str, past_failures: str = "") -> str:
    """
    Analyze a CI failure log and return structured JSON with root cause and fix.
    Accepts optional past_failures context from search_similar_failures.
    Returns JSON: error_type, root_cause, affected_file, fix_command, severity.
    """
    print("  [analyze_root_cause] Calling Gemini for structured JSON analysis...")
    context_section = f"\nPast similar failures:\n{past_failures}" if past_failures else ""

    response = client.models.generate_content(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction="You are a DevOps engineer. Return ONLY valid JSON. No text outside the JSON object.",
            response_mime_type="application/json",
        ),
        contents=f"""Analyze this CI failure log. Return JSON with exactly these keys:
{{
  "error_type": "short label e.g. npm-dependency, docker-build, test-failure",
  "root_cause": "one clear sentence",
  "affected_file": "file or config that needs to change",
  "fix_command": "exact command or change to fix it",
  "severity": "low or medium or high"
}}

Log:
{log_content}
{context_section}""",
    )

    raw = response.text.strip()
    # Strip code fences if Gemini wraps in ```json blocks
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
        print(f"  [analyze_root_cause] error_type : {parsed.get('error_type')}")
        print(f"  [analyze_root_cause] root_cause : {parsed.get('root_cause', '')[:100]}")
        print(f"  [analyze_root_cause] fix_command: {parsed.get('fix_command', '')[:100]}")
    except json.JSONDecodeError:
        print("  [analyze_root_cause] Warning: Gemini returned invalid JSON — using raw text")

    return raw


# ─── Tool 4 ──────────────────────────────────────────────────────────────────

def draft_pr_description(analysis_json: str) -> str:
    """
    Draft a GitHub PR description in Markdown from the root cause analysis JSON.
    Includes: what failed, root cause, proposed fix, and how to verify.
    Ends with the AI guardrail notice.
    """
    print("  [draft_pr_description] Calling Gemini...")

    response = client.models.generate_content(
        model=MODEL,
        contents=f"""Write a GitHub PR description in Markdown for this CI failure fix.

Structure it exactly like this:
## Problem
(what failed and where)

## Root Cause
(one clear sentence)

## Proposed Fix
(the exact fix_command and what file to change)

## How to Verify
(steps to confirm the fix works)

If past similar failures are mentioned in the analysis, reference them.
Keep it under 250 words. End with exactly this line:
⚠️ This fix was proposed by AI analysis. Review before merging.

Analysis:
{analysis_json}""",
    )

    result = response.text
    print(f"  [draft_pr_description] {len(result)} chars drafted")
    return result


# ─── Tool 5 ──────────────────────────────────────────────────────────────────

def create_github_pr(pr_title: str, pr_description: str) -> str:
    """
    Create a GitHub PR with the AI-generated analysis as the description.
    GUARDRAIL: AI proposes. Human reviews and merges. AI never auto-merges.
    Steps: configure git → create branch → write analysis file → commit → push → gh pr create.
    """
    print(f"  [create_github_pr] Creating branch and PR...")
    branch = f"fix/ai-analysis-{RUN_ID}"

    # Configure git identity
    subprocess.run(["git", "config", "user.email", f"{GITHUB_ACTOR}@users.noreply.github.com"], check=True)
    subprocess.run(["git", "config", "user.name", GITHUB_ACTOR], check=True)

    # Create and switch to new branch
    subprocess.run(["git", "checkout", "-b", branch], check=True)

    # Write the AI analysis as a file so the PR has something to commit
    analysis_path = "ci_analysis.md"
    with open(analysis_path, "w") as f:
        f.write(f"# CI Failure Analysis — Run {RUN_ID}\n\n")
        f.write(pr_description)

    subprocess.run(["git", "add", analysis_path], check=True)
    subprocess.run(
        ["git", "commit", "-m", f"ci: AI analysis for failed run {RUN_ID}"],
        check=True,
    )
    subprocess.run(["git", "push", "origin", branch], check=True)

    # Create the PR
    result = subprocess.run(
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

    if result.returncode == 0:
        pr_url = result.stdout.strip()
        print(f"  [create_github_pr] ✅ PR created: {pr_url}")
        return f"PR created: {pr_url}"
    else:
        error = result.stderr.strip()
        print(f"  [create_github_pr] ❌ gh pr create failed: {error}")
        return f"PR creation failed: {error}"


# ─── Gemini agentic loop (mirrors Day 06 Lab 2 exactly) ──────────────────────

TOOL_FUNCS = {
    "read_log_file": read_log_file,
    "search_similar_failures": search_similar_failures,
    "analyze_root_cause": analyze_root_cause,
    "draft_pr_description": draft_pr_description,
    "create_github_pr": create_github_pr,
}

tools = [
    read_log_file,
    search_similar_failures,
    analyze_root_cause,
    draft_pr_description,
    create_github_pr,
]

messages = [
    types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    "Read ci_failure.log, search for similar past failures, "
                    "analyze the root cause using the past failures context, "
                    "draft a PR description, then create a GitHub PR with the fix. "
                    "The PR title must start with 'fix:' and be under 72 characters."
                )
            )
        ],
    )
]

config = types.GenerateContentConfig(
    system_instruction=(
        "You are a CI failure analyzer agent running in GitHub Actions. "
        "Use tools in this exact order: "
        "1. read_log_file — filepath is 'ci_failure.log', "
        "2. search_similar_failures — pass a short description of the error type as query, "
        "3. analyze_root_cause — pass the log content AND the past_failures result from step 2, "
        "4. draft_pr_description — pass the analysis JSON from step 3, "
        "5. create_github_pr — pass pr_title (from analysis fix summary) and pr_description (from step 4). "
        "Never skip a step. Never auto-merge."
    ),
    tools=tools,
)

print("Starting Gemini agentic loop...\n")
while True:
    response = client.models.generate_content(model=MODEL, config=config, contents=messages)
    candidate = response.candidates[0]
    messages.append(types.Content(role="model", parts=candidate.content.parts))

    has_function_call = False
    function_responses = []

    for part in candidate.content.parts:
        if part.function_call:
            has_function_call = True
            name = part.function_call.name
            args = dict(part.function_call.args)
            print(f"\n  → Tool: {name}")
            try:
                result = TOOL_FUNCS[name](**args)
            except Exception as exc:
                result = f"Tool error: {exc}"
                print(f"  [ERROR] {exc}")
            function_responses.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=name,
                        response={"result": str(result)},
                    )
                )
            )

    if not has_function_call:
        final_text = ""
        for part in candidate.content.parts:
            if hasattr(part, "text") and part.text:
                final_text = part.text
        print(f"\n✅ Agent run complete.")
        if final_text:
            print(f"Final response: {final_text[:300]}")
        break

    messages.append(types.Content(role="user", parts=function_responses))

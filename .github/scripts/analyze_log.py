"""
CI Failure Analyzer — Step 1 of 2: Log Analysis

Runs inside the 'analyze-failure' job, Step 1.

Applies:
  Day 02 — Token economics: extract error lines only, print savings vs full log
  Day 03 — Structured output: Gemini returns JSON (response_mime_type=application/json)
  Day 05 — RAG: keyword-scored knowledge base (mirrors ChromaDB search_similar_failures)

Pipeline (deterministic — no AI orchestration):
  1. read_log_file            → extract error lines, compute token savings
  2. search_similar_failures  → score against knowledge base, return best match
  3. call_gemini              → ONE call returns structured JSON analysis AND
                                pr_description Markdown (combined to halve API usage)
  4. write analysis.json      → consumed by create_pr.py in Step 2

No git or GitHub operations occur in this file.
"""

import json
import os
import random
import re
import sys
import time
import warnings

# Suppress AFC warning: fires on import even when AFC is not used.
# We call models.generate_content() directly — stateless, one-shot, no tools.
warnings.filterwarnings("ignore", message=".*automatic function calling.*")

from google import genai
from google.genai import types


# ─── Configuration ─────────────────────────────────────────────────────────────

CHARS_PER_TOKEN = 4  # rough estimate: 1 token ≈ 4 chars

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RUN_ID = os.environ.get("RUN_ID", "unknown")
LOG_FILE = "ci_failure.log"
ANALYSIS_FILE = "analysis.json"

# Model priority list — if the primary model hits its daily RPD quota,
# the next model in the list is tried automatically.
# Override the primary via GitHub Actions Variable GEMINI_MODEL (plain text,
# visible in the UI — not a Secret):
# Repo → Settings → Secrets and variables → Actions → Variables → GEMINI_MODEL
#
# Confirmed free-tier API model IDs (from AI Studio dashboard):
#   gemini-3.1-flash-lite   500 RPD, 15 RPM   ← default primary
#   gemini-3.5-flash-lite   500 RPD, 15 RPM   ← fallback 1
#   gemini-3.5-flash         20 RPD,  5 RPM   ← fallback 2
#   gemini-3.6-flash         20 RPD,  5 RPM   ← fallback 3
_env_model = os.environ.get("GEMINI_MODEL") or "gemini-3.1-flash-lite"
_candidates = [
    _env_model,
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
]
_seen: set = set()
MODEL_PRIORITY = [m for m in _candidates if m and not (m in _seen or _seen.add(m))]
MODEL = MODEL_PRIORITY[0]

DIVIDER = "─" * 62


# ═══════════════════════════════════════════════════════════════════════════════
# SIDECAR — Display helpers
# ═══════════════════════════════════════════════════════════════════════════════

def header(step_num, title):
    """Print a numbered step header to CI log output."""
    print(f"\n{'━' * 62}")
    print(f"  STEP {step_num} — {title}")
    print(f"{'━' * 62}")


def kv(key, value, indent=2):
    """Print a key-value pair with aligned columns for CI log readability."""
    print(f"{' ' * indent}{key:<20}: {value}")


# ═══════════════════════════════════════════════════════════════════════════════
# SIDECAR — Gemini API retry handler
# ═══════════════════════════════════════════════════════════════════════════════

def with_retry(fn, label="Gemini", max_retries=5):
    """
    Call fn() and retry on transient Gemini API errors.

    Handles per-minute rate limits (429 RPM) and server overload (503).
    Does NOT catch daily quota exhaustion (RPD) — that triggers a model switch
    in the caller, not a retry of the same model.

    When the API response includes a "retry in Xs" hint, that exact wait time
    is used (plus a 2-second buffer). Otherwise, exponential backoff is applied.

    Args:
        fn:          zero-argument callable that makes the API call
        label:       identifier shown in log output
        max_retries: maximum attempts before re-raising the last exception

    Returns:
        The return value of fn() on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            is_retryable = (
                "429" in err
                or "503" in err
                or "RESOURCE_EXHAUSTED" in err
                or "UNAVAILABLE" in err
                or "quota" in err.lower()
                or "high demand" in err.lower()
            )
            if is_retryable and attempt < max_retries - 1:
                m = re.search(r"retry in (\d+\.?\d*)s", err)
                api_wait = float(m.group(1)) + 2.0 if m else None
                wait = api_wait if api_wait else min(60.0, (2 ** attempt) + random.uniform(0, 1))
                print(f"  ⚠️  {label} error (attempt {attempt + 1}/{max_retries}): {err}")
                print(f"  ⏳ Retrying in {wait:.1f}s{'  ← API-specified wait' if api_wait else ''}...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"{label} failed after {max_retries} retries")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — Knowledge base of known CI failure patterns
# ═══════════════════════════════════════════════════════════════════════════════

# Each entry captures a class of CI failure: the keywords that identify it in
# logs, a human-readable label, and the fix that resolved it historically.
# This is queried in Step 2 before the model call — a knowledge base hit injects
# the proven fix into the prompt, improving both accuracy and fix quality.
KNOWN_FAILURES = [
    {
        "keywords": ["eresolve", "peer", "dependency", "npm", "react"],
        "error": "npm ERESOLVE peer dependency conflict",
        "category": "nodejs",
        "past_fix": (
            "Align both packages to the same major version. "
            "Example: react@17 + react-dom@18 → both to react@18 + react-dom@18. "
            "Avoid --legacy-peer-deps in production; it masks conflicts that resurface later."
        ),
    },
    {
        "keywords": ["ecr", "cannotpull", "ecs", "private subnet", "nat"],
        "error": "ECS task failed to pull container image from ECR",
        "category": "aws-ecs",
        "past_fix": (
            "Add a NAT gateway to the private subnet route table, "
            "or move the ECS task to a public subnet with auto-assign public IP enabled."
        ),
    },
    {
        "keywords": ["modulenotfounderror", "pytest", "venv", "activate"],
        "error": "pytest ModuleNotFoundError — venv not activated in CI",
        "category": "python",
        "past_fix": (
            "Run pytest via .venv/bin/pytest or add "
            "'source .venv/bin/activate' before the test step in the pipeline YAML."
        ),
    },
    {
        "keywords": ["403", "resource not accessible", "pull-requests", "github_token"],
        "error": "GitHub Actions 403 — GITHUB_TOKEN missing pull-requests write permission",
        "category": "github-actions",
        "past_fix": (
            "Add permissions block to the workflow job: "
            "permissions: contents: write, pull-requests: write"
        ),
    },
    {
        "keywords": ["postgres", "connection refused", "5432", "service container"],
        "error": "Integration tests fail — postgres service container not ready",
        "category": "database",
        "past_fix": (
            "Add a healthcheck to the postgres service and "
            "a wait/depends-on step before running tests."
        ),
    },
]

# Regex to identify lines carrying error signal in a CI log.
# Used in Step 1 to extract only the relevant lines before sending to the model.
ERROR_PATTERNS = re.compile(
    r"(error|err |failed|failure|exception|traceback|exit code [^0]|"
    r"cannot|fatal|eresolve|enoent|permission denied|warn deprecated)",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — Knowledge base search
# ═══════════════════════════════════════════════════════════════════════════════

def search_similar_failures(log_text):
    """
    Find the closest matching past failure in KNOWN_FAILURES for the given log.

    Uses deterministic keyword extraction from specific error signals (no AI
    call) to form a search query, then scores each KNOWN_FAILURES entry by how
    many of its keywords appear in the query. The highest-scoring entry above a
    minimum threshold is returned as the match.

    Keeping this step deterministic (rather than delegating to the model) ensures
    reproducible results and avoids consuming an additional API call.

    Args:
        log_text: extracted error lines from the CI log (already filtered)

    Returns:
        dict with keys:
          matched (bool), similarity (float 0–1), score (str "x/y"),
          error (str), category (str), past_fix (str)
        If no match is found: {"matched": False, "similarity": 0.0}
    """
    lower = log_text.lower()

    # Map known error signals to targeted search queries.
    # Explicit signal matching is more reliable than generic full-text search
    # for the structured failure patterns we track.
    if "eresolve" in lower or ("peer" in lower and "npm" in lower):
        query = "npm peer dependency eresolve conflict react"
    elif "cannotpull" in lower or ("ecr" in lower and "ecs" in lower):
        query = "ecr cannotpull container image ecs private subnet nat"
    elif "modulenotfounderror" in lower:
        query = "pytest modulenotfounderror venv activate"
    elif "resource not accessible" in lower or ("403" in lower and "pull-request" in lower):
        query = "github actions 403 resource not accessible pull-requests github_token"
    elif "connection refused" in lower and "5432" in lower:
        query = "postgres connection refused 5432 service container"
    else:
        for line in log_text.splitlines():
            stripped = line.strip()
            if stripped:
                query = stripped[:100]
                break
        else:
            query = "ci pipeline failure"

    kv("Query", query)

    best_match = None
    best_score = 0
    for entry in KNOWN_FAILURES:
        score = sum(1 for k in entry["keywords"] if k in query.lower())
        if score > best_score:
            best_score = score
            best_match = entry

    if best_match and best_score >= 1:
        max_keywords = len(best_match["keywords"])
        similarity = round(best_score / max_keywords, 2)
        kv("Match", f"{best_match['error']} [{best_match['category']}]")
        kv("Similarity", f"{similarity} (score {best_score}/{max_keywords} keywords)")
        kv("Past fix", best_match["past_fix"][:90] + ("..." if len(best_match["past_fix"]) > 90 else ""))
        return {
            "matched": True,
            "similarity": similarity,
            "score": f"{best_score}/{max_keywords}",
            "error": best_match["error"],
            "category": best_match["category"],
            "past_fix": best_match["past_fix"],
        }

    kv("Match", "None found in knowledge base")
    return {"matched": False, "similarity": 0.0}


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — Gemini API call
# ═══════════════════════════════════════════════════════════════════════════════

def call_gemini(model, prompt):
    """
    Send the analysis prompt to Gemini and return the raw response object.

    Uses response_mime_type=application/json to instruct the model to return
    only valid JSON. The system instruction reinforces this constraint.

    A single call returns both the structured analysis fields (error_type,
    root_cause, fix_command, etc.) AND the PR description as an embedded
    Markdown string — one call instead of two separate analyze/draft calls.

    Args:
        model:  Gemini API model ID string (e.g. "gemini-3.1-flash-lite")
        prompt: the fully assembled prompt string including log content and
                any knowledge base context

    Returns:
        google.genai response object; access the result via .text
    """
    return client.models.generate_content(
        model=model,
        config=types.GenerateContentConfig(
            # temperature=0.0: fully deterministic output.
            # The same error always produces the same error_slug, which keeps
            # the fix branch name stable across re-runs of the same failure.
            temperature=0.0,
            system_instruction=(
                "You are a Senior DevOps Solutions Architect. "
                "Return ONLY a valid JSON object. No markdown fences, no text outside the JSON."
            ),
            response_mime_type="application/json",
        ),
        contents=prompt,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY is not set. Add it as a GitHub Actions secret.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

print(f"{'━' * 62}")
print(f"  CI FAILURE ANALYZER — Log Analysis")
print(f"  Model: {MODEL}  |  Run: {RUN_ID}")
print(f"{'━' * 62}")


# ─── Step 1: Log extraction ───────────────────────────────────────────────────
# Read the full CI log and extract only lines that carry error signal.
# Sending the full log to the model wastes tokens and buries the key error.
# We keep each matching line plus one line of context above it — the preceding
# line often identifies which step or command produced the error.

header(1, "Log Extraction")

try:
    with open(LOG_FILE, "r", errors="replace") as f:
        lines = f.readlines()
except FileNotFoundError:
    print(f"  ERROR: {LOG_FILE} not found. Did the log download step complete?")
    sys.exit(1)

full_chars = sum(len(l) for l in lines)
full_tokens = full_chars // CHARS_PER_TOKEN

kept_indices = set()
for i, line in enumerate(lines):
    if ERROR_PATTERNS.search(line):
        if i > 0:
            kept_indices.add(i - 1)
        kept_indices.add(i)

extracted_lines = [lines[i] for i in sorted(kept_indices)]
if len(extracted_lines) > 80:
    extracted_lines = extracted_lines[-80:]  # keep the most recent errors

extracted = "".join(extracted_lines)
extracted_chars = len(extracted)
extracted_tokens = extracted_chars // CHARS_PER_TOKEN
saved_tokens = full_tokens - extracted_tokens
reduction_pct = 100 * saved_tokens // max(full_tokens, 1)

kv("Source", LOG_FILE)
kv("Full log", f"{len(lines)} lines | {full_chars} chars | ~{full_tokens} tokens")
kv("Extracted", f"{len(extracted_lines)} lines | {extracted_chars} chars | ~{extracted_tokens} tokens")
kv("Tokens saved", f"~{saved_tokens} ({reduction_pct}% reduction)")
print()
print("  Extracted content:")
print(f"  {DIVIDER}")
for line in extracted_lines[:15]:
    print(f"  │ {line.rstrip()}")
if len(extracted_lines) > 15:
    print(f"  │ ... ({len(extracted_lines) - 15} more error lines)")
print(f"  {DIVIDER}")


# ─── Step 2: Knowledge base search ───────────────────────────────────────────
# Query the knowledge base for similar past failures before calling the model.
# When a match is found, the proven fix is injected into the model prompt,
# which significantly improves root cause accuracy and fix quality.

header(2, "Knowledge Base Search")
kb_result = search_similar_failures(extracted)


# ─── Step 3: Root cause analysis + PR description ────────────────────────────
# One Gemini call returns both the structured analysis (JSON fields) and the
# ready-to-use PR description (Markdown string embedded in the JSON).
# Combining both outputs into a single call avoids an extra API request.

header(3, "Root Cause Analysis + PR Description")

rag_note = (
    f"\nKnowledge base match (similarity: {kb_result['similarity']}):\n"
    f"Past failure: {kb_result['error']} [{kb_result['category']}]\n"
    f"Proven fix: {kb_result['past_fix']}"
    if kb_result.get("matched") else ""
)

combined_prompt = f"""You are a Senior DevOps Solutions Architect specializing in CI/CD reliability engineering.

Your role: diagnose CI failures and recommend long-term, maintainable fixes — not quick patches.

Rules:
1. Identify the ROOT CAUSE (what actually broke, not just which step failed)
2. Recommend the MINIMUM change that makes the system reliably correct going forward
3. Prefer explicit version pinning over floating ranges to prevent future conflicts
4. If the fix requires coordination with other teams, include that in fix_command

Return ONLY valid JSON with exactly these keys — no markdown fences, no text outside the JSON:
{{
  "error_type": "npm-dependency | docker-build | test-failure | github-actions | database | other",
  "error_slug": "kebab-case 2-4 word slug describing the specific error — used for git branch naming. Must be lowercase, hyphens only, no special chars. Examples: 'referenceerror-deploymentcount', 'eslint-config-missing', 'docker-entrypoint-not-found'. For known error_types use the type itself e.g. 'npm-dependency'.",
  "root_cause": "one sentence — the actual cause, not just which step failed",
  "affected_file": "exact file or config to change e.g. package.json",
  "fix_command": "exact command — copy-paste ready e.g. npm install react@18.0.0 react-dom@18.0.0 --save-exact",
  "severity": "low | medium | high",
  "confidence": "high | medium | low",
  "pr_title": "fix: short title under 72 chars",
  "pr_description": "## Problem\\n...\\n\\n## Root Cause\\n...\\n\\n## Proposed Fix\\n```\\n<fix_command>\\n```\\n\\n## How to Verify\\n- bullet 1\\n- bullet 2\\n\\n⚠️ This fix was proposed by AI analysis. Review before merging."
}}

Rules for pr_description:
- Use \\n for newlines inside the JSON string
- Reference the knowledge base match if present (similarity score + proven fix)
- Add a low confidence warning section if confidence is low
- End with exactly: ⚠️ This fix was proposed by AI analysis. Review before merging.
- Keep under 250 words

CI failure log (extracted error lines only — {extracted_tokens} tokens):
{extracted}
{rag_note}"""

prompt_tokens = len(combined_prompt) // CHARS_PER_TOKEN
kv("Sending to Gemini", f"~{prompt_tokens} tokens")
kv("Includes KB context", str(kb_result.get("matched", False)))
kv("Model priority", " → ".join(MODEL_PRIORITY))

# Try each model in priority order.
# Per-minute rate limits (RPM) → with_retry() waits and retries the same model.
# Per-day quota exhaustion (RPD) → detected here, switches to the next model.
response = None
used_model = None
for _model in MODEL_PRIORITY:
    print(f"  Calling {_model}...")
    try:
        response = with_retry(
            (lambda m: lambda: call_gemini(m, combined_prompt))(_model),
            label=f"analyze+draft[{_model}]",
        )
        used_model = _model
        break
    except Exception as e:
        err = str(e)
        is_daily_quota = "PerDay" in err or "GenerateRequestsPerDay" in err
        is_invalid_model = "404" in err or "NOT_FOUND" in err or "is not found" in err
        if (is_daily_quota or is_invalid_model) and _model != MODEL_PRIORITY[-1]:
            reason = "daily RPD quota exhausted" if is_daily_quota else "model ID not found"
            print(f"  ⚠️  {_model}: {reason} — switching to next model...")
            continue
        raise

if response is None:
    print(f"  ❌  All models exhausted their daily quota: {MODEL_PRIORITY}")
    sys.exit(1)

if used_model != MODEL:
    kv("Fallback used", f"{used_model}  (primary {MODEL} hit daily quota)")

raw = response.text.strip()

# Strip markdown code fences defensively — some model versions include them
# despite response_mime_type=application/json being set.
if raw.startswith("```"):
    inner = raw.split("\n")[1:-1]
    raw = "\n".join(inner).strip()

try:
    analysis = json.loads(raw)
except json.JSONDecodeError as exc:
    print(f"  ⚠️  Could not parse Gemini response as JSON: {exc}")
    print(f"  Raw response:\n{raw[:300]}")
    sys.exit(1)

pr_description = analysis.get("pr_description", "")

print()
kv("error_type", analysis.get("error_type", "—"))
kv("error_slug", analysis.get("error_slug", "—"))
kv("root_cause", str(analysis.get("root_cause", "—"))[:80])
kv("affected_file", analysis.get("affected_file", "—"))
kv("fix_command", str(analysis.get("fix_command", "—"))[:80])
kv("severity", analysis.get("severity", "—"))
kv("confidence", analysis.get("confidence", "—"))
kv("pr_title", analysis.get("pr_title", "—"))
kv("PR body length", f"{len(pr_description)} chars | ~{len(pr_description) // CHARS_PER_TOKEN} tokens")
print()
print("  PR body preview:")
print(f"  {DIVIDER}")
for line in pr_description.replace("\\n", "\n").split("\n")[:8]:
    print(f"  │ {line}")
print(f"  │ ...")
print(f"  {DIVIDER}")


# ─── Step 4: Write analysis.json ─────────────────────────────────────────────
# Persist the full analysis as JSON for create_pr.py to consume in Step 2.
# create_pr.py applies the fix and opens the PR without making any model calls.

print()
print(f"{'━' * 62}")
print(f"  SAVING ANALYSIS")
print(f"{'━' * 62}")

output = {
    "run_id": RUN_ID,
    "model": used_model,
    "error_type": analysis.get("error_type"),
    "error_slug": analysis.get("error_slug", ""),
    "root_cause": analysis.get("root_cause"),
    "affected_file": analysis.get("affected_file"),
    "fix_command": analysis.get("fix_command"),
    "severity": analysis.get("severity"),
    "confidence": analysis.get("confidence"),
    "pr_title": analysis.get("pr_title", "fix: CI failure detected by AI analysis"),
    "pr_description": pr_description.replace("\\n", "\n"),
    "knowledge_base_match": kb_result,
    "token_economics": {
        "full_log_tokens": full_tokens,
        "extracted_tokens": extracted_tokens,
        "tokens_saved": saved_tokens,
        "reduction_pct": reduction_pct,
        "gemini_calls": 1,
        "gemini_tokens_sent": prompt_tokens,
    },
}

with open(ANALYSIS_FILE, "w") as f:
    json.dump(output, f, indent=2)

kv("Written to", ANALYSIS_FILE)
kv("Gemini calls", "1")
kv("Tokens sent", f"~{prompt_tokens}")

print()
print(f"  {DIVIDER}")
print(f"  Full analysis.json:")
print(f"  {DIVIDER}")
print(json.dumps(output, indent=2))
print(f"  {DIVIDER}")
print()
print("  ✅ Analysis complete. Passing to Step 2 (create_pr.py).")

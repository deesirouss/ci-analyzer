"""
Step 1 of 2 — CI Log Analysis
Runs inside the 'analyze-failure' job, Step 1.

Applies:
  Day 02 — Token economics: extract error lines only, print savings
  Day 03 — Structured output: Gemini returns JSON (response_mime_type=application/json)
  Day 05 — RAG: keyword-scored knowledge base mirrors ChromaDB search_similar_failures

Pipeline (all deterministic — no AI orchestration needed here):
  1. read_log_file           → extract error lines, print token savings
  2. search_similar_failures → score against knowledge base, return best match
  3. analyze + draft (combined) → ONE Gemini call returns both structured JSON
                                   analysis AND the pr_description Markdown string
                                   Day 02: 1 call instead of 2 halves quota usage

Writes: analysis.json (read by create_pr.py in Step 2)
No git or GitHub operations happen here.
"""

import json
import os
import random
import re
import sys
import time
import warnings

# Suppress the google-genai SDK's AFC warning — it fires on import even when
# not using AFC. We call models.generate_content() directly (one-shot, no tools).
warnings.filterwarnings("ignore", message=".*automatic function calling.*")

from google import genai
from google.genai import types

CHARS_PER_TOKEN = 4  # ~4 chars per token — Day 02 estimation rule

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RUN_ID = os.environ.get("RUN_ID", "unknown")
LOG_FILE = "ci_failure.log"
ANALYSIS_FILE = "analysis.json"
MODEL = "gemini-3.6-flash"

DIVIDER = "─" * 62

def header(step_num, title, day_tag=""):
    tag = f"  [{day_tag}]" if day_tag else ""
    print(f"\n{'━' * 62}")
    print(f"  STEP {step_num} — {title}{tag}")
    print(f"{'━' * 62}")


def kv(key, value, indent=2):
    print(f"{' ' * indent}{key:<18}: {value}")


if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY is not set. Add it as a GitHub Actions secret.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

print(f"{'━' * 62}")
print(f"  CI FAILURE ANALYZER — Log Analysis")
print(f"  Model: {MODEL}  |  Run: {RUN_ID}")
print(f"{'━' * 62}")


# ─── Retry (catches 429 rate limit AND 503 server overload) ──────────────────

def with_retry(fn, label="Gemini", max_retries=5):
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
                # Respect the API's own retry-after value if present.
                # e.g. "Please retry in 38.572298888s" — never ignore this.
                m = re.search(r"retry in (\d+\.?\d*)s", err)
                api_wait = float(m.group(1)) + 2.0 if m else None
                wait = api_wait if api_wait else min(60.0, (2 ** attempt) + random.uniform(0, 1))
                print(f"  ⚠️  {label} error (attempt {attempt + 1}/{max_retries}): {err[:120]}")
                print(f"  ⏳ Retrying in {wait:.1f}s{'  ← API-specified wait' if api_wait else ''}...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"{label} failed after {max_retries} retries")


# ─── Embedded knowledge base (mirrors Day 05 ChromaDB collection) ────────────

KNOWN_FAILURES = [
    {
        "keywords": ["eresolve", "peer", "dependency", "npm", "react"],
        "error": "npm ERESOLVE peer dependency conflict",
        "category": "nodejs",
        "past_fix": (
            "Align both packages to the same major version. "
            "Example: react@17 + react-dom@18 → both to react@18 + react-dom@18. "
            "Avoid --legacy-peer-deps in production; it hides future conflicts."
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

ERROR_PATTERNS = re.compile(
    r"(error|err |failed|failure|exception|traceback|exit code [^0]|"
    r"cannot|fatal|eresolve|enoent|permission denied|warn deprecated)",
    re.IGNORECASE,
)


# ─── Step 1: Log extraction ───────────────────────────────────────────────────

header(1, "Log Extraction", "Day 02: Token Economics")

try:
    with open(LOG_FILE, "r", errors="replace") as f:
        lines = f.readlines()
except FileNotFoundError:
    print(f"  ERROR: {LOG_FILE} not found. Did the download step run?")
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
    extracted_lines = extracted_lines[-80:]

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

header(2, "Knowledge Base Search", "Day 05: RAG")

# Deterministic query extraction — no AI needed to figure out what to search
extracted_lower = extracted.lower()
if "eresolve" in extracted_lower or ("peer" in extracted_lower and "npm" in extracted_lower):
    kb_query = "npm peer dependency eresolve conflict react"
elif "cannotpull" in extracted_lower or ("ecr" in extracted_lower and "ecs" in extracted_lower):
    kb_query = "ecr cannotpull container image ecs private subnet nat"
elif "modulenotfounderror" in extracted_lower:
    kb_query = "pytest modulenotfounderror venv activate"
elif "resource not accessible" in extracted_lower or ("403" in extracted_lower and "pull-request" in extracted_lower):
    kb_query = "github actions 403 resource not accessible pull-requests github_token"
elif "connection refused" in extracted_lower and "5432" in extracted_lower:
    kb_query = "postgres connection refused 5432 service container"
else:
    # Fallback: first error-looking line
    for line in extracted_lines:
        stripped = line.strip()
        if stripped:
            kb_query = stripped[:100]
            break
    else:
        kb_query = "ci pipeline failure"

kv("Query", kb_query)

best_match = None
best_score = 0
for entry in KNOWN_FAILURES:
    score = sum(1 for k in entry["keywords"] if k in kb_query.lower())
    if score > best_score:
        best_score = score
        best_match = entry

if best_match and best_score >= 1:
    max_keywords = len(best_match["keywords"])
    similarity = round(best_score / max_keywords, 2)
    kv("Match", f"{best_match['error']} [{best_match['category']}]")
    kv("Similarity", f"{similarity} (score {best_score}/{max_keywords} keywords)")
    kv("Past fix", best_match["past_fix"][:90] + ("..." if len(best_match["past_fix"]) > 90 else ""))
    kb_result = {
        "matched": True,
        "similarity": similarity,
        "score": f"{best_score}/{max_keywords}",
        "error": best_match["error"],
        "category": best_match["category"],
        "past_fix": best_match["past_fix"],
    }
else:
    kv("Match", "None found in knowledge base")
    kb_result = {"matched": False, "similarity": 0.0}


# ─── Step 3: Root cause analysis ─────────────────────────────────────────────

header(3, "Root Cause Analysis", "Day 03: Structured Output")

past_context = ""
if kb_result.get("matched"):
    past_context = (
        f"\nKnowledge base match (similarity: {kb_result['similarity']}):\n"
        f"Past failure: {kb_result['error']} [{kb_result['category']}]\n"
        f"Proven fix: {kb_result['past_fix']}"
    )

# ─── Steps 3 + 4 combined: ONE Gemini call ───────────────────────────────────
# Day 02 principle: minimise API calls, not just tokens.
# Two separate calls (analyze → draft) consumed 2 of 20 free-tier daily requests.
# One combined call returns both the structured analysis AND the PR description.
# The pr_description is a Markdown string embedded in the JSON response.

header(3, "Root Cause Analysis + PR Description (combined)", "Day 02+03: 1 call, 2 outputs")

low_confidence_note = (
    "\n- If confidence must be low, add a `low_confidence_warning` field explaining why."
)
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
- Keep under 250 words{low_confidence_note}

CI failure log (extracted error lines only — {extracted_tokens} tokens):
{extracted}
{rag_note}"""

prompt_tokens = len(combined_prompt) // CHARS_PER_TOKEN
kv("Sending to Gemini", f"~{prompt_tokens} tokens  (analysis + PR body in one call)")
kv("Includes RAG context", str(kb_result.get("matched", False)))
kv("Gemini calls", "1  (was 2 — halves daily quota usage)")
print(f"  Calling {MODEL}...")

def call_combined():
    return client.models.generate_content(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are a Senior DevOps Solutions Architect. "
                "Return ONLY a valid JSON object. No markdown fences, no text outside the JSON."
            ),
            response_mime_type="application/json",
        ),
        contents=combined_prompt,
    )

response = with_retry(call_combined, label="analyze+draft")
raw = response.text.strip()

# Strip code fences defensively
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


# ─── Write analysis.json ─────────────────────────────────────────────────────

print()
print(f"{'━' * 62}")
print(f"  SAVING ANALYSIS")
print(f"{'━' * 62}")

output = {
    "run_id": RUN_ID,
    "model": MODEL,
    "error_type": analysis.get("error_type"),
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
kv("Total Gemini calls", "1  (combined analyze + draft)")
kv("Tokens sent", f"~{prompt_tokens}")
print()
print("  ✅ Analysis complete. Passing to Step 2 (create_pr.py).")

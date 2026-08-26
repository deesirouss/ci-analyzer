"""
Step 2 of 2 — Create Fix PR
Runs inside the 'analyze-failure' job, Step 2.
Only runs if Step 1 (analyze_log.py) succeeded.

No AI calls here. Reads analysis.json written by analyze_log.py.
Pure git + GitHub CLI operations only.

Pipeline:
  5. create_github_pr  → git branch + commit + push + gh pr create

Guardrail: AI proposes. Human reviews and merges. AI never auto-merges.
"""

import json
import os
import subprocess
import sys

ANALYSIS_FILE = "analysis.json"
DIVIDER = "─" * 62

GITHUB_ACTOR = os.environ.get("GITHUB_ACTOR", "ci-bot")
REPO = os.environ.get("REPO", "")
RUN_ID = os.environ.get("RUN_ID", "unknown")


def kv(key, value, indent=2):
    print(f"{' ' * indent}{key:<18}: {value}")


def run(cmd, check=True, capture=False):
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


print(f"{'━' * 62}")
print(f"  CI FAILURE ANALYZER — Create Fix PR")
print(f"  Run: {RUN_ID}")
print(f"{'━' * 62}")


# ─── Read analysis from Step 1 ───────────────────────────────────────────────

print(f"\n{'━' * 62}")
print(f"  STEP 5 — Create GitHub PR  [Guardrail: human reviews before merge]")
print(f"{'━' * 62}")

if not os.path.exists(ANALYSIS_FILE):
    print(f"  ERROR: {ANALYSIS_FILE} not found. Did Step 1 (analyze_log.py) complete?")
    sys.exit(1)

with open(ANALYSIS_FILE) as f:
    data = json.load(f)

pr_title = data.get("pr_title", "fix: CI failure detected by AI analysis")
pr_description = data.get("pr_description", "")
error_type = data.get("error_type", "unknown")
fix_command = data.get("fix_command", "—")
severity = data.get("severity", "—")
confidence = data.get("confidence", "—")
kb = data.get("knowledge_base_match", {})

print()
print("  Analysis summary from Step 1:")
kv("error_type", error_type)
kv("severity", severity)
kv("confidence", confidence)
kv("fix_command", str(fix_command)[:80])
kv("RAG match", f"{kb.get('error', 'none')} (similarity: {kb.get('similarity', 0)})" if kb.get("matched") else "none")
kv("PR title", pr_title)


# ─── Git setup ───────────────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Git setup")
print(f"  {DIVIDER}")

run(["git", "config", "user.email", f"{GITHUB_ACTOR}@users.noreply.github.com"])
run(["git", "config", "user.name", GITHUB_ACTOR])
kv("git user", f"{GITHUB_ACTOR}@users.noreply.github.com")


# ─── Create branch ───────────────────────────────────────────────────────────

branch = f"fix/ai-analysis-{RUN_ID}"
kv("branch", branch)

result = run(["git", "checkout", "-b", branch], check=False, capture=True)
if result.returncode != 0:
    print(f"  Branch already exists — checking out existing branch")
    run(["git", "checkout", branch])
else:
    print(f"  Branch created")


# ─── Commit analysis file ─────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Committing analysis file")
print(f"  {DIVIDER}")

with open("ci_analysis.md", "w") as f:
    f.write(f"# CI Failure Analysis — Run {RUN_ID}\n\n")
    f.write(pr_description)

run(["git", "add", "ci_analysis.md"])

diff = run(["git", "diff", "--cached", "--quiet"], check=False)
if diff.returncode != 0:
    run(["git", "commit", "-m", f"ci: AI analysis for failed run {RUN_ID}"])
    kv("Committed", "ci_analysis.md")
else:
    kv("Committed", "no changes (re-run — skipping commit)")


# ─── Push branch ─────────────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Pushing branch")
print(f"  {DIVIDER}")

run(["git", "push", "origin", branch, "--force-with-lease"])
kv("Pushed", f"origin/{branch}")


# ─── Check for existing PR ────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Creating PR")
print(f"  {DIVIDER}")

existing = run(
    ["gh", "pr", "list", "--head", branch, "--json", "number,url", "--jq", ".[0]"],
    capture=True,
    check=False,
)

existing_text = existing.stdout.strip()
if existing_text and existing_text != "null":
    try:
        ex = json.loads(existing_text)
        pr_url = ex.get("url", "")
        pr_num = ex.get("number", "")
        kv("Status", f"PR already exists (re-run)")
        kv("PR", f"#{pr_num}  {pr_url}")
        print()
        print("  ✅ PR already exists. Human review required before merging.")
        print(f"  ⚠️  This fix was proposed by AI analysis. Review before merging.")
        sys.exit(0)
    except (json.JSONDecodeError, KeyError):
        pass

# Create the PR
pr_result = run(
    [
        "gh", "pr", "create",
        "--title", pr_title,
        "--body", pr_description,
        "--base", "main",
        "--head", branch,
    ],
    capture=True,
    check=False,
)

if pr_result.returncode == 0:
    pr_url = pr_result.stdout.strip()
    kv("Status", "created ✅")
    kv("PR URL", pr_url)
    print()
    print("  ✅ PR created successfully.")
    print("  ⚠️  This fix was proposed by AI analysis. Review before merging.")
else:
    error = pr_result.stderr.strip()
    kv("Status", "FAILED ❌")
    kv("Error", error[:120])
    print()
    print("  ❌ PR creation failed. See error above.")
    sys.exit(1)

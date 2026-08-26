"""
Step 2 of 2 — Create Fix PR
Runs inside the 'analyze-failure' job, Step 2.
Only runs if Step 1 (analyze_log.py) succeeded.

No AI calls here. Reads analysis.json written by analyze_log.py.

What this does:
  - Applies the actual file fix when possible (e.g. patches package.json for npm errors)
  - Creates a branch named fix/{error_type}-{short_sha} — traceable to the failing commit
  - Commits the real fix + analysis document
  - Opens a PR for human review

Guardrail: AI proposes. Human reviews and merges. AI never auto-merges.
Branch naming: fix/{error_type}-{commit_sha[:7]}  e.g. fix/npm-dependency-aa6158f
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
COMMIT_SHA = os.environ.get("COMMIT_SHA", "unknown")
SHORT_SHA = COMMIT_SHA[:7]


def kv(key, value, indent=2):
    print(f"{' ' * indent}{key:<20}: {value}")


def run(cmd, check=True, capture=False):
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def _parse_npm_packages(fix_command: str) -> dict:
    """
    Parse npm fix_command into {package_name: version}.
    Handles: 'npm install react@18.0.0 react-dom@18.0.0 --save-exact'
    Handles scoped packages: '@company/utils@1.2.3'
    """
    skip = {"npm", "install", "add", "i"}
    result = {}
    for part in fix_command.split():
        if part.startswith("-"):
            continue
        if part in skip:
            continue
        # Scoped package: @scope/name@version
        if part.startswith("@") and part.count("@") >= 2:
            at_idx = part.rfind("@")
            result[part[:at_idx]] = part[at_idx + 1:]
        # Regular package: name@version
        elif "@" in part:
            name, ver = part.rsplit("@", 1)
            if name:
                result[name] = ver
    return result


print(f"{'━' * 62}")
print(f"  CI FAILURE ANALYZER — Create Fix PR")
print(f"  Commit: {SHORT_SHA}  |  Run: {RUN_ID}")
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
affected_file = data.get("affected_file", "—")
kb = data.get("knowledge_base_match", {})

print()
print("  Analysis summary from Step 1:")
kv("error_type", error_type)
kv("severity", severity)
kv("confidence", confidence)
kv("affected_file", affected_file)
kv("fix_command", str(fix_command)[:80])
kv("RAG match", f"{kb.get('error', 'none')} (similarity: {kb.get('similarity', 0)})" if kb.get("matched") else "none")
kv("PR title", pr_title)

# Branch name: fix/{error_type}-{short_sha} — traceable to the failing commit
branch = f"fix/{error_type}-{SHORT_SHA}"
kv("branch", branch)


# ─── Git setup ───────────────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Git setup")
print(f"  {DIVIDER}")

run(["git", "config", "user.email", f"{GITHUB_ACTOR}@users.noreply.github.com"])
run(["git", "config", "user.name", GITHUB_ACTOR])
kv("git user", f"{GITHUB_ACTOR}@users.noreply.github.com")

result = run(["git", "checkout", "-b", branch], check=False, capture=True)
if result.returncode != 0:
    print(f"  Branch already exists — checking out")
    run(["git", "checkout", branch])
else:
    print(f"  Branch created: {branch}")


# ─── Apply the actual file fix ────────────────────────────────────────────────
# For npm-dependency errors: patch package.json directly with corrected versions.
# For other error types: commit the analysis doc only — human applies the fix.
# The PR diff should show the REAL change, not just a description of it.

print()
print(f"  {DIVIDER}")
print("  Applying fix")
print(f"  {DIVIDER}")

files_changed = []

if error_type == "npm-dependency":
    parsed = _parse_npm_packages(fix_command)
    if parsed and os.path.exists("package.json"):
        with open("package.json") as f:
            pkg = json.load(f)

        print(f"  Patching package.json:")
        changed = False
        for name, to_ver in parsed.items():
            for dep_key in ("dependencies", "devDependencies", "peerDependencies"):
                if dep_key in pkg and name in pkg[dep_key]:
                    current = pkg[dep_key][name]
                    pkg[dep_key][name] = to_ver
                    kv(f"  {name}", f"{current} → {to_ver}  ({dep_key})")
                    changed = True

        if changed:
            with open("package.json", "w") as f:
                json.dump(pkg, f, indent=2)
                f.write("\n")
            run(["git", "add", "package.json"])
            files_changed.append("package.json")
            print(f"  package.json updated and staged ✅")
        else:
            print(f"  ⚠️  Packages from fix_command not found in package.json — analysis only")
    else:
        print(f"  ⚠️  Could not parse packages from fix_command or package.json missing")
        print(f"       fix_command: {fix_command}")
else:
    print(f"  error_type '{error_type}' — file patch not automated for this type")
    print(f"  Manual fix required: {fix_command}")
    print(f"  Affected file: {affected_file}")


# ─── Commit ───────────────────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Committing")
print(f"  {DIVIDER}")

# Always commit the analysis doc so the PR has context
with open("ci_analysis.md", "w") as f:
    f.write(f"# CI Failure Analysis — Run {RUN_ID}\n\n")
    f.write(pr_description)

run(["git", "add", "ci_analysis.md"])
files_changed.append("ci_analysis.md")

diff = run(["git", "diff", "--cached", "--quiet"], check=False)
if diff.returncode != 0:
    commit_msg = f"fix(ci): {error_type} fix for commit {SHORT_SHA}\n\nAI-generated analysis — run {RUN_ID}"
    run(["git", "commit", "-m", commit_msg])
    kv("Committed", ", ".join(files_changed))
else:
    kv("Committed", "no changes (re-run — skipping commit)")


# ─── Push ─────────────────────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Pushing branch")
print(f"  {DIVIDER}")

# --force: safe for ephemeral AI-generated fix branches — no shared work on them.
# --force-with-lease was rejected ("stale info") because actions/checkout pre-fetches
# remote branches, causing the lease expectation to mismatch on re-runs.
run(["git", "push", "origin", branch, "--force"])
kv("Pushed", f"origin/{branch}")


# ─── Create PR ────────────────────────────────────────────────────────────────

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
        kv("Status", f"already exists (re-run)")
        kv("PR", f"#{ex.get('number')}  {ex.get('url', '')}")
        print()
        print("  ✅ PR already exists. Human review required before merging.")
        sys.exit(0)
    except (json.JSONDecodeError, KeyError):
        pass

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
    kv("Branch", branch)
    kv("Files in PR", ", ".join(files_changed))
    print()
    print("  ✅ PR created. Human review required before merging.")
    print("  ⚠️  This fix was proposed by AI analysis. Review before merging.")
else:
    error_msg = pr_result.stderr.strip()
    kv("Status", "FAILED ❌")
    kv("Error", error_msg[:120])
    sys.exit(1)

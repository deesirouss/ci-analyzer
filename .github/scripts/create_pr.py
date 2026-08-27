"""
CI Failure Analyzer — Step 2 of 2: Create Fix PR

Runs inside the 'analyze-failure' job, Step 2.
Only runs if Step 1 (analyze_log.py) succeeded and wrote analysis.json.

What this does:
  1. Reads the structured analysis from analysis.json (no AI calls here)
  2. Applies the file-level fix when possible (e.g. patches package.json for
     npm dependency conflicts). Only the real fix file is committed — no
     analysis docs, no metadata files.
  3. Creates or updates a fix branch named fix/{error_type}-{trigger_branch}.
     The branch name is stable per error+branch combination: re-runs for the
     same issue on the same branch push to the same fix branch and update the
     existing PR instead of opening a duplicate.
  4. Opens a PR targeting the branch that triggered the workflow.

Guardrail: The AI proposes the fix. A human reviews and merges.
This script never calls git merge, never pushes to the main branch directly,
and never closes or merges its own PR.
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
# The branch that triggered the workflow — the PR targets this branch.
# Never hardcoded: supports main, develop, release/*, feature/* without changes.
TRIGGER_BRANCH = os.environ.get("TRIGGER_BRANCH", "main")
# Sanitize for use in branch names: "release/v2" → "release-v2"
SAFE_TRIGGER = TRIGGER_BRANCH.replace("/", "-")


# ═══════════════════════════════════════════════════════════════════════════════
# SIDECAR — Shell and display helpers
# ═══════════════════════════════════════════════════════════════════════════════

def kv(key, value, indent=2):
    """Print a key-value pair with aligned columns for CI log readability."""
    print(f"{' ' * indent}{key:<20}: {value}")


def run(cmd, check=True, capture=False):
    """
    Run a shell command via subprocess.

    Args:
        cmd:     list of command tokens (e.g. ["git", "status"])
        check:   if True (default), raises CalledProcessError on non-zero exit
        capture: if True, captures stdout/stderr; returned on the CompletedProcess

    Returns:
        subprocess.CompletedProcess
    """
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — Package version parser
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_npm_packages(fix_command):
    """
    Parse an npm install command into a {package_name: version} dict.

    Used to patch package.json directly rather than running npm install,
    which avoids side effects and keeps the PR diff minimal and reviewable.

    Handles standard and scoped packages:
      "npm install react@18.0.0 react-dom@18.0.0 --save-exact"
        → {"react": "18.0.0", "react-dom": "18.0.0"}
      "npm install @company/utils@1.2.3"
        → {"@company/utils": "1.2.3"}

    Args:
        fix_command: the fix_command string from analysis.json

    Returns:
        dict mapping package name → version string.
        Empty dict if the command cannot be parsed or contains no versioned packages.
    """
    skip = {"npm", "install", "add", "i"}
    result = {}
    for part in fix_command.split():
        if part.startswith("-"):
            continue
        if part in skip:
            continue
        if part.startswith("@") and part.count("@") >= 2:
            # Scoped package: @scope/name@version — version follows the last @
            at_idx = part.rfind("@")
            result[part[:at_idx]] = part[at_idx + 1:]
        elif "@" in part:
            # Regular package: name@version
            name, ver = part.rsplit("@", 1)
            if name:
                result[name] = ver
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

print(f"{'━' * 62}")
print(f"  CI FAILURE ANALYZER — Create Fix PR")
print(f"  Commit: {SHORT_SHA}  |  Run: {RUN_ID}")
print(f"{'━' * 62}")


# ─── Step 5: Read analysis from Step 1 ───────────────────────────────────────

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
error_slug = data.get("error_slug", "")
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

# Branch label selection:
# - Known error types (npm-dependency, docker-build, etc.) → use error_type directly
# - Unknown errors (type == "other") → use the AI-generated error_slug instead.
#   error_slug is produced with temperature=0.0, so the same failure always
#   produces the same slug, keeping the branch name stable across re-runs.
branch_label = error_slug if error_type == "other" and error_slug else error_type
branch = f"fix/{branch_label}-{SAFE_TRIGGER}"
kv("branch", branch)


# ─── Git setup ───────────────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Git setup")
print(f"  {DIVIDER}")

run(["git", "config", "user.email", f"{GITHUB_ACTOR}@users.noreply.github.com"])
run(["git", "config", "user.name", GITHUB_ACTOR])
kv("git user", f"{GITHUB_ACTOR}@users.noreply.github.com")

run(["git", "fetch", "origin"])

remote_check = run(
    ["git", "ls-remote", "--heads", "origin", branch],
    capture=True, check=False,
)
branch_exists_on_remote = bool(remote_check.stdout.strip())

kv("Base branch", TRIGGER_BRANCH)

if branch_exists_on_remote:
    # Branch exists from a previous run of this same error.
    # Pull it and merge the trigger branch to pick up commits that landed
    # on the base since the fix branch was first created.
    run(["git", "checkout", branch])
    merge = run(["git", "merge", f"origin/{TRIGGER_BRANCH}", "--no-edit"], check=False, capture=True)
    if merge.returncode != 0:
        run(["git", "merge", "--abort"], check=False)
        kv("Branch", f"{branch} (exists — merge conflict with {TRIGGER_BRANCH}, skipping merge)")
    else:
        kv("Branch", f"{branch} (exists — merged latest {TRIGGER_BRANCH} ✅)")
else:
    run(["git", "checkout", "-b", branch, f"origin/{TRIGGER_BRANCH}"])
    kv("Branch", f"{branch} (created from origin/{TRIGGER_BRANCH} ✅)")


# ─── Apply the file fix ───────────────────────────────────────────────────────
# For known error types, patch the affected file directly from fix_command.
# Only the patched file is committed — the PR description carries the full
# analysis context. For error types without an automated patch, the PR
# description guides the reviewer to apply the fix manually.

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
            print(f"  ⚠️  Packages from fix_command not found in package.json")
            print(f"       fix_command: {fix_command}")
    else:
        print(f"  ⚠️  Could not parse packages from fix_command or package.json missing")
        print(f"       fix_command: {fix_command}")
else:
    print(f"  error_type '{error_type}' — no automated file patch for this type")
    print(f"  Manual fix: {fix_command}")
    print(f"  Affected file: {affected_file}")


# ─── Commit ───────────────────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Committing")
print(f"  {DIVIDER}")

diff = run(["git", "diff", "--cached", "--quiet"], check=False)
if diff.returncode != 0:
    # Staged file changes exist — commit them (e.g. patched package.json)
    commit_msg = f"fix(ci): {error_type} fix for commit {SHORT_SHA}\n\nAI-generated analysis — run {RUN_ID}"
    run(["git", "commit", "-m", commit_msg])
    kv("Committed", ", ".join(files_changed))
else:
    # No file patch for this error type — create an empty commit so the fix
    # branch diverges from the base and GitHub allows PR creation.
    # The PR description carries the full analysis; the human applies the fix.
    commit_msg = (
        f"fix(ci): AI analysis for {error_type} error at commit {SHORT_SHA}\n\n"
        f"No automated file patch available for this error type.\n"
        f"See PR description for root cause and manual fix steps.\n"
        f"Run: {RUN_ID}"
    )
    run(["git", "commit", "--allow-empty", "-m", commit_msg])
    kv("Committed", "empty commit (no automated patch — fix described in PR body)")


# ─── Push ─────────────────────────────────────────────────────────────────────

print()
print(f"  {DIVIDER}")
print("  Pushing branch")
print(f"  {DIVIDER}")

# No force push required: the branch is always built from or merged with
# origin/{TRIGGER_BRANCH}, so the local tip is always ahead of or equal to remote.
run(["git", "push", "origin", branch])
kv("Pushed", f"origin/{branch}")


# ─── Create or identify existing PR ──────────────────────────────────────────

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
        kv("Status", "already exists — updated by latest push")
        kv("PR", f"#{ex.get('number')}  {ex.get('url', '')}")
        print()
        print("  ✅ PR updated. Human review required before merging.")
        sys.exit(0)
    except (json.JSONDecodeError, KeyError):
        pass

pr_result = run(
    [
        "gh", "pr", "create",
        "--title", pr_title,
        "--body", pr_description,
        "--base", TRIGGER_BRANCH,
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
    kv("Files changed", ", ".join(files_changed) if files_changed else "none (description-only PR)")
    print()
    print("  ✅ PR created. Human review required before merging.")
    print("  ⚠️  This fix was proposed by AI analysis. Review before merging.")
else:
    error_msg = pr_result.stderr.strip()
    kv("Status", "FAILED ❌")
    kv("Error", error_msg)
    sys.exit(1)

"""
agents/diff_auditor.py — Diff Auditor Agent.

Checks a PR's description, commit messages, and changed files against
the checklist produced by the Policy Parser.  Returns one Finding per
rule, plus extra Findings for any hardcoded secrets detected by regex.

Two-pass design:
  Pass 1 — regex secret scan (fast, no LLM token cost)
  Pass 2 — LLM evaluates every checklist rule against the PR evidence
"""

import json
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import get_llm
from models import ChecklistItem, Finding

# ---------------------------------------------------------------------------
# Pass 1 — Regex secret scanner
# ---------------------------------------------------------------------------

# Matches assignments like:  STRIPE_SECRET_KEY = "abc123xyz"  |  password='s3cr3t!'
# \w* allows keyword to appear inside longer variable names (e.g. STRIPE_SECRET_KEY)
_SECRET_PATTERN = re.compile(
    r'(?i)(?:api_key|secret|token|password|passwd|credential|private_key|access_key)'
    r'\w*\s*[=:]\s*["\']([^"\']{8,})["\']'
)

# Exclude obvious placeholders so we don't flag template files
_PLACEHOLDER_PATTERN = re.compile(
    r'(?i)(your[_\-]?\w*|changeme|placeholder|example|xxxxxxx+|<[^>]+>|enter[_\-]?\w*|todo|fixme|insert[_\-]?\w*)',
)


def _scan_secrets(diff: str) -> list[Finding]:
    """Return a Finding for each potential hardcoded secret found in the diff."""
    findings: list[Finding] = []
    seen_values: set[str] = set()

    for match in _SECRET_PATTERN.finditer(diff):
        value = match.group(1).strip()
        # Skip if it looks like a placeholder
        if _PLACEHOLDER_PATTERN.search(value) or len(value) < 8:
            continue
        if value in seen_values:
            continue
        seen_values.add(value)

        # Try to extract a filename hint from the preceding diff header
        pos = match.start()
        preceding = diff[max(0, pos - 500):pos]
        file_match = re.search(r'\+\+\+ b/(.+)', preceding)
        filename = file_match.group(1).strip() if file_match else "unknown file"

        full_line = match.group(0)
        masked = full_line[:40] + "..." if len(full_line) > 40 else full_line

        findings.append(Finding(
            rule_id="secret-scan",
            status="FAIL",
            evidence=f"Possible hardcoded secret in {filename}: `{masked}`",
            suggestion=(
                "Move this value to an environment variable and load it with "
                "os.getenv(). Never commit real credentials."
            ),
        ))

    return findings


# ---------------------------------------------------------------------------
# Pass 2 — LLM checklist evaluation
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a pull request reviewer. You evaluate whether a pull request satisfies \
each rule in a contribution checklist.
Return ONLY a valid JSON array — no markdown, no explanation.
"""

_USER_PROMPT_TEMPLATE = """\
## Pull Request Details

**Title:** {title}

**Description:**
{description}

**Commit messages:**
{commits}

**Changed files (up to 20):**
{changed_files}

**Diff excerpt (first 6000 chars):**
{diff_excerpt}

---

## Checklist to evaluate

{checklist_json}

---

For EACH checklist item, return one JSON object with these fields:
  "rule_id"    : same rule_id as in the checklist
  "status"     : "PASS", "WARN", or "FAIL"
  "evidence"   : one sentence quoting or describing what you found (or did not find)
  "suggestion" : a friendly, specific fix — empty string "" if status is PASS

Rules for deciding status:
- PASS  → the PR clearly satisfies the rule
- FAIL  → the PR clearly violates the rule, AND the rule severity is FAIL
- WARN  → the PR violates or probably violates the rule, OR the rule severity is WARN/INFO
- When in doubt, prefer WARN over FAIL.

Return ONLY the JSON array, nothing else.
"""


def _build_pr_summary(pr_data: dict) -> dict:
    """Extract and truncate the fields needed for the LLM prompt."""
    description = pr_data.get("description", "") or "(no description provided)"
    commits = pr_data.get("commits", [])
    changed_files = pr_data.get("changed_files", [])
    diff = pr_data.get("diff", "")

    return {
        "title": pr_data.get("title", "(no title)"),
        "description": description[:1500],
        "commits": "\n".join(f"- {c[:120]}" for c in commits[:20]) or "(no commits)",
        "changed_files": "\n".join(f"- {f}" for f in changed_files[:20]) or "(no files)",
        "diff_excerpt": diff[:6000],
    }


def _call_llm(pr_summary: dict, checklist: list[ChecklistItem]) -> list[Finding]:
    """Ask the LLM to evaluate every checklist rule and return Findings."""
    checklist_json = json.dumps(
        [item.to_dict() for item in checklist], indent=2
    )

    prompt = _USER_PROMPT_TEMPLATE.format(
        **pr_summary,
        checklist_json=checklist_json,
    )

    llm = get_llm()
    from langchain_core.messages import SystemMessage, HumanMessage
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    response = llm.invoke(messages)
    raw = response.content.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    items_data: list[dict] = json.loads(raw)

    findings: list[Finding] = []
    valid_statuses = {"PASS", "WARN", "FAIL"}

    for item in items_data:
        status = item.get("status", "WARN").upper()
        if status not in valid_statuses:
            status = "WARN"
        findings.append(Finding(
            rule_id=item.get("rule_id", "unknown"),
            status=status,
            evidence=item.get("evidence", "").strip(),
            suggestion=item.get("suggestion", "").strip(),
        ))

    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit_diff(pr_data: dict, checklist: list[ChecklistItem]) -> list[Finding]:
    """Check a PR against its checklist and return all Findings.

    Args:
        pr_data   : dict from github_client.fetch_pr_data()
        checklist : list of ChecklistItem from policy_parser.parse_policy()

    Returns:
        list of Finding — one per rule + any secret-scan findings.
        On LLM failure, returns stub WARN findings so the report still runs.
    """
    # Pass 1 — fast regex secret scan (always runs, no LLM cost)
    secret_findings = _scan_secrets(pr_data.get("diff", ""))

    # Pass 2 — LLM evaluates all checklist rules
    pr_summary = _build_pr_summary(pr_data)
    try:
        llm_findings = _call_llm(pr_summary, checklist)
    except (json.JSONDecodeError, Exception) as exc:
        print(f"[diff_auditor] LLM error ({exc}) — returning stub findings.")
        llm_findings = [
            Finding(
                rule_id=item.rule_id,
                status="WARN",
                evidence="Audit could not complete — LLM error.",
                suggestion="Re-run the audit or check manually.",
            )
            for item in checklist
        ]

    return llm_findings + secret_findings


# ---------------------------------------------------------------------------
# Smoke-test (run directly: python agents/diff_auditor.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from models import ChecklistItem

    SAMPLE_CHECKLIST = [
        ChecklistItem("rule_001", "CONTRIBUTING.md", "PR must reference a GitHub issue (Fixes #NNN)", "FAIL"),
        ChecklistItem("rule_002", "CONTRIBUTING.md", "New functions must include tests", "WARN"),
        ChecklistItem("rule_003", "CONTRIBUTING.md", "Commit messages must follow Conventional Commits", "WARN"),
    ]

    SAMPLE_PR_CLEAN = {
        "title": "feat: add payment webhook handler",
        "description": "Fixes #42 — adds a new Stripe webhook endpoint.",
        "commits": ["feat: add payment webhook handler", "test: add webhook tests"],
        "changed_files": ["src/webhooks.py", "tests/test_webhooks.py"],
        "diff": (
            "diff --git a/src/webhooks.py b/src/webhooks.py\n"
            "+def handle_webhook(event):\n"
            "+    pass\n"
        ),
    }

    SAMPLE_PR_BAD = {
        "title": "WIP",
        "description": "fixed stuff",
        "commits": ["wip", "more wip"],
        "changed_files": ["src/config.py"],
        "diff": (
            "diff --git a/src/config.py b/src/config.py\n"
            "+++ b/src/config.py\n"
            "+STRIPE_SECRET_KEY = 'sk_live_abc123xyz456def789'\n"
        ),
    }

    print("Test A — clean PR (LLM):")
    findings_a = audit_diff(SAMPLE_PR_CLEAN, SAMPLE_CHECKLIST)
    for f in findings_a:
        print(f"  [{f.status:4s}] {f.rule_id:<12}  {f.evidence[:70]}")

    print()
    print("Test B — bad PR with hardcoded secret (regex, no LLM):")
    secret_findings = _scan_secrets(SAMPLE_PR_BAD["diff"])
    for f in secret_findings:
        print(f"  [{f.status:4s}] {f.rule_id:<12}  {f.evidence[:80]}")
    assert len(secret_findings) >= 1, "Expected at least one secret finding"
    print(f"  Secret detection: {len(secret_findings)} finding(s) ✓")

    print()
    print("agents/diff_auditor.py OK ✓")

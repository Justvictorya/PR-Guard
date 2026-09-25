"""
agents/policy_parser.py — Policy Parser Agent.

Reads a repo's CONTRIBUTING.md and PR template, then uses the LLM to
extract a structured list of rules (ChecklistItems) the PR must satisfy.

If no contribution files are found, a sensible universal default checklist
is returned so the audit can still run on any public repo.
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import get_llm
from github_client import get_repo_file
from models import ChecklistItem

# ---------------------------------------------------------------------------
# File paths to probe for contribution guidelines
# ---------------------------------------------------------------------------

_CONTRIBUTING_PATHS = [
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CONTRIBUTING.txt",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
]

_PR_TEMPLATE_PATHS = [
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    ".github/PULL_REQUEST_TEMPLATE/general.md",
]

# ---------------------------------------------------------------------------
# Default checklist — used when no contribution files exist
# ---------------------------------------------------------------------------

DEFAULT_CHECKLIST: list[ChecklistItem] = [
    ChecklistItem("rule_001", "default", "PR description must reference a GitHub issue (e.g. 'Fixes #123')", "FAIL"),
    ChecklistItem("rule_002", "default", "New functions or features must include tests", "WARN"),
    ChecklistItem("rule_003", "default", "Commit messages should follow Conventional Commits format (type: description)", "WARN"),
    ChecklistItem("rule_004", "default", "PR title should be descriptive and not a placeholder like 'WIP' or 'fix stuff'", "WARN"),
    ChecklistItem("rule_005", "default", "No secrets, API keys, or passwords should appear in the diff", "FAIL"),
]

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a code review policy analyst. Your job is to extract contribution rules \
from repository documentation and return them as structured JSON.
"""

_USER_PROMPT_TEMPLATE = """\
Below are contribution guidelines found in this repository.
Extract every rule a pull request contributor must follow and return ONLY a JSON array.

Each item in the array must have exactly these fields:
  "rule_id"  : string, sequential like "rule_001", "rule_002", ...
  "source"   : filename the rule came from
  "text"     : the rule, written as a single clear sentence
  "severity" : "FAIL" if a violation must block the PR, "WARN" if it is a strong suggestion, "INFO" for minor style notes

Return ONLY valid JSON — no markdown, no explanation, no preamble.

--- CONTRIBUTION FILES ---
{content}
--- END ---
"""

# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def parse_policy(repo) -> list[ChecklistItem]:
    """Read the repo's contribution files and return a list of ChecklistItems.

    Args:
        repo : PyGithub Repository object

    Returns:
        List of ChecklistItem.  Falls back to DEFAULT_CHECKLIST if no
        contribution files are found or the LLM returns unparseable output.
    """
    # 1. Collect raw text from all recognised contribution files
    found_sections: list[str] = []

    for path in _CONTRIBUTING_PATHS:
        content = get_repo_file(repo, path)
        if content:
            found_sections.append(f"### {path}\n\n{content[:3000]}")  # cap per file
            break  # one CONTRIBUTING file is enough

    for path in _PR_TEMPLATE_PATHS:
        content = get_repo_file(repo, path)
        if content:
            found_sections.append(f"### {path}\n\n{content[:2000]}")
            break  # one PR template is enough

    # 2. If nothing found, return universal defaults
    if not found_sections:
        print("[policy_parser] No contribution files found — using default checklist.")
        return DEFAULT_CHECKLIST

    combined = "\n\n".join(found_sections)

    # 3. Ask the LLM to extract rules
    llm = get_llm()
    from langchain_core.messages import SystemMessage, HumanMessage

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=_USER_PROMPT_TEMPLATE.format(content=combined)),
    ]

    try:
        response = llm.invoke(messages)
        raw = response.content.strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        items_data: list[dict] = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        print(f"[policy_parser] LLM parse error ({exc}) — falling back to defaults.")
        return DEFAULT_CHECKLIST

    # 4. Validate and build ChecklistItems
    checklist: list[ChecklistItem] = []
    valid_severities = {"FAIL", "WARN", "INFO"}

    for i, item in enumerate(items_data):
        try:
            severity = item.get("severity", "WARN").upper()
            if severity not in valid_severities:
                severity = "WARN"
            checklist.append(ChecklistItem(
                rule_id=item.get("rule_id", f"rule_{i+1:03d}"),
                source=item.get("source", "unknown"),
                text=item.get("text", "").strip(),
                severity=severity,
            ))
        except Exception:
            continue  # skip malformed items silently

    if not checklist:
        print("[policy_parser] LLM returned empty list — using defaults.")
        return DEFAULT_CHECKLIST

    # 5. Always ensure a secrets rule exists
    has_secrets_rule = any(
        "secret" in item.text.lower() or "key" in item.text.lower()
        for item in checklist
    )
    if not has_secrets_rule:
        checklist.append(ChecklistItem(
            rule_id=f"rule_{len(checklist)+1:03d}",
            source="pr-guard-builtin",
            text="No secrets, API keys, or passwords should appear in the diff",
            severity="FAIL",
        ))

    return checklist


# ---------------------------------------------------------------------------
# Smoke-test (run directly: python agents/policy_parser.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json

    class _MockRepo:
        """Minimal repo stub that simulates a repo with a CONTRIBUTING.md."""
        def get_contents(self, path):
            if path == "CONTRIBUTING.md":
                class _C:
                    decoded_content = (
                        b"# Contributing Guide\n\n"
                        b"- All PRs must reference an open issue with Fixes #NNN.\n"
                        b"- New code must include unit tests with at least 80 percent coverage.\n"
                        b"- Commit messages must follow Conventional Commits: type(scope): message.\n"
                        b"- Do not commit secrets, API keys, or credentials.\n"
                        b"- Keep PRs focused - one feature or fix per PR.\n"
                    )
                return _C()
            from github import GithubException
            raise GithubException(404, "not found")

    print("Test A — repo with CONTRIBUTING.md:")
    checklist = parse_policy(_MockRepo())
    for item in checklist:
        print(f"  [{item.severity:4s}] {item.rule_id}  {item.text}")

    print()

    class _EmptyRepo:
        def get_contents(self, path):
            from github import GithubException
            raise GithubException(404, "not found")

    print("Test B — repo with NO contribution files (should use defaults):")
    checklist_b = parse_policy(_EmptyRepo())
    for item in checklist_b:
        print(f"  [{item.severity:4s}] {item.rule_id}  {item.text}")

    print("\nagents/policy_parser.py OK ✓")

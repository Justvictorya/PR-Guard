"""
agents/reporter.py — Reporter Agent.

Assembles all agent outputs into a single, well-formatted Markdown report.
No LLM call is made here — this is pure string templating so it is fast,
deterministic, and free.

Report sections:
  1. Header — PR title, repo, link
  2. Summary badge line — ✅ N passed | ⚠️ N warnings | ❌ N failed
  3. Policy Compliance table — one row per Finding
  4. External APIs & Dependencies table — one row per ApiEntry
  5. Similar Projects — list of up to 5 repos
  6. Next Steps — numbered action items (FAILs first, then WARNs)
"""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import ChecklistItem, Finding, ApiEntry

# ---------------------------------------------------------------------------
# Status / risk emoji maps
# ---------------------------------------------------------------------------

_STATUS_ICON = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
_RISK_ICON   = {"LOW": "🟢", "MED": "🟡", "HIGH": "🔴"}
_SEVERITY_ICON = {"FAIL": "❌", "WARN": "⚠️", "INFO": "ℹ️"}


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _header(pr_data: dict) -> str:
    repo  = pr_data.get("repo_full_name", "unknown/repo")
    num   = pr_data.get("pr_number")       # None in repo mode
    title = pr_data.get("title", "(no title)")
    mode  = pr_data.get("mode", "pr")
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if mode == "repo" or num is None:
        return (
            f"# PR Guard Report — Repository Audit\n\n"
            f"**Repository:** [{repo}](https://github.com/{repo})  \n"
            f"**Description:** {title}  \n"
            f"**Audit type:** 📁 General compliance audit (no PR diff)  \n"
            f"**Audited:** {ts}  \n"
        )

    url = f"https://github.com/{repo}/pull/{num}"
    return (
        f"# PR Guard Report\n\n"
        f"**Repository:** [{repo}](https://github.com/{repo})  \n"
        f"**Pull Request:** [#{num} — {title}]({url})  \n"
        f"**Audited:** {ts}  \n"
    )


def _summary_line(findings: list[Finding]) -> str:
    passed  = sum(1 for f in findings if f.status == "PASS")
    warned  = sum(1 for f in findings if f.status == "WARN")
    failed  = sum(1 for f in findings if f.status == "FAIL")

    parts = []
    if passed:  parts.append(f"✅ **{passed} passed**")
    if warned:  parts.append(f"⚠️ **{warned} warning{'s' if warned != 1 else ''}**")
    if failed:  parts.append(f"❌ **{failed} failed**")

    if not parts:
        return "\n> No findings to report.\n"

    return "\n> " + " &nbsp;|&nbsp; ".join(parts) + "\n"


def _policy_table(
    findings: list[Finding],
    checklist: list[ChecklistItem],
) -> str:
    if not findings:
        return "\n## 📋 Policy Compliance\n\n_No findings — checklist was empty._\n"

    # Build rule_id → text/severity lookup
    rule_map = {item.rule_id: item for item in checklist}

    rows: list[str] = []
    for f in findings:
        icon = _STATUS_ICON.get(f.status, "❓")
        rule_text = rule_map.get(f.rule_id, ChecklistItem(f.rule_id, "", f.rule_id, "WARN")).text
        evidence  = _escape_md(f.evidence)
        suggestion = _escape_md(f.suggestion) if f.suggestion else "—"
        rows.append(f"| {icon} {f.status} | {_escape_md(rule_text)} | {evidence} | {suggestion} |")

    table = (
        "\n## 📋 Policy Compliance\n\n"
        "| Status | Rule | Evidence | Suggested Fix |\n"
        "|--------|------|----------|---------------|\n"
        + "\n".join(rows)
        + "\n"
    )
    return table


def _api_table(api_entries: list[ApiEntry]) -> str:
    if not api_entries:
        return "\n## 🔌 External APIs & Dependencies\n\n_No external APIs or dependencies detected._\n"

    rows: list[str] = []
    for e in api_entries:
        risk_icon = _RISK_ICON.get(e.risk, "⚪")
        rows.append(
            f"| {e.entry_type} | `{_escape_md(e.value)}` | "
            f"{_escape_md(e.file)} | {risk_icon} {e.risk} |"
        )

    table = (
        "\n## 🔌 External APIs & Dependencies\n\n"
        "| Type | Value | File | Risk |\n"
        "|------|-------|------|------|\n"
        + "\n".join(rows)
        + "\n"
    )
    return table


def _similar_repos_section(similar_repos: list[dict]) -> str:
    if not similar_repos:
        return "\n## 🔍 Similar Projects\n\n_Could not retrieve similar projects (rate limit or no metadata)._\n"

    lines = ["\n## 🔍 Similar Projects\n"]
    for r in similar_repos:
        stars    = f"{r['stars']:,}"
        lang     = f"[{r['language']}]" if r.get("language") else ""
        desc     = f" — {r['description']}" if r.get("description") else ""
        lines.append(f"- [{r['name']}]({r['url']}) ★ {stars} {lang}{desc}")

    return "\n".join(lines) + "\n"


def _next_steps(findings: list[Finding]) -> str:
    # FAILs first, then WARNs — skip PASSes and items with no suggestion
    actionable = [
        f for f in findings
        if f.status in ("FAIL", "WARN") and f.suggestion.strip()
    ]
    # Sort: FAIL before WARN
    actionable.sort(key=lambda f: 0 if f.status == "FAIL" else 1)

    if not actionable:
        return "\n## ✅ Next Steps\n\n_Nothing to fix — all checks passed!_\n"

    lines = ["\n## 🛠️ Next Steps\n"]
    for i, f in enumerate(actionable, 1):
        icon = _STATUS_ICON.get(f.status, "")
        lines.append(f"{i}. {icon} **{f.rule_id}** — {f.suggestion}")

    return "\n".join(lines) + "\n"


def _footer() -> str:
    return "\n---\n_Report generated by [PR Guard](https://github.com) · Powered by IBM Bob_\n"


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _escape_md(text: str) -> str:
    """Escape pipe characters so they don't break Markdown tables."""
    return text.replace("|", "\\|")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(
    pr_data: dict,
    checklist: list[ChecklistItem],
    findings: list[Finding],
    api_entries: list[ApiEntry],
    similar_repos: list[dict],
) -> str:
    """Assemble the full Markdown PR Guard report.

    Args:
        pr_data      : dict from github_client.fetch_pr_data()
        checklist    : list[ChecklistItem] from policy_parser
        findings     : list[Finding] from diff_auditor
        api_entries  : list[ApiEntry] from api_extractor
        similar_repos: list[dict] from similar_repos detector

    Returns:
        Complete Markdown string ready to display or save as a .md file.
    """
    parts = [
        _header(pr_data),
        _summary_line(findings),
        _policy_table(findings, checklist),
        _api_table(api_entries),
        _similar_repos_section(similar_repos),
        _next_steps(findings),
        _footer(),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Smoke-test (run directly: python agents/reporter.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from models import ChecklistItem, Finding, ApiEntry

    CHECKLIST = [
        ChecklistItem("rule_001", "CONTRIBUTING.md", "PR must reference a GitHub issue (Fixes #NNN)", "FAIL"),
        ChecklistItem("rule_002", "CONTRIBUTING.md", "New functions must include tests", "WARN"),
        ChecklistItem("rule_003", "CONTRIBUTING.md", "Commit messages must follow Conventional Commits", "WARN"),
        ChecklistItem("rule_004", "pr-guard-builtin", "No secrets or API keys in diff", "FAIL"),
    ]

    FINDINGS = [
        Finding("rule_001", "FAIL",  "No 'Fixes #' found in PR description",    "Add 'Fixes #NNN' to your PR body"),
        Finding("rule_002", "PASS",  "tests/test_webhooks.py added",              ""),
        Finding("rule_003", "WARN",  "Commit 'wip' does not follow Conventional Commits", "Rename to 'fix: wip'"),
        Finding("secret-scan", "FAIL", "Possible hardcoded secret in src/config.py: `SECRET_KEY = ...`",
                "Move to .env and load with os.getenv()"),
    ]

    API_ENTRIES = [
        ApiEntry("endpoint",   "https://api.stripe.com/v1/charges", "src/payment.py", "LOW"),
        ApiEntry("env_var",    "STRIPE_API_KEY",                     "src/payment.py", "HIGH"),
        ApiEntry("sdk_import", "stripe",                             "src/payment.py", "MED"),
    ]

    SIMILAR = [
        {"name": "django/django",    "url": "https://github.com/django/django",    "stars": 78000, "language": "Python", "description": "The Web framework for perfectionists"},
        {"name": "fastapi/fastapi",  "url": "https://github.com/fastapi/fastapi",  "stars": 72000, "language": "Python", "description": "FastAPI framework, high performance"},
    ]

    PR_DATA = {
        "repo_full_name": "acme/my-app",
        "pr_number": 42,
        "title": "feat: add payment webhook handler",
    }

    report = generate_report(PR_DATA, CHECKLIST, FINDINGS, API_ENTRIES, SIMILAR)
    print(report)
    print("--- agents/reporter.py OK ✓ ---")

"""
models.py — Shared data models for PR Guard.

All agents produce and consume these structures so the rest of the code
has a single source of truth for what each piece of data looks like.
"""

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Severity / status type aliases — keeps strings consistent everywhere
# ---------------------------------------------------------------------------

Severity = Literal["FAIL", "WARN", "INFO"]
Status   = Literal["PASS", "WARN", "FAIL"]
Risk     = Literal["LOW", "MED", "HIGH"]
EntryType = Literal["endpoint", "env_var", "sdk_import", "dataset"]


# ---------------------------------------------------------------------------
# Policy Parser output
# ---------------------------------------------------------------------------

@dataclass
class ChecklistItem:
    """A single rule extracted from the repo's contribution guidelines.

    Attributes:
        rule_id  : Short unique ID, e.g. "rule_001"
        source   : Which file the rule came from, e.g. "CONTRIBUTING.md"
        text     : Human-readable rule description
        severity : How serious a violation is (FAIL | WARN | INFO)
    """
    rule_id: str
    source: str
    text: str
    severity: Severity = "WARN"

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "source": self.source,
            "text": self.text,
            "severity": self.severity,
        }


# ---------------------------------------------------------------------------
# Diff Auditor output
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """The result of checking one checklist rule against the actual PR.

    Attributes:
        rule_id    : Matches ChecklistItem.rule_id
        status     : PASS | WARN | FAIL
        evidence   : Short quote or description of what was (or wasn't) found
        suggestion : Friendly, actionable fix text (empty string if PASS)
    """
    rule_id: str
    status: Status
    evidence: str
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
        }


# ---------------------------------------------------------------------------
# API & Dependency Extractor output
# ---------------------------------------------------------------------------

@dataclass
class ApiEntry:
    """A third-party API, SDK import, environment variable, or dataset
    detected in the PR diff.

    Attributes:
        entry_type : endpoint | env_var | sdk_import | dataset
        value      : The actual value detected (URL, var name, package name)
        file       : The file where it was found (best-effort from diff context)
        risk       : LOW | MED | HIGH
    """
    entry_type: EntryType
    value: str
    file: str = "unknown"
    risk: Risk = "LOW"

    def to_dict(self) -> dict:
        return {
            "entry_type": self.entry_type,
            "value": self.value,
            "file": self.file,
            "risk": self.risk,
        }


# ---------------------------------------------------------------------------
# Similar Repos Detector output  (plain dict, no dataclass needed)
# ---------------------------------------------------------------------------

# Shape: {"name": str, "url": str, "stars": int, "language": str | None, "description": str | None}
# Used directly in reporter.py as a list of dicts — no dataclass overhead needed.


# ---------------------------------------------------------------------------
# Smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    item = ChecklistItem("rule_001", "CONTRIBUTING.md", "Link to an issue", "FAIL")
    finding = Finding("rule_001", "FAIL", "No 'Fixes #' in PR body", "Add 'Fixes #NNN' to description")
    entry = ApiEntry("endpoint", "https://api.stripe.com/v1", "payment.py", "LOW")

    print("ChecklistItem:", item.to_dict())
    print("Finding      :", finding.to_dict())
    print("ApiEntry     :", entry.to_dict())
    print("\nmodels.py OK ✓")

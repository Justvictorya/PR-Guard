"""
agents/api_extractor.py — API & Dependency Extractor Agent.

Scans the PR diff for:
  - HTTP/HTTPS endpoints  (regex)
  - Environment variable references  (regex)
  - Known third-party SDK imports  (regex)
  - Dataset references  (LLM second pass)

Risk is assigned by rule:
  - env_var  whose name ends in _KEY / _SECRET / _TOKEN / _PASSWORD  → HIGH
  - cloud SDK imports (boto3, azure, gcp, etc.)                      → MED
  - raw HTTP endpoints                                               → LOW
  - everything else                                                  → LOW

A lightweight LLM second pass deduplicates, adds context, and catches
anything the regexes miss (e.g. dataset URLs buried in comments).
"""

import json
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import get_llm
from models import ApiEntry

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# HTTP/HTTPS URLs — exclude common non-API domains (docs, images, CDNs)
_URL_PATTERN = re.compile(
    r'https?://[^\s"\'<>\]\[}{)(\n,;]{10,}',
)
_URL_NOISE = re.compile(
    r'(?i)(shields\.io|github\.com|githubusercontent\.com|docs\.|readme|'
    r'localhost|127\.0\.0\.1|example\.com|w3\.org|schema\.org|'
    r'\.png|\.jpg|\.gif|\.svg|\.ico|\.css|\.woff)',
)

# os.environ["VAR"] / os.environ.get("VAR") / os.getenv("VAR")
_ENV_VAR_PATTERN = re.compile(
    r'os\.environ\s*\[\s*["\'](\w+)["\']'        # os.environ["VAR"]
    r'|os\.environ\.get\s*\(\s*["\'](\w+)["\']'  # os.environ.get("VAR")
    r'|os\.getenv\s*\(\s*["\'](\w+)["\']',        # os.getenv("VAR")
)

# import boto3 / from stripe import ... / import anthropic
_IMPORT_PATTERN = re.compile(
    r'^[+]?\s*(?:import|from)\s+([\w.]+)',
    re.MULTILINE,
)

# Known cloud / AI / payment SDK top-level package names
_KNOWN_SDKS: dict[str, str] = {
    # cloud
    "boto3": "MED", "botocore": "MED",
    "azure": "MED", "google": "MED", "googleapiclient": "MED",
    "firebase_admin": "MED",
    # payments
    "stripe": "MED", "braintree": "MED", "paypalrestsdk": "MED",
    # messaging
    "twilio": "MED", "sendgrid": "MED", "mailchimp_marketing": "MED",
    # AI / LLM
    "openai": "MED", "anthropic": "MED", "cohere": "MED",
    "huggingface_hub": "MED", "transformers": "MED",
    # data
    "snowflake": "MED", "bigquery": "MED", "pymongo": "LOW",
    "psycopg2": "LOW", "sqlalchemy": "LOW",
    # http clients
    "requests": "LOW", "httpx": "LOW", "aiohttp": "LOW",
    "urllib3": "LOW",
    # auth
    "jwt": "MED", "authlib": "MED", "oauthlib": "MED",
}

# High-risk env var name suffixes
_HIGH_RISK_SUFFIX = re.compile(
    r'(?i)(key|secret|token|password|passwd|credential|private|cert)$'
)


# ---------------------------------------------------------------------------
# Pass 1 — pure regex extraction
# ---------------------------------------------------------------------------

def _extract_urls(diff: str) -> list[ApiEntry]:
    entries: list[ApiEntry] = []
    seen: set[str] = set()

    # Only look at added lines (lines starting with +)
    added_lines = [ln for ln in diff.splitlines() if ln.startswith("+")]
    added_text = "\n".join(added_lines)

    for match in _URL_PATTERN.finditer(added_text):
        url = match.group(0).rstrip(".,;)'\"")
        if url in seen or _URL_NOISE.search(url):
            continue
        seen.add(url)

        # Best-effort filename from surrounding diff context
        pos = diff.find(url)
        preceding = diff[max(0, pos - 400): pos]
        file_match = re.search(r'\+\+\+ b/(.+)', preceding)
        filename = file_match.group(1).strip() if file_match else "unknown"

        entries.append(ApiEntry(
            entry_type="endpoint",
            value=url,
            file=filename,
            risk="LOW",
        ))

    return entries


def _extract_env_vars(diff: str) -> list[ApiEntry]:
    entries: list[ApiEntry] = []
    seen: set[str] = set()

    added_lines = "\n".join(ln for ln in diff.splitlines() if ln.startswith("+"))

    for match in _ENV_VAR_PATTERN.finditer(added_lines):
        # Pattern has three capture groups; pick the non-None one
        name = match.group(1) or match.group(2) or match.group(3)
        if not name or name in seen:
            continue
        seen.add(name)

        risk = "HIGH" if _HIGH_RISK_SUFFIX.search(name) else "LOW"

        pos = diff.find(match.group(0))
        preceding = diff[max(0, pos - 400): pos]
        file_match = re.search(r'\+\+\+ b/(.+)', preceding)
        filename = file_match.group(1).strip() if file_match else "unknown"

        entries.append(ApiEntry(
            entry_type="env_var",
            value=name,
            file=filename,
            risk=risk,
        ))

    return entries


def _extract_sdk_imports(diff: str) -> list[ApiEntry]:
    entries: list[ApiEntry] = []
    seen: set[str] = set()

    for match in _IMPORT_PATTERN.finditer(diff):
        pkg = match.group(1).split(".")[0]  # top-level package only
        if pkg in seen or pkg not in _KNOWN_SDKS:
            continue
        seen.add(pkg)

        risk = _KNOWN_SDKS[pkg]

        pos = match.start()
        preceding = diff[max(0, pos - 400): pos]
        file_match = re.search(r'\+\+\+ b/(.+)', preceding)
        filename = file_match.group(1).strip() if file_match else "unknown"

        entries.append(ApiEntry(
            entry_type="sdk_import",
            value=pkg,
            file=filename,
            risk=risk,
        ))

    return entries


# ---------------------------------------------------------------------------
# Pass 2 — LLM enrichment / second-pass catch
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """\
You are a security-aware code reviewer. Analyse the provided diff excerpt and \
return a JSON array of any third-party API endpoints, SDK imports, environment \
variable references, or external dataset URLs that you can identify.
Return ONLY a valid JSON array — no markdown, no explanation.
"""

_LLM_USER_TEMPLATE = """\
## Already detected (do NOT repeat these):
{already_found}

## Diff excerpt to analyse:
{diff_excerpt}

For any NEW items not already listed above, return a JSON array.
Each item must have:
  "entry_type" : "endpoint" | "env_var" | "sdk_import" | "dataset"
  "value"      : the URL, variable name, package name, or dataset reference
  "file"       : filename if determinable, else "unknown"
  "risk"       : "LOW" | "MED" | "HIGH"

If you find nothing new, return an empty array [].
Return ONLY valid JSON.
"""


def _llm_second_pass(diff: str, already_found: list[ApiEntry]) -> list[ApiEntry]:
    """Ask the LLM to catch anything the regexes missed."""
    already_json = json.dumps(
        [e.to_dict() for e in already_found], indent=2
    ) if already_found else "[]"

    prompt = _LLM_USER_TEMPLATE.format(
        already_found=already_json,
        diff_excerpt=diff[:5000],
    )

    llm = get_llm()
    from langchain_core.messages import SystemMessage, HumanMessage
    messages = [
        SystemMessage(content=_LLM_SYSTEM),
        HumanMessage(content=prompt),
    ]

    try:
        response = llm.invoke(messages)
        raw = response.content.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        items: list[dict] = json.loads(raw)
    except Exception as exc:
        print(f"[api_extractor] LLM second pass skipped ({exc})")
        return []

    valid_types = {"endpoint", "env_var", "sdk_import", "dataset"}
    valid_risks = {"LOW", "MED", "HIGH"}
    result: list[ApiEntry] = []

    for item in items:
        entry_type = item.get("entry_type", "endpoint")
        risk = item.get("risk", "LOW").upper()
        if entry_type not in valid_types:
            continue
        if risk not in valid_risks:
            risk = "LOW"
        result.append(ApiEntry(
            entry_type=entry_type,
            value=str(item.get("value", "")).strip(),
            file=str(item.get("file", "unknown")).strip(),
            risk=risk,
        ))

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_apis(pr_data: dict) -> list[ApiEntry]:
    """Scan the PR diff for external APIs, SDKs, env vars, and datasets.

    Args:
        pr_data : dict from github_client.fetch_pr_data()

    Returns:
        Deduplicated list of ApiEntry sorted by risk (HIGH first).
    """
    diff = pr_data.get("diff", "")

    # Pass 1 — regex (fast, deterministic)
    regex_entries: list[ApiEntry] = []
    regex_entries.extend(_extract_urls(diff))
    regex_entries.extend(_extract_env_vars(diff))
    regex_entries.extend(_extract_sdk_imports(diff))

    # Pass 2 — LLM (catches what regexes miss)
    llm_entries = _llm_second_pass(diff, regex_entries)

    all_entries = regex_entries + llm_entries

    # Deduplicate by (entry_type, value) — keep first occurrence
    seen: set[tuple] = set()
    deduped: list[ApiEntry] = []
    for entry in all_entries:
        key = (entry.entry_type, entry.value.lower())
        if key not in seen:
            seen.add(key)
            deduped.append(entry)

    # Sort: HIGH first, then MED, then LOW
    risk_order = {"HIGH": 0, "MED": 1, "LOW": 2}
    deduped.sort(key=lambda e: risk_order.get(e.risk, 3))

    return deduped


# ---------------------------------------------------------------------------
# Smoke-test (run directly: python agents/api_extractor.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    SAMPLE_DIFF = """\
diff --git a/src/payment.py b/src/payment.py
index abc..def 100644
--- a/src/payment.py
+++ b/src/payment.py
@@ -1,5 +1,15 @@
+import stripe
+import boto3
+import requests
+
+STRIPE_ENDPOINT = "https://api.stripe.com/v1/charges"
+S3_BUCKET_URL   = "https://my-bucket.s3.amazonaws.com/data"
+
+def charge_customer(amount):
+    api_key = os.getenv("STRIPE_API_KEY")
+    region  = os.environ.get("AWS_REGION")
+    secret  = os.environ["DB_SECRET"]
+    return stripe.Charge.create(amount=amount, currency="usd")
"""

    print("Regex pass only:")
    url_entries = _extract_urls(SAMPLE_DIFF)
    env_entries = _extract_env_vars(SAMPLE_DIFF)
    sdk_entries = _extract_sdk_imports(SAMPLE_DIFF)

    for e in url_entries + env_entries + sdk_entries:
        print(f"  [{e.risk:3s}] {e.entry_type:<12}  {e.value}")

    print()
    print("Full extract_apis (regex + LLM):")
    pr_data = {"diff": SAMPLE_DIFF}
    entries = extract_apis(pr_data)
    for e in entries:
        print(f"  [{e.risk:3s}] {e.entry_type:<12}  {e.value}  ({e.file})")

    print()
    print("agents/api_extractor.py OK ✓")

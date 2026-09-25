"""
agents/pr_description.py — Auto-Generated PR Description Agent.

If a PR has a blank or weak description, this agent reads the diff and
commit messages and drafts a professional, structured PR description the
contributor can copy-paste directly into GitHub.

Only runs when the PR description is missing or shorter than 80 characters.
Returns None if the description is already adequate.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import get_llm

_MIN_DESCRIPTION_LENGTH = 80  # chars — below this we consider it "weak"

_SYSTEM_PROMPT = """\
You are an expert open-source contributor who writes clear, professional \
pull request descriptions. You write in plain English, are concise, and \
always follow the standard PR description structure.
"""

_USER_PROMPT = """\
A developer submitted a pull request with a blank or very short description.
Based on the information below, write a professional PR description they can \
copy and paste into GitHub.

## PR Title
{title}

## Commit Messages
{commits}

## Changed Files
{changed_files}

## Diff Excerpt (first 3000 chars)
{diff_excerpt}

---

Write the PR description using this exact structure:

## What does this PR do?
(1-3 sentences summarising the change)

## Why is this change needed?
(1-2 sentences on the motivation or problem solved)

## How was it implemented?
(Brief bullet points on the approach)

## Testing
(What testing was done, or what should be tested)

## Checklist
- [ ] Tests added or updated
- [ ] Documentation updated if needed
- [ ] No secrets or credentials committed

Write only the description — no preamble, no explanation.
"""


def generate_pr_description(pr_data: dict) -> str | None:
    """Generate a draft PR description if the existing one is weak.

    Args:
        pr_data : dict from github_client.fetch_pr_data()

    Returns:
        A Markdown string with the suggested PR description,
        or None if the existing description is already adequate.
    """
    existing = (pr_data.get("description") or "").strip()

    # Skip if description is already substantial
    if len(existing) >= _MIN_DESCRIPTION_LENGTH:
        return None

    diff    = pr_data.get("diff", "")
    commits = pr_data.get("commits", [])
    files   = pr_data.get("changed_files", [])
    title   = pr_data.get("title", "(no title)")

    prompt = _USER_PROMPT.format(
        title=title,
        commits="\n".join(f"- {c[:120]}" for c in commits[:10]) or "(no commits)",
        changed_files="\n".join(f"- {f}" for f in files[:20]) or "(no files)",
        diff_excerpt=diff[:3000],
    )

    try:
        llm = get_llm()
        from langchain_core.messages import SystemMessage, HumanMessage
        response = llm.invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        return response.content.strip()
    except Exception as exc:
        print(f"[pr_description] LLM error ({exc}) — skipping description generation.")
        return None


# ---------------------------------------------------------------------------
# Smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    SAMPLE = {
        "title": "feat: add Stripe webhook handler",
        "description": "fixed stuff",   # weak — should trigger generation
        "commits": [
            "feat: add stripe webhook endpoint",
            "test: add webhook integration tests",
            "fix: handle missing signature header",
        ],
        "changed_files": ["src/webhooks.py", "tests/test_webhooks.py", "requirements.txt"],
        "diff": (
            "+import stripe\n"
            "+from flask import request, jsonify\n"
            "+\n"
            "+@app.route('/webhook', methods=['POST'])\n"
            "+def handle_webhook():\n"
            "+    payload = request.get_data()\n"
            "+    sig = request.headers.get('Stripe-Signature')\n"
            "+    event = stripe.Webhook.construct_event(payload, sig, os.getenv('STRIPE_WEBHOOK_SECRET'))\n"
            "+    return jsonify({'status': 'ok'})\n"
        ),
    }

    print("Testing with weak description ('fixed stuff'):\n")
    result = generate_pr_description(SAMPLE)
    if result:
        print(result)
    else:
        print("(No description generated — existing was adequate)")

    print("\n--- Testing with adequate description (should return None) ---")
    SAMPLE["description"] = "This PR adds a Stripe webhook handler that verifies signatures and processes payment events. It includes integration tests and handles missing headers gracefully."
    result2 = generate_pr_description(SAMPLE)
    print(f"Result: {result2!r} (should be None)")

    print("\nagents/pr_description.py OK ✓")

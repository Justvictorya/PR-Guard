# PR Guard — Hackathon Build Plan

## Overview

PR Guard reads a repository's contribution rules (CONTRIBUTING.md, PR template) and checks
a pull request against them, then writes friendly, specific feedback for the contributor.
It runs as a Streamlit web app, uses the GitHub API for real PR data, and uses Groq (free)
as the LLM provider. Three of the five agents run in parallel via Bob subagents.

**Two API keys required:**
- `GROQ_API_KEY` — free at console.groq.com (no credit card)
- `GITHUB_TOKEN` — free personal access token at github.com/settings/tokens (read:repo scope)

---

## Folder Structure

```
pr-guard/
├── .env
├── requirements.txt
├── app.py
├── config.py
├── github_client.py
├── models.py
├── agents/
│   ├── policy_parser.py
│   ├── diff_auditor.py
│   ├── api_extractor.py
│   ├── similar_repos.py
│   └── reporter.py
└── pr-guard-plan.md
```

---

## Data Formats

### ChecklistItem
```python
{
  "rule_id": "rule_001",
  "source": "CONTRIBUTING.md",
  "text": "PR must link to a GitHub issue",
  "severity": "FAIL"   # FAIL | WARN | INFO
}
```

### Finding
```python
{
  "rule_id": "rule_001",
  "status": "FAIL",    # PASS | WARN | FAIL
  "evidence": "No 'Fixes #' found in PR description",
  "suggestion": "Add 'Fixes #NNN' to your PR body"
}
```

### ApiEntry
```python
{
  "entry_type": "endpoint",  # endpoint | env_var | sdk_import | dataset
  "value": "https://api.stripe.com/v1",
  "file": "payment.py",
  "risk": "LOW"   # LOW | MED | HIGH
}
```

### Report (final Markdown string)
Sections: Summary badges → Policy table → API/dependency table → Similar repos list →
Next-steps numbered list.

---

## Constraints & Scope Limits

- GitHub only (no GitLab, no Bitbucket)
- Analyze first 20 changed files max (note truncation in report)
- Similar repos: cap at 5 results, names + stars only (no LLM analysis)
- No auto-fix / no write access to PRs
- Secret detection: pattern-match on diff text only (_KEY, _SECRET, _TOKEN, _PASSWORD)
- LLM: Groq Llama 3 8B (configurable via config.py)

---

## Sub-Tasks

---

### Task 1 — Project Scaffold & Config

**Intent**
Create the repo skeleton, install dependencies, and wire up the two API keys so every
subsequent task has a working foundation to build on.

**Expected Outcomes**
- `requirements.txt` with all packages
- `.env.example` with placeholder keys (safe to commit)
- `config.py` that loads env vars and exposes `get_llm()` returning a LangChain LLM
- `models.py` with the three dataclasses: ChecklistItem, Finding, ApiEntry
- Running `python config.py` prints "Config OK" without errors

**Todo List**
1. Create `pr-guard/` directory
2. Write `requirements.txt`: streamlit, langchain, langchain-groq, PyGithub, python-dotenv, pydantic
3. Write `.env.example` with `GROQ_API_KEY=` and `GITHUB_TOKEN=`
4. Write `config.py`: load dotenv, define `get_llm()` that returns `ChatGroq(model="llama3-8b-8192")`
5. Write `models.py`: define `ChecklistItem`, `Finding`, `ApiEntry` as Python dataclasses
6. Smoke-test: `python config.py` confirms keys are loaded

**Relevant Context**
- LangChain Groq integration: `from langchain_groq import ChatGroq`
- Pydantic or dataclasses both fine; dataclasses preferred for simplicity

**Status:** [x] done

---

### Task 2 — GitHub Client

**Intent**
Centralise all GitHub API calls so agents never call the API directly. This makes
rate-limit handling and mocking for tests easy.

**Expected Outcomes**
- `github_client.py` with functions:
  - `get_pr(repo_full_name, pr_number)` → PyGithub PullRequest object
  - `get_pr_diff(pr)` → raw unified diff string
  - `get_repo_file(repo, filepath)` → file content string or None
  - `get_pr_commits(pr)` → list of commit message strings
  - `parse_pr_url(url)` → (repo_full_name, pr_number) tuple
- Calling `parse_pr_url("https://github.com/owner/repo/pull/42")` returns `("owner/repo", 42)`

**Todo List**
1. Write `github_client.py` importing `PyGithub` and `python-dotenv`
2. Implement `parse_pr_url` using regex or string split
3. Implement `get_pr` using `Github(token).get_repo(name).get_pull(number)`
4. Implement `get_pr_diff` using the PR's diff URL with `requests.get`
5. Implement `get_repo_file` with try/except returning None if file not found
6. Implement `get_pr_commits` returning list of `.commit.message` strings
7. Manual smoke-test against a real public PR

**Relevant Context**
- PyGithub docs: `repo.get_contents(path)` for file fetching
- Diff URL pattern: `pr.diff_url` — fetch with requests + auth header

**Status:** [x] done

---

### Task 3 — Policy Parser Agent

**Intent**
Read the repo's CONTRIBUTING.md and PR template and use the LLM to extract a structured
list of rules the PR must follow.

**Expected Outcomes**
- `agents/policy_parser.py` with `parse_policy(repo) -> list[ChecklistItem]`
- Given a repo with a CONTRIBUTING.md, returns at least: issue-link rule, test rule, commit-format rule
- If no CONTRIBUTING.md exists, returns a sensible default checklist (3–5 universal rules)
- Output is a JSON-serialisable list of ChecklistItem dicts

**Todo List**
1. Write `parse_policy(repo)` that calls `get_repo_file` for both `CONTRIBUTING.md` and `.github/PULL_REQUEST_TEMPLATE.md`
2. Build a LangChain prompt: "Given these contribution rules, extract a JSON list of checklist items..."
3. Parse LLM JSON response into list of ChecklistItem
4. Add fallback: if files missing, return hardcoded default checklist
5. Test against a real open-source repo (e.g., facebook/react)

**Relevant Context**
- models.py: ChecklistItem shape
- Use `ChatGroq` from config.get_llm()
- LLM prompt should request strict JSON output; use `response_format` or ask for ```json blocks

**Status:** [x] done

---

### Task 4 — Diff Auditor Agent

**Intent**
Check the PR's description, commit messages, and changed file list against the checklist
items produced by the Policy Parser. Produce a Finding for each rule.

**Expected Outcomes**
- `agents/diff_auditor.py` with `audit_diff(pr_data, checklist) -> list[Finding]`
- `pr_data` is a dict: `{description, commits, diff, changed_files}`
- For each ChecklistItem, returns a Finding with PASS/WARN/FAIL + evidence + suggestion
- Secret detection: scans diff text for _KEY, _SECRET, _TOKEN, _PASSWORD patterns

**Todo List**
1. Write `audit_diff(pr_data, checklist)` function
2. Build prompt: feed checklist + PR description + commits + first 20 file names; ask for JSON findings
3. Add regex secret scan on `pr_data["diff"]` for sensitive variable name patterns
4. Merge secret-scan results into findings list as additional Finding entries
5. Parse LLM response into list of Finding
6. Test against a PR that is known to be missing an issue link

**Relevant Context**
- models.py: Finding shape
- Diff can be large — truncate to first 8000 chars when passing to LLM
- Secret patterns: `r'(?i)(api_key|secret|token|password)\s*=\s*["\'][^"\']{8,}'`

**Status:** [x] done

---

### Task 5 — API & Dependency Extractor Agent

**Intent**
Scan the changed files in the diff for new third-party API calls, SDK imports, environment
variable references, and hardcoded URLs. Flag anything that needs documentation or review.

**Expected Outcomes**
- `agents/api_extractor.py` with `extract_apis(pr_data) -> list[ApiEntry]`
- Detects: http/https URLs, `import` statements for known cloud SDKs, `os.environ` / `os.getenv` calls
- Assigns risk: env vars named *_KEY/*_SECRET = HIGH; raw URLs = LOW; cloud SDK imports = MED
- Returns list of ApiEntry

**Todo List**
1. Write `extract_apis(pr_data)` that works on `pr_data["diff"]`
2. Add regex scan for URLs: `r'https?://[^\s"\'<>]+'`
3. Add regex scan for env vars: `r'os\.(?:environ|getenv)\(["\'](\w+)["\']'`
4. Add regex scan for SDK imports: `r'^(?:import|from)\s+(boto3|stripe|twilio|openai|anthropic|requests)'`
5. Use LLM for a second pass to classify and deduplicate results
6. Test on a PR that adds a Stripe or AWS call

**Relevant Context**
- models.py: ApiEntry shape
- Risk assignment logic: if entry_type==env_var and value matches `_KEY|_SECRET|_TOKEN` → HIGH

**Status:** [x] done

---

### Task 6 — Similar Repositories Detector

**Intent**
Query GitHub search for the top 5 repositories that are similar to the target repo,
to provide prior art context in the report. This is purely informational — no LLM call.

**Expected Outcomes**
- `agents/similar_repos.py` with `find_similar(repo) -> list[dict]`
- Uses repo's primary language and top topics to build a GitHub search query
- Returns list of up to 5 dicts: `{name, stars, url, description}`
- Gracefully handles rate-limit errors (returns empty list with a note)

**Todo List**
1. Write `find_similar(repo)` using `github_client` Github object
2. Build query from `repo.language` + `repo.get_topics()` (first 2 topics)
3. Call `github.search_repositories(query, sort="stars")` and take first 5
4. Return list of name/stars/url/description dicts
5. Wrap in try/except for rate limit; return `[]` on failure

**Relevant Context**
- PyGithub: `g.search_repositories(query, sort="stars", order="desc")`
- Do NOT pass these repos through the LLM — display only

**Status:** [x] done

---

### Task 7 — Reporter Agent

**Intent**
Assemble all outputs from tasks 3–6 into a single, well-formatted Markdown report that
is friendly and actionable for the contributor.

**Expected Outcomes**
- `agents/reporter.py` with `generate_report(pr, checklist, findings, api_entries, similar_repos) -> str`
- Returns a Markdown string with all five sections (see data formats above)
- Summary line: "✅ N passed | ⚠️ N warnings | ❌ N failed"
- Next-steps section lists only the FAIL and WARN items, numbered

**Todo List**
1. Write `generate_report(...)` that builds Markdown via string templating (no LLM needed here)
2. Count PASS/WARN/FAIL from findings for the summary line
3. Build the policy table from findings
4. Build the API/dependency table from api_entries
5. Build the similar repos list from similar_repos
6. Build numbered next-steps from FAIL items first, then WARN items
7. Test output renders correctly in a Markdown viewer

**Relevant Context**
- No LLM call in this task — pure string assembly
- Use Python f-strings and list comprehensions

**Status:** [x] done

---

### Task 8 — Streamlit Dashboard & Parallel Orchestration

**Intent**
Wire everything together in the Streamlit UI. Policy Parser, Diff Auditor, and API
Extractor run in parallel via Python's `concurrent.futures.ThreadPoolExecutor` (this is
the Bob subagent parallel moment — screenshot opportunity here). Reporter runs after.

**Expected Outcomes**
- `app.py` with a single-page Streamlit UI
- Input: text box for GitHub PR URL + "Run Audit" button
- While running: spinner with step labels
- Output: full Markdown report rendered in-page
- Download button for the Markdown report

**Todo List**
1. Write `app.py` with `st.text_input` for PR URL and `st.button("Run Audit")`
2. On button click: call `parse_pr_url`, then `get_pr`, `get_pr_diff`, `get_pr_commits`
3. Launch Policy Parser, Diff Auditor, and API Extractor in parallel with `ThreadPoolExecutor`
4. After parallel tasks complete: call `find_similar` then `generate_report`
5. Display report with `st.markdown(report)`
6. Add `st.download_button` to save report as `.md` file
7. Add `st.error` handling for bad URLs and API failures
8. Test end-to-end with a real public PR

**Relevant Context**
- `concurrent.futures.ThreadPoolExecutor` + `as_completed` or `map`
- Policy Parser needs `repo` object; Diff Auditor needs `pr_data + checklist`; these are sequential
  at the data-fetch level but the LLM calls inside them can run in parallel
- Streamlit re-runs the script on each interaction — use `st.session_state` to cache the report

**Status:** [x] done

---

## Cut List (do not build)

| Feature | Reason cut |
|---|---|
| Auto-fix / push commits | Requires write permissions; out of scope |
| GitLab / Bitbucket support | Different APIs; not worth the complexity |
| Custom LLM fine-tuning | Weeks of work |
| Deep file-by-file secret scan | Too slow; diff-only scan is sufficient for demo |
| LLM analysis of similar repos | No value add; just display names + stars |

---

## Implementation Notes for Agent Mode

- Build tasks 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 in order
- Each task must be independently testable before moving to the next
- After Task 8 is done, run `streamlit run pr-guard/app.py` to verify the full demo works
- Screenshot moment: Task 8 step 3, when the three agents fire in parallel

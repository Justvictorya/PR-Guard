"""
github_client.py — All GitHub API interactions in one place.

Every agent calls these functions rather than touching PyGithub or requests
directly. This keeps auth, error handling, and rate-limit concerns in a
single file that is easy to mock during testing.
"""

import re
import requests
from github import Github, GithubException
from config import get_github_token


# ---------------------------------------------------------------------------
# Internal — authenticated clients (created once per process)
# ---------------------------------------------------------------------------

def _gh() -> Github:
    """Return an authenticated PyGithub client."""
    return Github(get_github_token())


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def parse_pr_url(url: str) -> tuple[str, int]:
    """Parse a GitHub PR URL into (repo_full_name, pr_number).

    Accepts:
      https://github.com/owner/repo/pull/42
      https://github.com/owner/repo/pull/42/files   (trailing path ignored)

    Returns:
      ("owner/repo", 42)

    Raises:
      ValueError if the URL does not match the expected pattern.
    """
    pattern = r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)"
    match = re.search(pattern, url.strip())
    if not match:
        raise ValueError(
            f"Could not parse PR URL: {url!r}\n"
            "Expected format: https://github.com/owner/repo/pull/NUMBER"
        )
    return match.group(1), int(match.group(2))


# ---------------------------------------------------------------------------
# PR data fetchers
# ---------------------------------------------------------------------------

def get_pr(repo_full_name: str, pr_number: int):
    """Return a PyGithub PullRequest object.

    Args:
        repo_full_name : "owner/repo"
        pr_number      : integer PR number

    Raises:
        GithubException on auth or not-found errors.
    """
    repo = _gh().get_repo(repo_full_name)
    return repo.get_pull(pr_number)


def get_pr_diff(pr) -> str:
    """Fetch the raw unified diff for a PR as a string.

    Falls back to an empty string if the diff cannot be retrieved,
    so downstream agents degrade gracefully instead of crashing.
    """
    headers = {
        "Authorization": f"token {get_github_token()}",
        "Accept": "application/vnd.github.v3.diff",
    }
    try:
        response = requests.get(pr.diff_url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        print(f"[github_client] Warning: could not fetch diff — {exc}")
        return ""


def get_pr_commits(pr) -> list[str]:
    """Return a list of commit message strings for the PR.

    Trims whitespace; filters out empty strings.
    """
    return [
        c.commit.message.strip()
        for c in pr.get_commits()
        if c.commit.message.strip()
    ]


def get_pr_changed_files(pr) -> list[str]:
    """Return a list of changed file paths (relative to repo root).

    Capped at 20 files per the plan's scope limit.
    """
    files = list(pr.get_files())[:20]
    return [f.filename for f in files]


def get_repo_file(repo, filepath: str) -> str | None:
    """Fetch the decoded text content of a file from the repo's default branch.

    Returns None if the file does not exist, rather than raising.
    Common filepaths to try:
      - CONTRIBUTING.md
      - .github/PULL_REQUEST_TEMPLATE.md
      - .github/pull_request_template.md
    """
    try:
        contents = repo.get_contents(filepath)
        return contents.decoded_content.decode("utf-8", errors="replace")
    except GithubException:
        return None


# ---------------------------------------------------------------------------
# Convenience bundle — everything agents need about a PR in one call
# ---------------------------------------------------------------------------

def fetch_pr_data(url: str) -> dict:
    """Parse the URL, fetch all PR data, and return a single dict.

    Shape:
    {
        "repo_full_name" : str,
        "pr_number"      : int,
        "title"          : str,
        "description"    : str,   # PR body (may be empty string)
        "commits"        : list[str],
        "changed_files"  : list[str],
        "diff"           : str,
        "pr_obj"         : PullRequest,   # raw PyGithub object
        "repo_obj"       : Repository,    # raw PyGithub object
    }
    """
    repo_full_name, pr_number = parse_pr_url(url)
    gh = _gh()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    return {
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "title": pr.title or "",
        "description": pr.body or "",
        "commits": get_pr_commits(pr),
        "changed_files": get_pr_changed_files(pr),
        "diff": get_pr_diff(pr),
        "pr_obj": pr,
        "repo_obj": repo,
    }


# ---------------------------------------------------------------------------
# Smoke-test (run directly: python github_client.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    TEST_PR = "https://github.com/pallets/flask/pull/5522"

    print(f"Parsing URL: {TEST_PR}")
    name, num = parse_pr_url(TEST_PR)
    print(f"  repo={name!r}  pr_number={num}")

    print("Fetching PR data (requires GITHUB_TOKEN in .env)...")
    data = fetch_pr_data(TEST_PR)
    print(f"  Title          : {data['title']}")
    print(f"  Commits        : {len(data['commits'])} — first: {data['commits'][0][:60]!r}")
    print(f"  Changed files  : {data['changed_files'][:3]} ...")
    print(f"  Diff length    : {len(data['diff'])} chars")
    contrib = get_repo_file(data["repo_obj"], "CONTRIBUTING.rst")
    print(f"  CONTRIBUTING   : {'found' if contrib else 'not found'} ({len(contrib or '')} chars)")
    print("\ngithub_client.py OK ✓")

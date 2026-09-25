"""
github_client.py — All GitHub API interactions in one place.

Every agent calls these functions rather than touching PyGithub or requests
directly. This keeps auth, error handling, and rate-limit concerns in a
single file that is easy to mock during testing.

Supports two URL modes:
  PR mode   — https://github.com/owner/repo/pull/42
  Repo mode — https://github.com/owner/repo
"""

import re
import requests
from github import Github, GithubException
from config import get_github_token

# Audit mode constants consumed by app.py and reporter.py
MODE_PR   = "pr"
MODE_REPO = "repo"


# ---------------------------------------------------------------------------
# Internal — authenticated clients (created once per process)
# ---------------------------------------------------------------------------

def _gh() -> Github:
    """Return an authenticated PyGithub client."""
    return Github(get_github_token())


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def classify_url(url: str) -> str:
    """Return MODE_PR or MODE_REPO depending on the URL shape.

    Raises ValueError if the URL is not a recognised GitHub URL.
    """
    url = url.strip()
    if re.search(r"https://github\.com/[^/]+/[^/]+/pull/\d+", url):
        return MODE_PR
    if re.search(r"https://github\.com/[^/]+/[^/]+/?$", url):
        return MODE_REPO
    raise ValueError(
        f"Not a recognised GitHub URL: {url!r}\n"
        "Accepted formats:\n"
        "  • https://github.com/owner/repo/pull/42  (pull request)\n"
        "  • https://github.com/owner/repo           (repository)"
    )


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


def parse_repo_url(url: str) -> str:
    """Parse a GitHub repo URL into repo_full_name ('owner/repo').

    Raises ValueError if the URL does not match.
    """
    pattern = r"https://github\.com/([^/]+/[^/?#]+)"
    match = re.search(pattern, url.strip())
    if not match:
        raise ValueError(f"Could not parse repo URL: {url!r}")
    return match.group(1).rstrip("/")


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
# Convenience bundles — one call per mode
# ---------------------------------------------------------------------------

def fetch_pr_data(url: str) -> dict:
    """Parse the URL, fetch all PR data, and return a single dict.

    Shape:
    {
        "mode"           : "pr",
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
        "mode": MODE_PR,
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


def fetch_repo_data(url: str) -> dict:
    """Fetch top-level repo metadata for a general repository URL.

    Collects the README, CONTRIBUTING.md, PR template, and a list of
    root-level files to give the agents enough context for a structural audit.

    Shape:
    {
        "mode"           : "repo",
        "repo_full_name" : str,
        "pr_number"      : None,
        "title"          : str,   # repo description or name
        "description"    : str,   # README excerpt (first 2000 chars)
        "commits"        : list[str],  # last 5 default-branch commit messages
        "changed_files"  : list[str],  # root-level file names
        "diff"           : str,   # empty — no PR diff available
        "pr_obj"         : None,
        "repo_obj"       : Repository,
    }
    """
    repo_full_name = parse_repo_url(url)
    gh = _gh()
    repo = gh.get_repo(repo_full_name)

    # README excerpt
    readme = get_repo_file(repo, "README.md") or get_repo_file(repo, "README.rst") or ""

    # Root-level file listing (capped at 20)
    try:
        root_contents = repo.get_contents("")
        root_files = [c.path for c in root_contents if c.type == "file"][:20]
    except GithubException:
        root_files = []

    # Last 5 commits on the default branch
    try:
        commits = [
            c.commit.message.strip().splitlines()[0]   # first line only
            for c in repo.get_commits()[:5]
            if c.commit.message.strip()
        ]
    except GithubException:
        commits = []

    return {
        "mode": MODE_REPO,
        "repo_full_name": repo_full_name,
        "pr_number": None,
        "title": repo.description or repo.name,
        "description": readme[:2000],
        "commits": commits,
        "changed_files": root_files,
        "diff": "",        # no diff for repo-mode
        "pr_obj": None,
        "repo_obj": repo,
    }


def fetch_data(url: str) -> dict:
    """Auto-detect URL type and call the right fetcher.

    This is the single entry point app.py should use.
    Returns the same dict shape regardless of mode; check data["mode"].
    """
    mode = classify_url(url)
    if mode == MODE_PR:
        return fetch_pr_data(url)
    return fetch_repo_data(url)


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

"""
agents/similar_repos.py — Similar Repositories Detector.

Queries the GitHub Search API for up to 5 repositories that are similar
to the target repo, using its primary language and top topics as search
terms.  Results are returned as plain dicts — no LLM call is made.

Shape of each result dict:
  {
    "name"        : str,   e.g. "stripe/stripe-python"
    "url"         : str,   e.g. "https://github.com/stripe/stripe-python"
    "stars"       : int,
    "language"    : str | None,
    "description" : str | None,
  }

Gracefully returns [] (with a printed warning) on any API or rate-limit
error so the report always completes even without this section.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import get_github_token

MAX_RESULTS = 5


def find_similar(repo) -> list[dict]:
    """Return up to 5 similar repos for the given PyGithub Repository object.

    Search strategy:
      1. Use the repo's primary language (if set).
      2. Use the first two topics (if any).
      3. Fall back to the repo's name keywords if neither is available.

    Args:
        repo : PyGithub Repository object (from github_client.fetch_pr_data)

    Returns:
        List of up to 5 dicts with name, url, stars, language, description.
        Returns [] on any error.
    """
    from github import Github, GithubException, RateLimitExceededException

    try:
        gh = Github(get_github_token())

        # Build search query
        terms: list[str] = []

        language = getattr(repo, "language", None)
        if language:
            terms.append(f"language:{language}")

        try:
            topics = repo.get_topics()[:2]
            terms.extend(f"topic:{t}" for t in topics)
        except Exception:
            topics = []

        # If we have nothing to search on, fall back to repo name words
        if not terms:
            words = [
                w for w in repo.name.replace("-", " ").replace("_", " ").split()
                if len(w) > 3
            ]
            terms = words[:3]

        if not terms:
            print("[similar_repos] Not enough repo metadata to build a query.")
            return []

        query = " ".join(terms)
        own_full_name = repo.full_name.lower()

        results: list[dict] = []
        try:
            search = gh.search_repositories(query, sort="stars", order="desc")
            for r in search:
                if r.full_name.lower() == own_full_name:
                    continue  # skip the repo itself
                results.append({
                    "name":        r.full_name,
                    "url":         r.html_url,
                    "stars":       r.stargazers_count,
                    "language":    r.language,
                    "description": (r.description or "")[:120],
                })
                if len(results) >= MAX_RESULTS:
                    break
        except RateLimitExceededException:
            print("[similar_repos] GitHub rate limit hit — skipping similar repos.")
            return []

        return results

    except Exception as exc:
        print(f"[similar_repos] Error fetching similar repos: {exc}")
        return []


# ---------------------------------------------------------------------------
# Smoke-test (run directly: python agents/similar_repos.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("find_similar() smoke-test (requires GITHUB_TOKEN in .env)\n")

    class _MockRepo:
        full_name = "pallets/flask"
        name = "flask"
        language = "Python"
        def get_topics(self): return ["flask", "web", "python"]

    results = find_similar(_MockRepo())
    if results:
        for r in results:
            print(f"  ★ {r['stars']:>6,}  {r['name']:<40}  [{r['language']}]")
            if r['description']:
                print(f"           {r['description']}")
    else:
        print("  (no results — check GITHUB_TOKEN or rate limit)")

    print("\nagents/similar_repos.py OK ✓")

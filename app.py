"""
app.py — PR Guard Streamlit Dashboard.

Entry point for the web UI. Paste a GitHub PR URL, click "Run Audit",
and get a complete Markdown review report.

Run with:
    streamlit run app.py

Parallel execution:
    Policy Parser, Diff Auditor, and API Extractor are launched in parallel
    using ThreadPoolExecutor.  Similar Repos runs sequentially after because
    it only needs the repo object (which is already available), and its
    GitHub Search call is independent of the LLM results.

    ┌─────────────────────────────────────────────────────────┐
    │  GitHub fetch (sequential — each step needs the prev)   │
    │    parse_url → get_repo → get_pr → get_diff/commits     │
    └──────────────────────────┬──────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
       Policy Parser     Diff Auditor      API Extractor   ← PARALLEL
       (needs repo)     (needs pr_data    (needs pr_data
                         + checklist*)     only)
              │                │                 │
              └────────────────┴─────────────────┘
                               │
                        ┌──────▼──────┐
                        │ Similar     │  (sequential, fast)
                        │ Repos       │
                        └──────┬──────┘
                               │
                        ┌──────▼──────┐
                        │  Reporter   │  (no LLM, instant)
                        └─────────────┘

* Diff Auditor needs the checklist from Policy Parser, so it starts
  immediately after Policy Parser completes (still within the parallel
  window alongside API Extractor).
"""

import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st

# ---------------------------------------------------------------------------
# Path setup so imports work when run from any directory
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))

from config import get_github_token, get_llm
from github_client import fetch_pr_data, get_repo_file
from agents.policy_parser import parse_policy
from agents.diff_auditor import audit_diff
from agents.api_extractor import extract_apis
from agents.similar_repos import find_similar
from agents.reporter import generate_report

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="PR Guard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Custom CSS — minimal, clean
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .main .block-container { max-width: 900px; padding-top: 2rem; }
    .stAlert { border-radius: 6px; }
    .step-label { color: #57606a; font-size: 0.85rem; margin-bottom: 0.25rem; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("🛡️ PR Guard")
st.caption("Automated pull request auditor — checks your PR against the repo's contribution rules.")

st.divider()

# ---------------------------------------------------------------------------
# Input form
# ---------------------------------------------------------------------------
with st.form("audit_form"):
    pr_url = st.text_input(
        "GitHub Pull Request URL",
        placeholder="https://github.com/owner/repo/pull/42",
        help="Paste any public GitHub PR URL. Private repos require a token with read:repo scope.",
    )
    submitted = st.form_submit_button("🔍 Run Audit", type="primary", use_container_width=True)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _check_env() -> list[str]:
    """Return a list of missing environment variable names."""
    missing = []
    if not os.getenv("GITHUB_TOKEN"):
        missing.append("GITHUB_TOKEN")
    if not os.getenv("GROQ_API_KEY") and os.getenv("LLM_PROVIDER", "groq") == "groq":
        missing.append("GROQ_API_KEY")
    return missing


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_audit(url: str) -> str:
    """Full audit pipeline. Returns the Markdown report string."""

    progress = st.progress(0, text="Starting audit…")
    status   = st.status("Running PR Guard…", expanded=True)

    def log(msg: str):
        status.write(msg)

    try:
        # ── Step 1: Fetch PR data from GitHub ──────────────────────────────
        progress.progress(10, text="Fetching PR from GitHub…")
        log("📡 Fetching PR data from GitHub…")
        t0 = time.time()
        pr_data = fetch_pr_data(url)
        log(f"   ✅ Fetched PR #{pr_data['pr_number']}: **{pr_data['title']}** "
            f"({len(pr_data['diff'])} diff chars, "
            f"{len(pr_data['commits'])} commits, "
            f"{len(pr_data['changed_files'])} files) "
            f"in {time.time()-t0:.1f}s")

        repo = pr_data["repo_obj"]

        # ── Step 2: Parallel — Policy Parser + API Extractor ───────────────
        # Policy Parser and API Extractor are fully independent and run at
        # the same time.  Diff Auditor needs the checklist, so it starts
        # as soon as Policy Parser finishes (still parallel with API Extractor
        # if API Extractor is still running).
        #
        # 🎯 SCREENSHOT MOMENT: three futures submitted simultaneously below.

        progress.progress(25, text="Running agents in parallel…")
        log("⚡ Launching parallel agents: Policy Parser + API Extractor…")

        checklist = None
        api_entries = None
        findings = None

        with ThreadPoolExecutor(max_workers=3) as executor:

            future_policy = executor.submit(parse_policy, repo)
            future_apis   = executor.submit(extract_apis, pr_data)

            # Wait for Policy Parser first so Diff Auditor can start
            for future in as_completed([future_policy, future_apis]):
                if future is future_policy:
                    checklist = future.result()
                    log(f"   ✅ Policy Parser: {len(checklist)} rules extracted")
                    progress.progress(45, text="Policy parsed — launching Diff Auditor…")

                    # Now submit Diff Auditor (still parallel with API Extractor)
                    future_audit = executor.submit(audit_diff, pr_data, checklist)

                elif future is future_apis:
                    api_entries = future.result()
                    log(f"   ✅ API Extractor: {len(api_entries)} entries found")

            # Collect remaining futures
            if findings is None:
                findings = future_audit.result()
                log(f"   ✅ Diff Auditor: {len(findings)} findings")
                progress.progress(70, text="Diff audited…")

            if api_entries is None:
                api_entries = future_apis.result()
                log(f"   ✅ API Extractor: {len(api_entries)} entries found")

        # ── Step 3: Similar Repos (sequential, no LLM) ─────────────────────
        progress.progress(80, text="Searching for similar repositories…")
        log("🔍 Searching GitHub for similar repositories…")
        similar = find_similar(repo)
        log(f"   ✅ Similar Repos: {len(similar)} found")

        # ── Step 4: Generate report ─────────────────────────────────────────
        progress.progress(95, text="Generating report…")
        log("📝 Assembling report…")
        report = generate_report(pr_data, checklist, findings, api_entries, similar)

        progress.progress(100, text="Done!")
        status.update(label="✅ Audit complete!", state="complete", expanded=False)

        return report

    except Exception as exc:
        status.update(label="❌ Audit failed", state="error", expanded=True)
        progress.empty()
        raise exc


# ---------------------------------------------------------------------------
# Main — run on form submit
# ---------------------------------------------------------------------------

if submitted:
    # Pre-flight: check env vars
    missing = _check_env()
    if missing:
        st.error(
            f"**Missing environment variables:** {', '.join(missing)}\n\n"
            "Copy `.env.example` to `.env` and fill in your API keys, then restart Streamlit."
        )
        st.stop()

    if not pr_url.strip():
        st.warning("Please enter a GitHub PR URL.")
        st.stop()

    try:
        report = run_audit(pr_url.strip())
        st.session_state["last_report"] = report
        st.session_state["last_url"]    = pr_url.strip()
    except ValueError as e:
        st.error(f"**Invalid URL:** {e}")
        st.stop()
    except Exception as e:
        st.error(f"**Audit failed:** {e}")
        st.stop()

# Display cached report (survives re-renders)
if "last_report" in st.session_state:
    st.divider()

    col1, col2 = st.columns([6, 1])
    with col1:
        st.subheader("Audit Report")
    with col2:
        st.download_button(
            label="⬇️ Download .md",
            data=st.session_state["last_report"],
            file_name="pr-guard-report.md",
            mime="text/markdown",
            use_container_width=True,
        )

    st.markdown(st.session_state["last_report"])

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
from github_client import fetch_data, get_repo_file, MODE_PR, MODE_REPO
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
# Custom CSS — PR Guard brand identity (purple #7C3AED palette)
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    /* Layout */
    .main .block-container { max-width: 900px; padding-top: 2rem; }
    .stAlert { border-radius: 6px; }
    .step-label { color: #D8B4FE; font-size: 0.85rem; margin-bottom: 0.25rem; }

    /* Primary button — Run Audit */
    div.stButton > button[kind="primary"],
    div.stFormSubmitButton > button[kind="primary"] {
        background-color: #7C3AED !important;
        border: none !important;
        color: #F3F4F6 !important;
        font-weight: 600 !important;
        border-radius: 8px !important;
        transition: background-color 0.2s ease !important;
    }
    div.stButton > button[kind="primary"]:hover,
    div.stFormSubmitButton > button[kind="primary"]:hover {
        background-color: #6D28D9 !important;
        color: #ffffff !important;
    }

    /* Download button */
    div.stDownloadButton > button {
        background-color: #1E222D !important;
        border: 1px solid #7C3AED !important;
        color: #D8B4FE !important;
        border-radius: 8px !important;
        transition: background-color 0.2s ease !important;
    }
    div.stDownloadButton > button:hover {
        background-color: #7C3AED !important;
        color: #ffffff !important;
    }

    /* Input field — purple focus border */
    div[data-baseweb="input"] input:focus,
    div[data-baseweb="textarea"] textarea:focus {
        border-color: #7C3AED !important;
        box-shadow: 0 0 0 2px rgba(124, 58, 237, 0.25) !important;
    }

    /* Text input container */
    div[data-baseweb="input"],
    div[data-baseweb="textarea"] {
        border-radius: 8px !important;
    }

    /* Lavender accents on captions and helper text */
    .stCaption, small { color: #D8B4FE !important; }

    /* Markdown report — code blocks get subtle purple border-left */
    .stMarkdown pre {
        border-left: 3px solid #7C3AED;
        padding-left: 1rem;
        background-color: #1E222D;
    }

    /* Divider */
    hr { border-color: #7C3AED33 !important; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Header — logo + title side by side
# ---------------------------------------------------------------------------
_logo_path = os.path.join(os.path.dirname(__file__), "pr-guard-logo.svg")
_col_logo, _col_title = st.columns([1, 8])
with _col_logo:
    if os.path.exists(_logo_path):
        with open(_logo_path) as _f:
            st.markdown(
                f'<div style="padding-top:0.25rem">{_f.read()}</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown("🛡️", unsafe_allow_html=False)
with _col_title:
    st.title("PR Guard")
    st.caption("Automated pull request auditor — checks your PR against the repo's contribution rules.")

st.divider()

# ---------------------------------------------------------------------------
# Input form
# ---------------------------------------------------------------------------
with st.form("audit_form"):
    pr_url = st.text_input(
        "GitHub URL",
        placeholder="https://github.com/owner/repo/pull/42  or  https://github.com/owner/repo",
        help=(
            "Paste a **PR URL** to audit a specific pull request, "
            "or a **repo URL** to run a general compliance audit on the codebase."
        ),
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
        # ── Step 1: Detect mode + fetch data from GitHub ───────────────────
        progress.progress(10, text="Fetching data from GitHub…")
        log("📡 Fetching data from GitHub…")
        t0 = time.time()
        pr_data = fetch_data(url)
        mode = pr_data["mode"]

        if mode == MODE_PR:
            log(f"   🔀 **PR audit mode** — PR #{pr_data['pr_number']}: **{pr_data['title']}** "
                f"({len(pr_data['diff'])} diff chars, "
                f"{len(pr_data['commits'])} commits, "
                f"{len(pr_data['changed_files'])} files) "
                f"in {time.time()-t0:.1f}s")
        else:
            log(f"   📁 **Repository audit mode** — `{pr_data['repo_full_name']}` "
                f"({len(pr_data['changed_files'])} root files, "
                f"{len(pr_data['commits'])} recent commits) "
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
        st.warning("Please enter a GitHub URL (PR or repository).")
        st.stop()

    try:
        report = run_audit(pr_url.strip())
        st.session_state["last_report"] = report
        st.session_state["last_url"]    = pr_url.strip()
        # store mode for the badge (run_audit doesn't return it directly;
        # we infer it from the URL after a successful run)
        from github_client import classify_url
        try:
            st.session_state["last_mode"] = classify_url(pr_url.strip())
        except Exception:
            st.session_state["last_mode"] = MODE_PR
    except ValueError as e:
        st.error(f"**Invalid URL:** {e}")
        st.stop()
    except Exception as e:
        st.error(f"**Audit failed:** {e}")
        st.stop()

# Display cached report (survives re-renders)
if "last_report" in st.session_state:
    st.divider()

    # Mode badge
    _mode = st.session_state.get("last_mode", MODE_PR)
    if _mode == MODE_PR:
        st.info("🔀 **Pull Request Audit** — results reflect the specific PR diff and commits.", icon="🔀")
    else:
        st.info("📁 **Repository Audit** — results reflect the repo's overall structure and contribution guidelines.", icon="📁")

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

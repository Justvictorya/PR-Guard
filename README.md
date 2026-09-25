# 🛡️ PR Guard

**AI-powered code review coach for auditing pull requests against repository guidelines, detecting secrets, and identifying prior art.**

Built for the **IBM Bob 2.0 Hackathon**.

## 🚀 Overview
PR Guard automatically audits GitHub Pull Requests using a parallel multi-agent system powered by IBM Bob 2.0. It checks PR diffs against repository guidelines (`CONTRIBUTING.md`, security policies), identifies leaked credentials or sensitive data, extracts API & dependency changes, and searches for similar open-source repositories to avoid duplicate work.

## 🏗️ Architecture
PR Guard uses a multi-agent parallel execution pipeline:
* **Policy Parser Agent**: Scans repo guidelines and extracts checklist rules.
* **Diff Auditor Agent**: Inspects pull request diffs for policy violations and leaked secrets.
* **API Extractor Agent**: Extracts new or modified API endpoints, routes, and dependencies.
* **Similar Repos Detector**: Queries GitHub for prior art and related repositories.
* **Reporter Agent**: Assembles findings into a structured, markdown-formatted code review report.

Evidence of architectural planning and execution using IBM Bob can be found in the [`bob_sessions/`](./bob_sessions/) folder.

## 🛠️ Getting Started

### 1. Clone the repository
```bash
git clone https://github.com/Justvictorya/PR-Guard.git
cd PR-Guard
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Set up environment variables
Copy `.env.example` to `.env` and fill in your keys:
```bash
GROQ_API_KEY=your_groq_api_key
GITHUB_TOKEN=your_github_token
```

### 4. Run the Streamlit Dashboard
```bash
streamlit run app.py
```

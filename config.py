"""
config.py — Centralised configuration and LLM factory for PR Guard.

Loads environment variables from .env and exposes get_llm() which returns
a LangChain chat model ready to use. LLM provider is configurable via
LLM_PROVIDER and LLM_MODEL in .env (defaults to Groq / llama3-8b-8192).
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_llm():
    """Return a configured LangChain chat model.

    Supported providers (set LLM_PROVIDER in .env):
      - groq   → ChatGroq  (free tier, recommended for hackathon)
      - openai → ChatOpenAI
    """
    provider = os.getenv("LLM_PROVIDER", "groq").lower()

    if provider == "groq":
        from langchain_groq import ChatGroq

        model = os.getenv("LLM_MODEL", "llama3-8b-8192")
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "GROQ_API_KEY is not set. "
                "Get a free key at https://console.groq.com and add it to your .env file."
            )
        return ChatGroq(model=model, api_key=api_key, temperature=0)

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "OPENAI_API_KEY is not set. Add it to your .env file."
            )
        return ChatOpenAI(model=model, api_key=api_key, temperature=0)

    raise ValueError(
        f"Unknown LLM_PROVIDER '{provider}'. Supported values: groq, openai"
    )


def get_github_token() -> str:
    """Return the GitHub personal access token from the environment."""
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN is not set. "
            "Create a free token at https://github.com/settings/tokens "
            "(read:repo scope) and add it to your .env file."
        )
    return token


# ---------------------------------------------------------------------------
# Smoke-test (run directly: python config.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Checking environment variables...")

    token = get_github_token()
    print(f"  GITHUB_TOKEN : {'*' * 8}{token[-4:]}")

    llm = get_llm()
    print(f"  LLM          : {llm.__class__.__name__} / {os.getenv('LLM_MODEL', 'default')}")

    print("\nConfig OK ✓")

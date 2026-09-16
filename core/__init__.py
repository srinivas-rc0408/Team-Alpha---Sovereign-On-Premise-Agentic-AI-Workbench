"""Aegis core — air-gapped refinery agent (LangGraph + local Ollama).

Telemetry is force-disabled here, at package import, which is the earliest point
any `core.*` module can reach — and therefore before langchain/langsmith are
imported and read these variables. They are hard assignments, not setdefault: a
stale .env or an inherited shell export must not be able to switch phone-home
behaviour back on in an air-gapped build.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Anchored to the repo root rather than the CWD, so `python -m core.agent` and
# the Streamlit app load the same .env no matter where they're launched from.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TELEMETRY_OFF = {
    "LANGCHAIN_TRACING_V2": "false",
    "LANGCHAIN_TRACING": "false",
    "LANGSMITH_TRACING": "false",
    "ANONYMIZED_TELEMETRY": "false",
    "SCARF_NO_ANALYTICS": "true",
    "DO_NOT_TRACK": "1",
    "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
}

os.environ.update(TELEMETRY_OFF)

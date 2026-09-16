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
    "LANGCHAIN_ENDPOINT": "",
    "ANONYMIZED_TELEMETRY": "false",
    "SCARF_NO_ANALYTICS": "true",
    "DO_NOT_TRACK": "1",
    "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    # No model is ever fetched at runtime — weights come from Ollama, which was
    # populated once by install.sh. These stop any transitive dependency from
    # reaching a model hub on import or first use.
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}

os.environ.update(TELEMETRY_OFF)


def _normalise_ollama_host() -> str:
    """0.0.0.0 is a *listen* address, not a destination. Connecting to it happens
    to work on Linux and fails outright on macOS/Windows, so a .env carrying it
    would silently break the cross-platform promise. Rewrite it to loopback here,
    at the same layer that pins telemetry off, rather than trusting every caller."""
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").strip()
    for unspecified in ("0.0.0.0", "[::]", "::"):
        if unspecified in host:
            host = host.replace(unspecified, "127.0.0.1")
    if "//" not in host:
        host = f"http://{host}"
    os.environ["OLLAMA_HOST"] = host
    return host


OLLAMA_HOST = _normalise_ollama_host()

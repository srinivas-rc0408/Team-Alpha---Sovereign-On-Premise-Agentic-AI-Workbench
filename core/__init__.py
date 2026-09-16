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

def _keep_alive_seconds() -> int:
    """How long Ollama keeps a model in memory after a request, in SECONDS.

    Ollama's default is 5 minutes, so any pause longer than that — every question in
    a demo, most of a plant shift — reloads a 5 GB model first: measured 4.1s cold vs
    0.5s warm per call on a 4 GB RTX 3050 Ti, and one query makes several calls.
    Sent with every request, so it holds however the Ollama service was started, and
    Ollama still evicts a model early when another needs the memory — this cannot
    exhaust the GPU.

    Seconds as an int, not Ollama's "60m" duration string: OllamaEmbeddings types
    this field as int-only and rejects the string outright, while ChatOllama and the
    raw client accept either. An int is the one form all three take. The env var
    still accepts the friendly "30m"/"2h" spelling people expect from Ollama."""
    raw = os.getenv("OLLAMA_KEEP_ALIVE", "60m").strip().lower() or "60m"
    units = {"s": 1, "m": 60, "h": 3600}
    try:
        if raw[-1] in units:
            return max(1, int(float(raw[:-1]) * units[raw[-1]]))
        return max(1, int(float(raw)))  # bare number: seconds, as Ollama reads it
    except ValueError:
        return 3600


KEEP_ALIVE = _keep_alive_seconds()

# AEGIS — Air-Gapped Engineering Intelligence System

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)
![SIH 2026](https://img.shields.io/badge/SIH-2026-orange)
![Air-Gapped](https://img.shields.io/badge/network-air--gapped-black)
![Status](https://img.shields.io/badge/status-prototype-yellow)

SIH26117 · Team Alpha · Sovereign On-Premise Agentic AI Workbench using
Open-Weight Multimodal LLMs for Confidential Industrial Work.

**What is this?** A LangGraph agent for refinery operators that answers
questions grounded in internal SOPs, runs verification math in a sandbox, and
gates safety-critical findings behind a named human sign-off — entirely on
local Ollama models, with no network dependency at query time.

A local-only agent that answers operator questions ("is this pressure reading a
violation?") by planning a multi-step job, retrieving grounded SOP context,
running verification code in an isolated sandbox, checking any safety
thresholds with plain deterministic code (never an LLM judgment call), and
reflecting before it answers — with every step written to a tamper-evident
audit log. No query, document, or model weight ever leaves the machine; the UI
binds to `127.0.0.1` only, and a network audit runs on every query to prove it
(see `core/network_monitor.py`).

## Quickstart

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
ollama pull qwen2.5:7b && ollama pull moondream && ollama pull nomic-embed-text
# .env: OLLAMA_HOST=http://localhost:11434 (plus optional overrides — see table below)

python test_aegis.py          # smoke test: models, docker sandbox, RAG, LLM
streamlit run ui/app.py       # the demo UI
```

## Architecture

```
Plan → Vision (if image attached) → RAG → Calc → Safety Check → Reflect ──┬─→ Answer
                    ^                                                     │
                    └───────────────────── retry (≤2x) ────────────────────┘
```

Every node logs to `logs/audit.jsonl` (SHA-256 hash-chained — `audit.verify()`
finds the first broken link if any past entry is edited). Every `run()` call
also runs a network audit (`core/network_monitor.py`) over its own process
tree and logs a `NETWORK_AUDIT` entry — see ARCHITECTURE.md for details.

**Safety check is deterministic, not an LLM judgment.** The planner LLM only
extracts which check applies and the numbers involved (grounded in the query
and retrieved SOP text — the prompt explicitly forbids inventing a threshold);
`core/safety_rules.py`'s plain comparisons decide the verdict. A hallucinated
extraction can point at the wrong check, but it can never talk its way past a
CRITICAL/EXCEEDS/BELOW_MINIMUM verdict — that's one of three independent gates
on `requires_approval` (the others: reflect's own judgment, and a keyword
backstop over the rendered answer).

## Demo

```
streamlit run ui/app.py --server.address 127.0.0.1   # loopback only, not LAN-exposed
```

_Screenshot: see `screenshots/` (placeholder — add one from your run)._

| Deck component | This prototype | Why |
|---|---|---|
| vLLM · Qwen2.5-Coder-32B | Ollama · qwen2.5:7b | Fits a single consumer GPU; vLLM/32B is the scale-up path once on dedicated server hardware |
| Qwen2.5-VL-7B (defect boxes) | Ollama · moondream (text description) | moondream is the model actually installed; swap the model name in `VISION_MODEL` to upgrade |
| LanceDB + bge-m3 hybrid retrieval | FAISS + BM25 hybrid (`nomic-embed-text` + `rank-bm25`, reciprocal-rank fusion) | Same hybrid dense+keyword property the deck claims (catches exact SOP IDs like "SOP-MNT-402" that pure-vector search misses), without standing up a new vector DB |
| Next.js + React Flow live DAG | Streamlit | One file, no frontend build step, fast to demo |
| FastAPI + Redis | direct Python calls | No queue/service boundary needed at single-user prototype scale |
| Docker/gVisor sandbox, network=none | Docker, network=none (no gVisor runtime installed) | Real container isolation for the calc/code tool: `--network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges` + memory/pids/cpu limits. Falls back to an in-process math-only `eval()` if the Docker daemon isn't reachable, so a live demo never hard-fails on infra. gVisor (`runsc`) would add syscall-level isolation on top — not installed here |
| "Hard iteration cap (max 4 cycles)" | Reflect can loop back to RAG at most `MAX_LOOPS=2` times (`core/agent.py`) | Implemented as a real conditional edge in the LangGraph state machine, not just a deck claim |
| "Human-in-the-loop sign-off" | Reflect + a deterministic keyword backstop (`core/agent.py:_mentions_safety_risk`) flag `requires_approval`; the UI blocks on a named supervisor sign-off, logged as its own audit event | The LLM's own approval judgment is non-deterministic on identical input — verified by running the same violation query 3x with 3 different reflect outputs, all flagged (2 by the LLM, 1 by the keyword backstop). Never gate safety sign-off on a single LLM call alone |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | local Ollama server |
| `LLM_MODEL` | `qwen2.5:7b` | reasoning/tool-calling model |
| `VISION_MODEL` | `moondream` | image description model |
| `EMBED_MODEL` | `nomic-embed-text` | embedding model for RAG |
| `DOCS_DIR` | `docs` | SOPs to index |
| `INDEX_DIR` | `data/embeddings` | FAISS + BM25 index output |
| `RAG_TOP_K` | `4` | retrieved chunks per query |
| `AUDIT_LOG` | `logs/audit.jsonl` | hash-chained audit log path |
| `SANDBOX_IMAGE` | `python:3.12-slim` | Docker image for the sandboxed calc tool |
| `SANDBOX_TIMEOUT` | `10` | seconds before the sandbox run is killed |
| `SAFETY_LIMITS_FILE` | `config/safety_limits.json` | operator-supplied reference limits — copy from `config/safety_limits.example.json` and fill in numbers verified from your own SOPs/standards; never pre-populated by AEGIS itself |

## Project layout

```
core/agent.py            — LangGraph state machine (plan/vision/rag/calc/safety_check/reflect/answer)
core/rag.py              — hybrid FAISS + BM25 retrieval over docs/
core/tools.py            — vision (moondream), sandboxed calc, RAG search
core/audit.py            — SHA-256 hash-chained JSONL audit log
core/safety_rules.py     — deterministic pressure/temperature/vibration/wall-thickness checks, no LLM
core/network_monitor.py  — per-query air-gap audit (external connections, byte counts)
core/differ.py           — SOP/P&ID revision diffing + LLM-written, diff-grounded MOC impact summary
ui/app.py                — Streamlit demo: example queries, live per-step pipeline, sign-off gate,
                           audit/sandbox/network status, document comparison tab
config/                  — safety_limits.example.json: template for your own verified thresholds
docs/                    — SOPs to index (PDF/TXT/MD)
test_aegis.py            — end-to-end smoke test
```

## Known gaps vs. the full pitch deck

- No LanceDB, FastAPI/Redis, Next.js/React Flow, or gVisor — see the table above for what stands in for each and why.
- Vision returns a text description, not structured defect bounding boxes (needs Qwen2.5-VL; only moondream is installed here).
- Single-user, single-process — no RBAC/multi-tenant plant-DMZ deployment yet.
- `safety_rules.py`'s vibration/wall-thickness checkers are generic threshold evaluators, not preloaded with ISO 10816-3 (or any other) table values — the limits must come from the query, retrieved SOP text, or your own `config/safety_limits.json`, so the system never asserts an unverified regulatory number as fact.
- `core/differ.py` splits documents into sections on blank lines and pairs revisions by text similarity (stdlib `difflib`) — works well on the prose-style SOPs here, untested on structured P&ID exports.

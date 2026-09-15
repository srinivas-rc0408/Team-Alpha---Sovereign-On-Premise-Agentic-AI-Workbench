# AEGIS — Air-Gapped Engineering Intelligence System

SIH26117 · Team Alpha · Sovereign On-Premise Agentic AI Workbench using
Open-Weight Multimodal LLMs for Confidential Industrial Work.

A local-only agent that answers operator questions ("is this pressure reading a
violation?") by planning a multi-step job, retrieving grounded SOP context,
running verification code in an isolated sandbox, and reflecting before it
answers — with every step written to a tamper-evident audit log. No query,
document, or model weight ever leaves the machine.

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
Plan → Vision (if image attached) → RAG → Calc → Reflect ──┬─→ Answer
                    ^                                       │
                    └───────────── retry (≤2x) ─────────────┘
```

Every node logs to `logs/audit.jsonl` (SHA-256 hash-chained — `audit.verify()`
finds the first broken link if any past entry is edited).

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

## Project layout

```
core/agent.py   — LangGraph state machine (plan/vision/rag/calc/reflect/answer)
core/rag.py     — hybrid FAISS + BM25 retrieval over docs/
core/tools.py   — vision (moondream), sandboxed calc, RAG search
core/audit.py   — SHA-256 hash-chained JSONL audit log
ui/app.py       — Streamlit demo, sign-off gate, audit/sandbox status
docs/           — SOPs to index (PDF/TXT/MD)
test_aegis.py   — end-to-end smoke test
```

## Known gaps vs. the full pitch deck

- No LanceDB, FastAPI/Redis, Next.js/React Flow, or gVisor — see the table above for what stands in for each and why.
- Vision returns a text description, not structured defect bounding boxes (needs Qwen2.5-VL; only moondream is installed here).
- Single-user, single-process — no RBAC/multi-tenant plant-DMZ deployment yet.

# AEGIS — Air-Gapped Engineering Intelligence System

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)
![SIH 2026](https://img.shields.io/badge/SIH-2026-orange)
![Air-Gapped](https://img.shields.io/badge/network-air--gapped-black)
![Status](https://img.shields.io/badge/status-prototype-yellow)

SIH26117 · Team Alpha · Sovereign On-Premise Agentic AI Workbench using
Open-Weight Multimodal LLMs for Confidential Industrial Work.

AEGIS is a local AI assistant for refinery operators: it answers questions
grounded in your own SOPs, verifies the arithmetic in a sandbox, judges safety
thresholds with deterministic code rather than model opinion, and gates
safety-critical findings behind a named human sign-off. Every model, document,
index, chat and audit record stays on the machine it runs on — nothing is sent
anywhere, ever.

---

## Hardware requirements

| | Minimum | Notes |
|---|---|---|
| GPU | 4GB VRAM (or none) | With less VRAM Ollama offloads layers to CPU — slower, still works |
| RAM | 16GB | 8GB runs but swaps heavily under `qwen2.5:7b` |
| Disk | ~8GB free | Model weights (~6.5GB) + index + history |
| OS | Linux (Arch/Ubuntu) or macOS | The installer detects and adapts |
| Python | 3.10+ | Installer creates its own venv |

Reference machine for the measured timings in ARCHITECTURE.md: i7-11800H,
RTX 3050 Ti (4GB), 16GB RAM, Arch Linux — roughly 20–60s per full agent run.

## ⚠️ The one honest note about internet

**First-time setup pulls ~6.5GB of models and needs internet ONCE. After that,
AEGIS runs 100% offline.** You can unplug the network permanently after
`./install.sh` finishes and every feature keeps working. `run.sh` refuses to
start if any configured endpoint points off-machine.

---

## Install

```bash
unzip aegis.zip && cd aegis
chmod +x install.sh run.sh
./install.sh      # one-time, pulls models, sets up everything
./run.sh          # starts the app — works offline from here on
```

Then open **http://127.0.0.1:8501**.

![AEGIS console](screenshots/aegis-ui.png)

### What each command actually does

| Command | In plain English |
|---|---|
| `chmod +x install.sh run.sh` | Marks the two scripts as runnable. Zip files don't preserve the executable bit, so this is needed once. |
| `./install.sh` | Installs Ollama if you don't have it, starts it, downloads the three AI models, creates an isolated Python environment, installs the Python libraries, creates the local storage folders, reads your documents in `docs/` into a searchable index, and runs the test suite to prove it all works. Safe to re-run — anything already done is skipped. |
| `./run.sh` | Starts everything up: activates the Python environment, makes sure Ollama is running, **verifies nothing can reach the internet**, builds the index if it's missing, and opens the console on `127.0.0.1:8501`. |

Prefer `make`? `make install`, `make run`, `make test`, `make index`,
`make clean`, `make reset-chats` do the same things (`make help` lists them).

---

## How to use it

1. **Add your documents.** Drop SOPs, P&IDs or manuals (`.pdf`, `.txt`, `.md`)
   into `docs/`, then click **Rebuild index from docs/** in the sidebar (or run
   `make index`). A sample SOP is included so you can try it immediately.
2. **Ask a question.** Type it, or click one of the three example buttons —
   e.g. *"Pressure reading is 18.4 bar. Safe limit is 15 bar per SOP-402. Is
   this a violation?"* Optionally attach a gauge or equipment photo.
3. **Watch the pipeline run.** Each of the steps — Plan, Vision, RAG, Calc,
   Safety Check, Reflect, Answer — flips from ⏳ to ✅ with its real elapsed time.
4. **Read the audited report.** You get the answer, the deterministic safety
   verdict, the retrieved SOP passages it used, the full reasoning trace, a
   network audit proving nothing left the machine, and the audit hash chain.
5. **Sign off if required.** Anything safety-critical is blocked behind a named
   supervisor sign-off, which is itself written to the audit log.
6. **Everything is saved.** Each run is appended to the current chat in
   `data/chats/`. Use the sidebar to search past chats, reopen one (it restores
   the full report), start a new one, or delete one.

There's also a **Document Comparison** tab: upload an old and a new revision of
an SOP and AEGIS diffs them, flags changed pressure/temperature/shutdown/SIL
values as HIGH or MEDIUM risk, and writes an MOC-style impact summary.

---

## Where your data is stored

Everything is a plain file under the project folder. Nothing leaves it.

| Path | Contents |
|---|---|
| `data/chats/` | Chat history — one JSON file per session, human-readable |
| `data/embeddings/` | The FAISS vector index + BM25 index built from `docs/` |
| `logs/audit.jsonl` | SHA-256 hash-chained audit trail of every step of every run |
| `docs/` | Your source SOPs (the only folder you put things into) |
| `temp/` | Scratch space |

`data/`, `logs/` and `temp/` are gitignored — operator questions and plant
readings never end up in version control. To wipe history: `make reset-chats`,
or just delete the JSON files.

---

## How to verify it's actually offline

AEGIS doesn't ask you to take its word for it.

**In the app:** the sidebar shows a live `🔒 OFFLINE MODE — 0 external
connections` badge, and every query's report includes a network audit of the
sockets AEGIS's own process tree opened.

**On startup:** `run.sh` runs four checks and refuses to launch if any fail —
endpoints resolve to loopback, telemetry switches are off, no external sockets
are open, and every network audit ever recorded was clean.

**Prove it yourself** — pull the network cable (or `nmcli networking off`),
then:

```bash
ping -c1 8.8.8.8        # fails: no route to host
./run.sh                # starts anyway
```

Ask a question. It answers normally. Run the check directly for the receipts:

```bash
./venv/bin/python -m core.offline_check
# offline_check ok — offline=True, external=[], network_audits_reviewed=N
```

Telemetry is force-disabled in `core/__init__.py` at import time — before
langchain or langsmith can read those variables — so an edited `.env` or an
inherited shell export can't switch phone-home behaviour back on.
`.streamlit/config.toml` disables Streamlit usage stats and binds the server to
`127.0.0.1`, never `0.0.0.0`, so the console isn't reachable from the plant LAN.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot reach Ollama at http://localhost:11434` | Start it: `ollama serve` (or `sudo systemctl start ollama`). `run.sh` tries this for you; check `logs/ollama.log`. |
| `Permission denied: ./install.sh` | `chmod +x install.sh run.sh` — the zip didn't preserve the executable bit. |
| Model download fails or stalls | Re-run `./install.sh`. It skips models already downloaded, so it resumes rather than restarting the 6.5GB. |
| `port 8501 is already in use` | Another copy is running. Stop it, or use a different port: `AEGIS_PORT=8502 ./run.sh`. |
| `no virtual environment found` | You ran `./run.sh` before `./install.sh`. Run the installer first. |
| `could not create the venv` (Ubuntu) | `sudo apt install python3-venv`, then re-run `./install.sh`. |
| `offline verification failed — refusing to start` | `OLLAMA_HOST` in `.env` points off-machine. Set it back to `http://localhost:11434`. |
| Answers are slow (20–60s) | Expected on a 4GB GPU — `qwen2.5:7b` partly runs on CPU. See ARCHITECTURE.md. |
| `No relevant documents found` | The index is empty. Put files in `docs/` and run `make index`. |
| Docker warning at startup | Harmless. The calculator falls back to a restricted in-process evaluator. Start Docker for full sandbox isolation. |

---

## Features

- ✅ **One-command install** — `./install.sh` handles Ollama, models, venv, deps, folders, index and verification; idempotent and re-runnable
- ✅ **LangGraph 6-node agent loop** (Plan → Vision → RAG → Calc → Safety Check → Reflect → Answer) with a bounded reflect→RAG retry
- ✅ **Hybrid FAISS + BM25 retrieval** with reciprocal rank fusion over your local SOPs
- ✅ **Local vision** on gauge/equipment photos via `moondream`
- ✅ **Sandboxed calculator** — Docker `network=none`, capabilities dropped, with an AST-whitelist fallback
- ✅ **Deterministic safety checks** (no LLM in the decision path) for pressure, temperature, vibration and wall thickness
- ✅ **Three independent gates** on human sign-off (reflect judgment, keyword backstop, rule-engine verdict)
- ✅ **SHA-256 hash-chained audit log** — tamper-evident, `verify()` finds the first broken link
- ✅ **Per-query network audit** proving zero external connections
- ✅ **Local chat history** — searchable, reloadable, one JSON file per session in `data/chats/`
- ✅ **Document revision diffing** with risk-rated safety-critical change flagging and MOC impact summary
- ✅ **Offline enforcement** — four startup checks, forced telemetry kill, loopback-only binding

## Architecture

```
Plan → Vision (if image attached) → RAG → Calc → Safety Check → Reflect ──┬─→ Answer
                    ^                                                     │
                    └───────────────────── retry (≤2x) ────────────────────┘
```

Every node logs to `logs/audit.jsonl` (SHA-256 hash-chained — `audit.verify()`
finds the first broken link if any past entry is edited). Every run also
performs a network audit over its own process tree. See **ARCHITECTURE.md** for
the full technical detail.

**Safety check is deterministic, not an LLM judgment.** The planner LLM only
extracts which check applies and the numbers involved (grounded in the query
and retrieved SOP text — the prompt explicitly forbids inventing a threshold);
`core/safety_rules.py`'s plain comparisons decide the verdict. A hallucinated
extraction can point at the wrong check, but it can never talk its way past a
CRITICAL/EXCEEDS/BELOW_MINIMUM verdict.

## Project layout

```
install.sh               — one-time setup (the only step needing internet)
run.sh                   — start the app (fully offline, verifies it before launching)
Makefile                 — install / run / test / index / clean / reset-chats
.streamlit/config.toml   — telemetry off, loopback-only binding, dark theme
core/agent.py            — LangGraph state machine (plan/vision/rag/calc/safety_check/reflect/answer)
core/rag.py              — hybrid FAISS + BM25 retrieval over docs/
core/tools.py            — vision (moondream), sandboxed calc, RAG search
core/audit.py            — SHA-256 hash-chained JSONL audit log
core/safety_rules.py     — deterministic pressure/temperature/vibration/wall-thickness checks
core/doc_diff.py         — SOP/P&ID revision diffing + risk-rated safety flagging + MOC summary
core/history.py          — local chat persistence (data/chats/)
core/offline_check.py    — four-way offline verification (endpoints/telemetry/live/history)
core/network_monitor.py  — per-query air-gap audit (external connections, byte counts)
ui/app.py                — Streamlit console: history sidebar, live pipeline, sign-off, doc compare
config/                  — safety_limits.example.json: template for your own verified thresholds
docs/                    — SOPs to index (PDF/TXT/MD)
test_aegis.py            — end-to-end smoke test
```

## Environment variables

Copy `.env.example` to `.env` to override any of these. All have working defaults.

| Var | Default | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | local Ollama server — must stay loopback or `run.sh` refuses to start |
| `LLM_MODEL` | `qwen2.5:7b` | reasoning model |
| `VISION_MODEL` | `moondream` | image description model |
| `EMBED_MODEL` | `nomic-embed-text` | embedding model for RAG |
| `DOCS_DIR` | `docs` | SOPs to index |
| `INDEX_DIR` | `data/embeddings` | FAISS + BM25 index output |
| `CHATS_DIR` | `data/chats` | local chat history |
| `RAG_TOP_K` | `4` | retrieved chunks per query |
| `AUDIT_LOG` | `logs/audit.jsonl` | hash-chained audit log path |
| `SANDBOX_IMAGE` | `python:3.12-slim` | Docker image for the sandboxed calc tool |
| `SANDBOX_TIMEOUT` | `10` | seconds before the sandbox run is killed |
| `SAFETY_LIMITS_FILE` | `config/safety_limits.json` | operator-supplied reference limits — copy from `config/safety_limits.example.json` and fill in numbers verified from your own SOPs; never pre-populated by AEGIS |
| `AEGIS_PORT` | `8501` | port for `./run.sh` |

Telemetry switches (`LANGCHAIN_TRACING_V2`, `LANGSMITH_TRACING`,
`ANONYMIZED_TELEMETRY`, `SCARF_NO_ANALYTICS`, `DO_NOT_TRACK`,
`STREAMLIT_BROWSER_GATHER_USAGE_STATS`) are **force-set to disabled** by
`core/__init__.py` at import time and cannot be re-enabled via `.env`.

## Honest architecture table — pitch deck vs. this prototype

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

## Known gaps

- No LanceDB, FastAPI/Redis, Next.js/React Flow, or gVisor — see the table above for what stands in for each and why.
- Vision returns a text description, not structured defect bounding boxes (needs Qwen2.5-VL; only moondream is installed here).
- Single-user, single-process — no RBAC/multi-tenant plant-DMZ deployment yet.
- `safety_rules.py`'s vibration/wall-thickness checkers are generic threshold evaluators, not preloaded with ISO 10816-3 (or any other) table values — limits must come from the query, retrieved SOP text, or your own `config/safety_limits.json`, so the system never asserts an unverified regulatory number as fact.
- `core/doc_diff.py` splits documents into sections on blank lines and pairs revisions by text similarity (stdlib `difflib`); its safety-critical flagging is a keyword heuristic that deliberately over-flags. Works well on prose-style SOPs, untested on structured P&ID exports.
- The audit log is tamper-*evident*, not tamper-proof: hash-chaining catches edits to entries that remain, but can't prove entries were never truncated and re-chained. That needs an external anchor.

## License

MIT — see [LICENSE](LICENSE).

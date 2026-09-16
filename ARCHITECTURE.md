# Architecture

Deeper technical detail behind the README's summary table. See `README.md`
first for the honest deck-vs-prototype mapping — this doc explains *how* the
prototype side of that table actually works.

## Data flow — where every byte goes

Everything inside the dashed box is on the operator's machine. Nothing crosses
it at runtime; the single arrow that ever leaves is the one-time model pull
during install, drawn separately because it is the only exception.

```
   ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  THIS MACHINE  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
                                                                               
   │   Operator                                                             │
        │  query + optional photo                                            
   │    ▼                                                                   │
     ┌─────────────────────┐   HTTP 127.0.0.1:8501 (loopback, CORS+XSRF on)   
   │ │  ui/app.py          │                                                 │
     │  Streamlit console  │                                                  
   │ └──────────┬──────────┘                                                 │
                │                                                             
   │            ▼                                                           │
     ┌───────────────────────────────────────────────┐                        
   │ │  core/agent.py — LangGraph state machine      │                       │
     │                                               │                        
   │ │  Plan → Vision → RAG → Calc → Safety → Reflect│──┐                    │
     │    │       │      │      │       │        │   │  │ retry ≤2x           
   │ └────┼───────┼──────┼──────┼───────┼────────┼───┘  │ (back to RAG)      │
          │       │      │      │       │        └──────┘                     
   │      │       │      │      │       │                                    │
          │       │      │      │       └─► core/safety_rules.py             
   │      │       │      │      │            (pure Python, no LLM)           │
          │       │      │      └─► core/tools.py ─► Docker --network=none    
   │      │       │      │                           (or AST-whitelist eval) │
          │       │      └─► core/rag.py ─► FAISS + BM25 ◄── data/embeddings/ 
   │      │       │                                                         │
          │       └─► qwen2.5-VL ─┐                                          
   │      └─► qwen2.5:7b ─────────┼─► Ollama daemon @ 127.0.0.1:11434       │
                nomic-embed-text ─┘        (local model weights on disk)      
   │                                                                        │
        every node ──► core/audit.py ──► logs/audit.jsonl (SHA-256 chained)  
   │    every run  ──► core/history.py ─► data/chats/*.json (atomic writes)  │
        every run  ──► core/network_monitor.py ─► was anything external?      
   │                   core/offline_check.py ──► refuses to start if so      │
                                                                              
   └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘

   ONE-TIME ONLY, during install.sh / install.ps1:
       ollama pull  ────────────────────────────────►  ollama.com  (~8GB)
   After that the box above is closed. No runtime path crosses it.
```

## The ReAct agent loop

```
                    ┌─────────────────────────────────────────┐
                    │                                          │
                    ▼                                          │
Plan → Vision (skip if no image) → RAG → Calc → Safety Check → Reflect ─┬─→ Answer → END
                                                                          │
                                                        insufficient? retry (≤2x)
```

`core/agent.py` builds this as a `langgraph.StateGraph`. Each node is a plain
function `(state) -> dict` that returns only the keys it changes; LangGraph
merges the returned dict into the shared `AgentState`. Nodes that the planner
marks unnecessary (no image attached → Vision, `needs_rag: false` → RAG)
return `{}` and no-op — the edge still fires, the node just does nothing.

Reflect is the only node with a conditional edge: if it judges the gathered
evidence insufficient, the graph routes to `bump_loop` (increments a counter,
logs why) and back to RAG for another retrieval pass, seeded with reflect's
own note about what was missing. `MAX_LOOPS = 2` bounds this — the graph
cannot run away regardless of what reflect decides, because the conditional
edge checks the loop counter, not just reflect's opinion.

Why plan decides per-query rather than always running every node: an image-
free text query doing an unnecessary vision call would burn ~10-20s on this
hardware for nothing; a query that already states its own limit doesn't need
a RAG pass. The cost is that a wrong plan skips something it should have
used — mitigated by reflect's retry loop, which re-enters RAG (not vision or
calc) if the answer looks unsupported.

## Why hybrid RAG (FAISS + BM25) instead of pure vector search

Dense embeddings (`nomic-embed-text` via Ollama) cluster on semantic meaning,
which is exactly what makes them miss exact identifiers: "SOP-MNT-402",
"18 bar(g)", or a specific standard clause number don't have neighbors in
embedding space the way a paraphrased sentence does. BM25 (`rank_bm25`) is a
term-frequency ranker — it excels at exactly this kind of exact-match lookup
and is bad at paraphrase.

`core/rag.py` runs both ranked lists per query and merges them with
reciprocal rank fusion (`_rrf_merge`): `score = Σ 1/(60 + rank)` across
whichever ranking(s) a chunk appears in. A chunk that's top-5 in both gets a
much higher combined score than one that's top-1 in only one — the fusion
rewards agreement without needing to calibrate the two score scales against
each other (BM25 and cosine similarity aren't on the same scale; fusing on
*rank* sidesteps that entirely).

Both indexes are built together in `build_index()` and persisted to
`INDEX_DIR` (`data/embeddings/`): the FAISS index via `vs.save_local()`, and
the BM25 index + original chunks pickled alongside it (`bm25.pkl`) so ranking
can reconstruct source metadata without re-embedding on load.

## Docker sandbox isolation

`core/tools.py:_run_in_docker()` runs the calculator's expression as a
one-line Python script inside a container launched with:

```
docker run --rm \
  --network=none --read-only --tmpfs /tmp \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --memory=128m --pids-limit=64 --cpus=0.5 \
  -v <script>:/sandbox/run.py:ro \
  python:3.12-slim python /sandbox/run.py
```

What each flag actually buys:
- `--network=none` — no interface but loopback inside the container; a
  malicious or hallucinated expression cannot exfiltrate anything even if it
  somehow gained arbitrary code execution.
- `--read-only` + `--tmpfs /tmp` — the container's filesystem can't be
  written to except a throwaway tmpfs, so nothing persists past the run.
- `--cap-drop=ALL` + `--security-opt=no-new-privileges` — no Linux
  capabilities beyond the unprivileged default, and no privilege escalation
  via setuid binaries.
- `--memory=128m --pids-limit=64 --cpus=0.5` — bounds resource exhaustion
  (fork bombs, memory-hog expressions) from affecting the host.
- The script itself is mounted read-only (`:ro`), so the container can't
  even modify the file it's executing.

If the Docker daemon isn't reachable (`docker info` fails), `calculate()`
falls back to `core/tools.py:_safe_eval()` — an AST-walking evaluator that
only permits arithmetic, comparisons, and a whitelisted set of `math`
functions to reach Python's evaluation machinery. This exists because the
naive alternative, `eval(expr, {"__builtins__": {}}, allowed_ns)`, is a known
sandbox-escape pattern: attribute access (`().__class__.__bases__[0]
.__subclasses__()`) needs no builtins to reach arbitrary classes, so removing
`__builtins__` alone doesn't close it. The AST walker has no `Attribute` or
`Subscript` node in its allowed set, so that escape path doesn't exist
syntactically, not just by policy. `core/tools.py`'s `__main__` self-check
exercises exactly this escape and asserts it's blocked.

Docker gives real container isolation on top of that; gVisor (`runsc`) would
add syscall-level interception as a second layer, but isn't installed here —
see the README's gap table.

## SHA-256 hash-chained audit log

`core/audit.py:log()` appends one JSON line per event to `logs/audit.jsonl`.
Each entry's hash covers its own payload *and* the previous entry's hash:

```python
hash_n = SHA256(hash_{n-1} + json({ts, event, data, prev_hash: hash_{n-1}}))
```

This is the same construction as a blockchain's block-linking, minus the
distributed-consensus part (there's exactly one writer — this process — so
no distribution is needed). The property it gives: editing entry `k` changes
`hash_k`, which no longer matches what entry `k+1` recorded as its
`prev_hash`, which `verify()` catches immediately by recomputing the chain
from `GENESIS` and comparing at each step. It reports the exact index of the
first broken link, not just "tampered somewhere."

What this does *not* protect against: someone with write access truncating
the file and re-appending a fresh, internally-consistent chain from that
point forward — hash-chaining detects edits to entries that remain, it can't
prove entries were never removed. Real tamper-evidence at that level would
need an external anchor (e.g. periodically publishing the latest hash
somewhere the log's own writer can't rewrite) — not implemented here.

## The human sign-off gate

Reflect asks the LLM to judge `requires_human_approval` directly. That's a
single non-deterministic classifier call sitting on the safety-critical path,
which is a bad place for a probabilistic judgment to be a single point of
failure — the same input can produce different reflect outputs across runs.

So `requires_approval` is the OR of three independent signals, not reflect's
opinion alone:

1. **Reflect's own judgment** (`requires_human_approval` in its JSON reply).
2. **A keyword backstop** (`agent.py:_mentions_safety_risk`) over the answer
   text actually shown to the operator — catches cases where reflect said no
   but the final answer still describes a violation.
3. **The deterministic safety-rules verdict** (`safety_check_node`) — if
   `core/safety_rules.py` returns `CRITICAL`/`EXCEEDS`/`BELOW_MINIMUM` from a
   plain numeric comparison, sign-off is required regardless of what either
   LLM call decided. This is the strongest of the three because it's not a
   model output at all.

Why the safety-rules module never hardcodes an industry threshold (ISO
10816-3 zone boundaries, API 510 corrosion-rate limits, etc.): the planner
LLM extracts *which* check applies and *what numbers* are involved, grounded
in the query and retrieved SOP text — but if `safety_rules.py` also baked in
an unverified constant from training data, "deterministic" would just mean
"a hallucination that happens to be reproducible." The checker functions take
`safe_limit`/`critical_limit` as arguments; the actual regulatory numbers
come from the plant's own documents via RAG, not from the model's memory.

## Keeping the limit and the verdict out of the LLM's hands

Taking limits as arguments only helps if the argument is trustworthy, and the
planner LLM will invent a threshold even when the prompt forbids it (observed:
an 18.4 bar reading paired with a fabricated `safe_limit` of 20, flipping a
real CRITICAL to NORMAL). So when `config/safety_limits.json` has an
operator-verified number for the check type, `safety_check_node` overwrites
whatever the LLM extracted — config wins, and the LLM's value is used only
where config is silent.

That fixes the verdict but not what the operator reads. The answering LLM sees
the question too, so it kept narrating the *question's* limit alongside the
corrected verdict ("18.4 bar is below the safe limit of 20 bar" under a
CRITICAL status). Two things close that gap:

- `safety_check_node` returns `safety_input` — the limits the verdict was
  actually computed against, plus the config `source` citation — so the
  answering prompt carries the authoritative numbers, not just the status.
- The verdict line at the top of every safety answer is **built in code**, not
  written by the model. Asked to restate the machine fields itself, the model
  mangled them (copying the query's `safe_limit` of 10 next to a NORMAL verdict
  computed against the verified 15). A wrong number in the first line an
  operator reads is exactly the failure this gate exists to prevent, so the
  model is told to explain the reading and next steps and to leave the status,
  action and numbers alone.

One related prompt rule, learned the same way: these directives live in the
instruction block, never among the facts in `CONTEXT`. Placed among the facts,
the model echoed them back verbatim into the operator's answer.

## Hardware and measured performance

- **Machine:** i7-11800H, RTX 3050 Ti (4GB VRAM), 16GB RAM, Arch Linux.
- **qwen2.5:7b** exceeds the 4GB VRAM budget at full precision, so Ollama
  offloads part of the model to CPU. Measured throughput on this hardware:
  **~6-10 tokens/sec**. A full agent run (plan + reflect + answer, plus RAG/
  calc/safety_check when the plan calls for them, none of which need the LLM)
  takes roughly 20-60 seconds depending on how many of the optional nodes
  fire and how long the generated answer is.
- **nomic-embed-text** (embeddings) is small enough to run comfortably within
  the 4GB VRAM budget. **qwen2.5vl:3b** (vision) is larger and partly offloads
  to CPU on a 4GB GPU, so image queries add ~20-50s of vision time — the
  trade-off for a model that actually reads gauge displays and P&ID/drawing text
  (tag numbers, pressure ratings, revisions) rather than just describing the
  scene. On very low VRAM, set `VISION_MODEL=moondream` for a lighter,
  gist-only fallback.
- This is expected, not a bug: the pitch deck's vLLM/Qwen2.5-Coder-32B path
  assumes dedicated server-class GPU hardware. The measured numbers above are
  what a single consumer laptop GPU actually delivers, offered honestly
  rather than extrapolated from the deck's target hardware.

## Local chat history (`core/history.py`)

One JSON file per session under `data/chats/`, plus a derived `index.json` for
fast listing and search. Each turn records the query, the attached image (copied
into `data/chats/media/` so history stays self-contained after Streamlit's temp
file is gone), all six node outputs, the deterministic safety verdict, the
sign-off flag, the reasoning trace, per-step timings, the network audit, and the
slice of audit-chain hashes that this specific run produced.

Two properties matter more than the schema:

**Writes are atomic.** `_write_atomic()` writes to a temp file in the same
directory, `fsync`s it, then `os.replace()`s it into position — atomic on both
POSIX and Windows. A crash mid-write leaves the previous good copy intact rather
than a truncated file. The temp name carries a uuid *and* the pid: Streamlit
serves concurrent sessions as threads of one process, so a pid-only name lets
two simultaneous writers share a temp path and delete each other's file. That
was a real bug, found by a 60-thread stress test, not a hypothetical.

**The index is a cache, never the source of truth.** If `index.json` is missing,
truncated or stale, `list_sessions()` rebuilds it from the session files. The
cost of losing it is the speed of one listing, never the history itself. In-process
index updates are serialised with a lock; across processes the worst case is a
stale listing that the next rebuild repairs.

## Offline enforcement (`core/offline_check.py`)

Four independent checks, because each catches a failure the others cannot see:

1. **endpoints** — every configured service URL resolves to loopback. Catches a
   `.env` pointed at a remote Ollama, which would be a genuine leak. `0.0.0.0` is
   treated as local-but-warned: as a *destination* it reaches this host on Linux
   and fails outright on macOS/Windows, so `core/__init__.py` rewrites it to
   `127.0.0.1` at import rather than letting it break portability silently.
2. **telemetry** — the phone-home switches are still disabled at the moment of
   the check, not merely at startup.
3. **live** — external sockets open right now in this process tree, via
   `core/network_monitor.py`.
4. **history** — every `network_audit` entry ever written to the audit log was
   clean. This is what makes "nothing ever left" a claim about the whole recorded
   history rather than about this instant; a leak that happened an hour ago and
   has since closed is invisible to a live snapshot but permanent in the log.

`assert_offline()` raises `OfflineViolation` instead of returning a verdict, and
`run.sh` / `run.ps1` call it before opening the console. An air-gapped tool that
merely *reports* a leak has already leaked — the only useful behaviour is to
refuse to start.

## Error handling (`core/agent.py:friendly_error`)

A refinery technician reading a Python traceback learns nothing actionable, so
every recognised failure is translated into the exact command that fixes it:
a missing model becomes `ollama pull <the model name parsed from the error>`,
an unreachable daemon becomes `ollama serve`, a corrupt index becomes
`make index`, an empty knowledge base names the folder to fill. Unrecognised
exceptions still surface their real message rather than a generic apology, and
the UI keeps the full traceback one expander away for support.

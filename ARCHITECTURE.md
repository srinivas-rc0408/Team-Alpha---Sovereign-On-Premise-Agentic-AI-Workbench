# Architecture

Deeper technical detail behind the README's summary table. See `README.md`
first for the honest deck-vs-prototype mapping — this doc explains *how* the
prototype side of that table actually works.

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

## Hardware and measured performance

- **Machine:** i7-11800H, RTX 3050 Ti (4GB VRAM), 16GB RAM, Arch Linux.
- **qwen2.5:7b** exceeds the 4GB VRAM budget at full precision, so Ollama
  offloads part of the model to CPU. Measured throughput on this hardware:
  **~6-10 tokens/sec**. A full agent run (plan + reflect + answer, plus RAG/
  calc/safety_check when the plan calls for them, none of which need the LLM)
  takes roughly 20-60 seconds depending on how many of the optional nodes
  fire and how long the generated answer is.
- **moondream** (vision) and **nomic-embed-text** (embeddings) are both small
  enough to run comfortably within the 4GB VRAM budget.
- This is expected, not a bug: the pitch deck's vLLM/Qwen2.5-Coder-32B path
  assumes dedicated server-class GPU hardware. The measured numbers above are
  what a single consumer laptop GPU actually delivers, offered honestly
  rather than extrapolated from the deck's target hardware.

"""LangGraph agent: Plan → Vision → RAG → Calc → Reflect → [retry RAG | Answer].

Every node runs locally against Ollama and logs to the tamper-evident audit
trail. Nodes no-op (return {}) when the plan says they aren't needed. Reflect
can loop the graph back to RAG for another retrieval pass when evidence looks
thin, capped at MAX_LOOPS retries so the agent can never run away.
"""
import json
import os
import re
import time
from typing import Optional, TypedDict

import ollama
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph

from . import audit, network_monitor, safety_rules, tools


MAX_LOOPS = 2  # reflect may send the agent back for more evidence at most this many times

_SAFETY_KEYWORDS = (
    "safety violation", "critical limit", "critical/", "exceed", "isolat",
    "evacuat", "immediate", "trip", "alarm", "do not re-enter", "unsafe",
)


def _mentions_safety_risk(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _SAFETY_KEYWORDS)


class AgentState(TypedDict, total=False):
    query: str
    image_path: Optional[str]
    plan: dict
    vision: str
    context: str
    calc: str
    safety: dict
    reflection: dict
    answer: str
    trace: list
    loop_count: int
    requires_approval: bool


def _llm(fmt=None):
    return ChatOllama(
        model=os.getenv("LLM_MODEL", "qwen2.5:7b"),
        base_url=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        temperature=0,
        format=fmt,
    )


def plan_node(state):
    q = state["query"]
    has_image = bool(state.get("image_path"))
    prompt = (
        "You are the planner for an air-gapped oil-refinery assistant. Decide which "
        "capabilities the query needs. Reply as JSON with boolean keys "
        '"needs_vision", "needs_rag", "needs_calc", a string "calc_expression" '
        "(a pure Python/math expression to evaluate, else empty), and a "
        '"safety_check" object when the query gives a specific measured value to '
        'judge against a limit: {"type": one of "pressure"/"temperature"/'
        '"vibration"/"wall_thickness"/"none", "reading": number, "safe_limit": '
        'number, "critical_limit": number or null}. Only fill safe_limit/'
        "critical_limit with values the query or context actually states — never "
        'invent a threshold. Use type "none" if no such check applies.\n'
        f"An image is {'ATTACHED' if has_image else 'NOT attached'}.\n"
        f"Query: {q}"
    )
    try:
        plan = json.loads(_llm(fmt="json").invoke(prompt).content)
    except Exception:
        plan = {}
    plan["needs_vision"] = bool(plan.get("needs_vision", has_image)) and has_image
    plan.setdefault("needs_rag", True)
    plan.setdefault("needs_calc", False)
    plan.setdefault("calc_expression", "")
    audit.log("plan", {"query": q, "plan": plan})
    return {"plan": plan, "trace": state.get("trace", []) + [f"PLAN {plan}"]}


def vision_node(state):
    if not state["plan"].get("needs_vision"):
        return {}
    desc = tools.vision(state["image_path"])
    audit.log("vision", {"image": os.path.basename(state["image_path"]), "chars": len(desc)})
    return {"vision": desc, "trace": state["trace"] + [f"VISION {desc[:80]}…"]}


def rag_node(state):
    if not state["plan"].get("needs_rag"):
        return {}
    # Fold the image description into the query so retrieval reflects what's seen.
    q = state["query"] + (("\n" + state["vision"]) if state.get("vision") else "")
    # On a retry loop, steer retrieval with reflect's own note about what was missing.
    if state.get("loop_count", 0) > 0 and state.get("reflection", {}).get("note"):
        q += "\n" + state["reflection"]["note"]
    ctx = tools.rag_search(q)
    audit.log("rag", {"chars": len(ctx), "loop_count": state.get("loop_count", 0)})
    return {"context": ctx, "trace": state["trace"] + [f"RAG {len(ctx)} chars"]}


def calc_node(state):
    plan = state["plan"]
    expr = (plan.get("calc_expression") or "").strip()
    if not plan.get("needs_calc") or not expr:
        return {}
    result = tools.calculate(expr)
    audit.log("calc", {"expression": expr, "result": result})
    return {"calc": f"{expr} = {result}", "trace": state["trace"] + [f"CALC {expr} = {result}"]}


def safety_check_node(state):
    """Deterministic judgment, no LLM in the verdict: plan_node's LLM extracted
    which check applies and the reading; SafetyChecker's plain comparisons decide.

    Operator-verified limits in config/safety_limits.json are AUTHORITATIVE and
    override the LLM's extracted limit. This matters because the planner LLM will
    hallucinate a threshold even when the prompt forbids it (observed: an 18.4 bar
    reading paired with a fabricated safe_limit of 20, silently flipping a real
    CRITICAL violation to NORMAL). A safety gate whose limit can be moved by a
    hallucination is not deterministic — so when the plant has stated a verified
    number for this check type, that number wins, and the LLM's is used only when
    config has none for that type."""
    sc = state["plan"].get("safety_check") or {}
    check_type = sc.get("type", "none")
    if check_type == "none" or sc.get("reading") is None:
        return {}
    used_reference = False
    ref = safety_rules.load_reference_limits().get(check_type, {})
    if ref.get("safe_limit") is not None:
        # Config is authoritative: overwrite whatever the LLM extracted. Keep the
        # LLM's critical_limit only if config doesn't specify one.
        sc = {**sc, "safe_limit": ref["safe_limit"],
              "critical_limit": ref.get("critical_limit", sc.get("critical_limit"))}
        used_reference = True
    if sc.get("safe_limit") is None:
        return {}
    checker = safety_rules.SafetyChecker()
    try:
        if check_type == "pressure":
            verdict = checker.check_pressure(sc["reading"], sc["safe_limit"], sc.get("critical_limit"))
        elif check_type == "temperature":
            verdict = checker.check_temperature(sc["reading"], sc["safe_limit"], sc.get("critical_limit"))
        elif check_type == "vibration":
            verdict = checker.check_vibration(sc["reading"], sc["safe_limit"])
        elif check_type == "wall_thickness":
            verdict = checker.check_wall_thickness(sc["reading"], sc["safe_limit"])
        else:
            return {}
    except (KeyError, TypeError, ZeroDivisionError):
        return {}
    audit.log("safety_check", {"type": check_type, "input": sc, "verdict": verdict,
                                "used_reference_config": used_reference})
    ref_note = " (limit from config/safety_limits.json)" if used_reference else ""
    return {
        "safety": verdict,
        "trace": state["trace"] + [f"SAFETY_CHECK {check_type}: {verdict['status']}{ref_note}"],
    }


def reflect_node(state):
    have = [k for k in ("vision", "context", "calc") if state.get(k)]
    prompt = (
        "Assess whether the gathered evidence is sufficient to answer the query safely, "
        "and whether the finding is safety-critical enough to need a human supervisor's "
        'sign-off before acting on it. Reply as JSON: {"sufficient": bool, "note": '
        '"one short sentence", "requires_human_approval": bool}. Set requires_human_approval '
        "true for anything indicating a limit/threshold breach, equipment failure, or other "
        f"safety violation.\nQuery: {state['query']}\nEvidence present: {have or 'none'}"
    )
    try:
        r = json.loads(_llm(fmt="json").invoke(prompt).content)
    except Exception:
        r = {"sufficient": bool(have), "note": "", "requires_human_approval": False}
    r.setdefault("requires_human_approval", False)
    audit.log("reflect", r)
    return {
        "reflection": r,
        "requires_approval": bool(r["requires_human_approval"]),
        "trace": state["trace"] + [f"REFLECT {r}"],
    }


def _after_reflect(state):
    """Loop back to RAG for another retrieval pass if reflect found the evidence
    insufficient, capped at MAX_LOOPS so the graph can never run away."""
    insufficient = not state.get("reflection", {}).get("sufficient", True)
    loop_count = state.get("loop_count", 0)
    if insufficient and loop_count < MAX_LOOPS:
        return "retry"
    return "answer"


def bump_loop_node(state):
    n = state.get("loop_count", 0) + 1
    audit.log("loop", {"loop_count": n, "reason": state.get("reflection", {}).get("note", "")})
    return {"loop_count": n, "trace": state["trace"] + [f"LOOP retry #{n}"]}


def answer_node(state):
    parts = []
    if state.get("vision"):
        parts.append(f"IMAGE ANALYSIS:\n{state['vision']}")
    if state.get("context"):
        parts.append(f"KNOWLEDGE BASE (SOPs):\n{state['context']}")
    if state.get("calc"):
        parts.append(f"CALCULATION:\n{state['calc']}")
    if state.get("safety"):
        s = state["safety"]
        parts.append(
            f"DETERMINISTIC SAFETY CHECK (not an LLM judgment): status={s['status']}, "
            f"action={s['action']}"
        )
    grounding = "\n\n".join(parts) if parts else "No supporting context was retrieved."
    reflection = state.get("reflection", {})
    caution = "" if reflection.get("sufficient", True) else (
        f"\n\nNOTE: evidence may be insufficient — {reflection.get('note', '')}"
    )
    prompt = (
        "You are Aegis, an air-gapped AI assistant for oil-refinery operators. Answer using "
        "ONLY the context below. If it is insufficient, say so plainly and tell the operator "
        "to consult a supervisor or the relevant SOP — never guess on safety. Cite SOP sources "
        f"in [brackets]. Be precise and concise.\n\nCONTEXT:\n{grounding}{caution}\n\n"
        f"QUESTION: {state['query']}\n\nANSWER:"
    )
    ans = _llm().invoke(prompt).content.strip()

    # Three independent gates, any one of which can force sign-off: reflect's LLM
    # judgment, a keyword check over the answer actually shown to the operator, and
    # (strongest, since it's pure code) the safety_rules verdict. A hallucination or
    # a flip in either LLM call can't silently skip sign-off past the rule engine.
    safety_status = state.get("safety", {}).get("status")
    requires_approval = (
        state.get("requires_approval", False)
        or _mentions_safety_risk(ans)
        or safety_status in ("CRITICAL", "EXCEEDS", "BELOW_MINIMUM")
    )

    audit.log("answer", {"chars": len(ans), "requires_approval": requires_approval})
    return {
        "answer": ans,
        "requires_approval": requires_approval,
        "trace": state["trace"] + ["ANSWER"],
    }


def build_agent():
    g = StateGraph(AgentState)
    for name, fn in [
        ("plan", plan_node),
        ("vision", vision_node),
        ("rag", rag_node),
        ("calc", calc_node),
        ("safety_check", safety_check_node),
        ("reflect", reflect_node),
        ("bump_loop", bump_loop_node),
        ("answer", answer_node),
    ]:
        g.add_node(name, fn)
    g.set_entry_point("plan")
    for a, b in [
        ("plan", "vision"), ("vision", "rag"), ("rag", "calc"),
        ("calc", "safety_check"), ("safety_check", "reflect"),
    ]:
        g.add_edge(a, b)
    g.add_conditional_edges("reflect", _after_reflect, {"retry": "bump_loop", "answer": "answer"})
    g.add_edge("bump_loop", "rag")
    g.add_edge("answer", END)
    return g.compile()


_AGENT = {"g": None}


def _check_ollama():
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    try:
        ollama.Client(host=host).list()
    except Exception as e:
        raise RuntimeError(
            f"Cannot reach Ollama at {host}. Is it running? Start it with: ollama serve"
        ) from e


_MODEL_VARS = ("LLM_MODEL", "VISION_MODEL", "EMBED_MODEL")


def friendly_error(exc: Exception) -> str:
    """Turn an exception into something an operator can act on.

    A refinery technician reading a Python traceback learns nothing they can use;
    every branch here ends in the exact command that fixes the problem. Anything
    unrecognised still reports its real message rather than a generic apology.
    """
    text = str(exc)
    low = text.lower()

    if "not found" in low and "model" in low:
        # Recover the offending model name so the fix is copy-pasteable.
        match = re.search(r"model ['\"]?([\w.:\-/]+)['\"]? not found", text)
        model = match.group(1) if match else next(
            (os.getenv(v) for v in _MODEL_VARS if os.getenv(v) and os.getenv(v) in text), None
        )
        if model:
            return (f"The model '{model}' is not installed in Ollama.\n\n"
                    f"Fix — run this, then try again:\n\n    ollama pull {model}")
        return ("A required model is not installed in Ollama.\n\n"
                "Fix — run:\n\n    ollama pull qwen2.5:7b\n"
                "    ollama pull qwen2.5vl:3b\n    ollama pull nomic-embed-text")

    if any(s in low for s in ("connection refused", "cannot reach ollama", "failed to connect",
                              "connection error", "max retries", "connectionerror")):
        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        return (f"Cannot reach Ollama at {host}.\n\n"
                "Fix — start it, then try again:\n\n    ollama serve")

    if "unreadable or corrupted" in low or "index" in low and "corrupt" in low:
        return (f"{text}\n\n"
                "Fix — rebuild the index from the sidebar, or run:\n\n    make index")

    if "no relevant documents" in low or "no .pdf/.txt/.md documents" in low:
        docs = os.getenv("DOCS_DIR", "docs")
        return (f"The knowledge base is empty — no documents found in {docs}/.\n\n"
                f"Fix — copy your SOPs (PDF/TXT/MD) into {docs}/, then rebuild the index "
                "from the sidebar (or run: make index)")

    if isinstance(exc, MemoryError) or "out of memory" in low or "cuda" in low:
        return ("The machine ran out of memory loading the model.\n\n"
                "Fix — close other applications, or switch to a smaller model by setting "
                "LLM_MODEL in .env (e.g. LLM_MODEL=qwen2.5:3b) and running: ollama pull qwen2.5:3b")

    if isinstance(exc, (PermissionError, OSError)) and any(
        s in low for s in ("permission denied", "read-only")
    ):
        return (f"AEGIS could not write to disk: {text}\n\n"
                "Fix — check you own the project folder and it is not read-only.")

    return f"{type(exc).__name__}: {text}"


def run_streaming(query: str, image_path: str = None):
    """Yields (node_name, elapsed_seconds, state_so_far) as each node actually
    completes, via LangGraph's .stream() — real incremental progress, not an
    animated replay of an already-finished run. The final yield uses node_name
    "__final__" and carries the complete state, network audit included."""
    _check_ollama()
    if _AGENT["g"] is None:
        _AGENT["g"] = build_agent()
    audit.log("query", {"query": query, "image": os.path.basename(image_path) if image_path else None})

    io_before = network_monitor.snapshot_io()
    t0 = time.time()
    full = {"query": query, "image_path": image_path, "trace": []}
    for step in _AGENT["g"].stream(full):
        node_name, output = next(iter(step.items()))
        full.update(output or {})  # LangGraph's stream reports a node's {} return as None
        yield node_name, round(time.time() - t0, 2), dict(full)

    net_report = network_monitor.report(io_before)
    audit.log("network_audit", net_report)
    full["network_audit"] = net_report
    full["trace"].append(
        f"NETWORK_AUDIT {'clean' if net_report['clean'] else 'EXTERNAL CONNECTIONS DETECTED'}"
    )
    yield "__final__", round(time.time() - t0, 2), full


def run(query: str, image_path: str = None) -> dict:
    final = None
    for _, _, state in run_streaming(query, image_path):
        final = state
    return final


run_aegis = run


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print('Usage: python -m core.agent "your query here"')
        sys.exit(1)
    try:
        result = run(" ".join(sys.argv[1:]))
    except Exception as e:
        print(f"\nAEGIS could not complete that query.\n\n{friendly_error(e)}\n")
        sys.exit(1)
    print(result["answer"])

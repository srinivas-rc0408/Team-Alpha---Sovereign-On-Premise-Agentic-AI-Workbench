"""LangGraph agent: Plan → Vision → RAG → Calc → Reflect → [retry RAG | Answer].

Every node runs locally against Ollama and logs to the tamper-evident audit
trail. Nodes no-op (return {}) when the plan says they aren't needed. Reflect
can loop the graph back to RAG for another retrieval pass when evidence looks
thin, capped at MAX_LOOPS retries so the agent can never run away.
"""
import json
import os
from typing import Optional, TypedDict

import ollama
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph

from . import audit, tools


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
        '"needs_vision", "needs_rag", "needs_calc", and a string "calc_expression" '
        "(a pure Python/math expression to evaluate, else empty).\n"
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

    # The reflect LLM's own approval judgment is a non-deterministic single
    # classifier — not trustworthy as the sole gate on a safety-critical sign-off.
    # Back it with a deterministic keyword check over the answer actually shown
    # to the operator, so a flip in the LLM's call can't silently skip sign-off.
    requires_approval = state.get("requires_approval", False) or _mentions_safety_risk(ans)

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
        ("reflect", reflect_node),
        ("bump_loop", bump_loop_node),
        ("answer", answer_node),
    ]:
        g.add_node(name, fn)
    g.set_entry_point("plan")
    for a, b in [("plan", "vision"), ("vision", "rag"), ("rag", "calc"), ("calc", "reflect")]:
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


def run(query: str, image_path: str = None) -> dict:
    _check_ollama()
    if _AGENT["g"] is None:
        _AGENT["g"] = build_agent()
    audit.log("query", {"query": query, "image": os.path.basename(image_path) if image_path else None})
    return _AGENT["g"].invoke({"query": query, "image_path": image_path, "trace": []})


run_aegis = run


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print('Usage: python -m core.agent "your query here"')
        sys.exit(1)
    try:
        result = run(" ".join(sys.argv[1:]))
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(result["answer"])

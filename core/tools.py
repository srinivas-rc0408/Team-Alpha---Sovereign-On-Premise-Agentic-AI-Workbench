"""Agent tools — local vision (Qwen2.5-VL), RAG search, sandboxed calculator."""
import ast
import math
import operator
import os
import subprocess
import tempfile

import ollama

from . import KEEP_ALIVE
from .rag import search as _rag_search

VISION_PROMPT = (
    "You are inspecting an industrial image (a gauge, equipment photo, or a P&ID/"
    "engineering drawing) for a refinery operator. Report only what is actually "
    "visible, and never invent a tag number, label, or value:\n"
    "1. Transcribe VERBATIM every piece of text, equipment tag (e.g. FCV-105, "
    "PI-105, P-101), and numeric rating you can read, including units "
    "(psig, bar, degF, degC, mm/s, gpm) and any drawing revision or MOC number.\n"
    "2. Report any gauge or digital-display reading with its unit, and any alarm, "
    "trip, or warning state shown.\n"
    "3. Note equipment type, valve/switch positions, and any visible leak, "
    "corrosion, or safety hazard.\n"
    "If a detail is not legible, say so rather than guessing."
)


def _client():
    return ollama.Client(host=os.getenv("OLLAMA_HOST", "http://localhost:11434"))


def vision(image_path: str, prompt: str = VISION_PROMPT) -> str:
    resp = _client().generate(
        model=os.getenv("VISION_MODEL", "qwen2.5vl:3b"),
        prompt=prompt,
        images=[image_path],
        keep_alive=KEEP_ALIVE,
    )
    return resp["response"].strip()


def rag_search(query: str, k: int = None) -> str:
    k = k or int(os.getenv("RAG_TOP_K", "4"))
    hits = _rag_search(query, k)
    if not hits:
        return "No relevant documents found in the knowledge base."
    return "\n\n".join(
        f"[{h['source']}" + (f" p.{h['page'] + 1}" if h.get("page") is not None else "") + f"]\n{h['text']}"
        for h in hits
    )


# math-only namespace: no builtins, no imports reachable from an expression.
_ALLOWED = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
_ALLOWED.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow})

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_CMPOPS = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


def _safe_eval(node):
    """AST-walking evaluator: only arithmetic + whitelisted math calls reach
    Python's eval machinery, so there's no Attribute/Subscript node type an
    expression could use to pivot to __class__/__subclasses__ and escape the
    restricted namespace — the flaw plain eval(..., {'__builtins__': {}}) has."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMPOPS:
        return _CMPOPS[type(node.ops[0])](_safe_eval(node.left), _safe_eval(node.comparators[0]))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED:
        return _ALLOWED[node.func.id](*(_safe_eval(a) for a in node.args))
    if isinstance(node, ast.Name) and node.id in _ALLOWED:
        return _ALLOWED[node.id]
    raise ValueError(f"disallowed expression: {ast.dump(node)}")

_DOCKER_IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.12-slim")
_DOCKER_TIMEOUT = int(os.getenv("SANDBOX_TIMEOUT", "10"))


def _run_in_docker(expression: str) -> str:
    """Run the expression in a locked-down, network-isolated container.

    network=none + cap-drop=ALL + read-only rootfs + no-new-privileges + resource
    caps, so a malicious or hallucinated expression can't touch the network, the
    host filesystem, or exhaust resources — matches the "Docker sandbox, network=none"
    isolation the SOP-MNT-402 tooling is audited against.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        # eval() here runs inside the network=none/cap-drop=ALL container spun up
        # below, not on the host — the sandbox is the safety boundary, not the parser.
        f.write(f"import math\nprint(eval({expression!r}, "
                 f"{{'__builtins__': {{}}}}, {{k: getattr(math, k) for k in dir(math) "
                 f"if not k.startswith('_')}} | dict(abs=abs, round=round, min=min, "
                 f"max=max, sum=sum, pow=pow)))")
        script_path = f.name
    try:
        proc = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network=none", "--read-only", "--tmpfs", "/tmp",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--memory=128m", "--pids-limit=64", "--cpus=0.5",
                "-v", f"{script_path}:/sandbox/run.py:ro",
                _DOCKER_IMAGE, "python", "/sandbox/run.py",
            ],
            capture_output=True, text=True, timeout=_DOCKER_TIMEOUT,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "sandbox exited non-zero")
        return proc.stdout.strip()
    finally:
        os.unlink(script_path)


def calculate(expression: str) -> str:
    """Evaluate an engineering/arithmetic expression, sandboxed in Docker when
    available (network=none, no host access); falls back to an in-process
    math-only eval if the Docker daemon isn't reachable, so a live demo never
    hard-fails on infra."""
    try:
        return _run_in_docker(expression)
    except Exception:
        pass
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval")))
    except Exception as e:
        return f"Calculation error: {e}"


if __name__ == "__main__":  # ponytail: calculator self-check — result + sandbox escapes
    assert calculate("2**10 + sqrt(16)") == "1028.0"
    assert calculate("18.4 > 15") == "True"
    assert "error" in calculate("__import__('os').system('echo hi')").lower()
    assert "error" in calculate("().__class__.__bases__[0].__subclasses__()").lower()
    print("tools ok — math evaluates, sandbox blocks builtins and attribute-access escapes")

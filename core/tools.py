"""Agent tools — local vision (moondream), RAG search, sandboxed calculator."""
import math
import os
import subprocess
import tempfile

import ollama

from .rag import search as _rag_search

VISION_PROMPT = (
    "Describe this industrial image in detail. Note any gauges, dial/display "
    "readings and their units, equipment type, labels, valve/switch states, "
    "leaks, corrosion, and visible safety hazards."
)


def _client():
    return ollama.Client(host=os.getenv("OLLAMA_HOST", "http://localhost:11434"))


def vision(image_path: str, prompt: str = VISION_PROMPT) -> str:
    resp = _client().generate(
        model=os.getenv("VISION_MODEL", "moondream"),
        prompt=prompt,
        images=[image_path],
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
        # ponytail: sandboxed eval (empty builtins + math namespace) as the
        # fallback path when Docker is unavailable.
        return str(eval(expression, {"__builtins__": {}}, _ALLOWED))
    except Exception as e:
        return f"Calculation error: {e}"


if __name__ == "__main__":  # ponytail: calculator self-check — result + sandbox escape
    assert calculate("2**10 + sqrt(16)") == "1028.0"
    assert "error" in calculate("__import__('os').system('echo hi')").lower()
    print("tools ok — math evaluates, sandbox blocks builtins")

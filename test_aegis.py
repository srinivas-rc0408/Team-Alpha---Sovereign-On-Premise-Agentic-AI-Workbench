# test_aegis.py — Quick smoke test to verify the whole stack works end to end.
# Run with: python test_aegis.py

import sys
import os

# Every FAIL line is counted and the script exits non-zero at the end. It used to
# print FAIL and still exit 0, so the installer's `|| die` could never fire and a
# broken install reported success.
failures = []

print("=" * 60)
print("  AEGIS — System Test")
print("=" * 60)

print("\n[TEST 1] Checking imports...")
try:
    import langchain
    import langgraph
    import faiss
    import ollama
    import rank_bm25
    print("  OK — All libraries imported")
except ImportError as e:
    print(f"  FAIL — Missing library: {e}")
    failures.append("Missing library")
    print("  Run: pip install -r requirements.txt")
    sys.exit(1)

print("\n[TEST 2] Checking Ollama connection...")
try:
    models = ollama.list()
    model_names = [m.model for m in models.models]
    print(f"  OK — Ollama is running, {len(model_names)} models found")
    for name in model_names:
        print(f"       - {name}")
except Exception as e:
    print(f"  FAIL — Cannot connect to Ollama: {e}")
    failures.append("Cannot connect to Ollama")
    print("  Make sure Ollama is running and OLLAMA_HOST in .env is correct")
    sys.exit(1)

print("\n[TEST 3] Checking required models...")
required = ["qwen2.5:7b", "qwen2.5vl:3b", "nomic-embed-text"]
for req in required:
    found = any(req in name for name in model_names)
    status = "OK" if found else "MISSING"
    print(f"  {status} — {req}")
    if not found:
        print(f"       Run: ollama pull {req}")
        failures.append(f"model {req} missing")

print("\n[TEST 4] Checking Docker sandbox (for the calc/Python-REPL tool)...")
import shutil
import subprocess
if shutil.which("docker") and subprocess.run(
    ["docker", "info"], capture_output=True, timeout=5
).returncode == 0:
    print("  OK — Docker daemon reachable, calc tool will run sandboxed (network=none)")
else:
    print("  WARN — Docker not reachable; calc tool will fall back to in-process eval")

print("\n[TEST 5] Checking docs folder...")
docs_path = os.path.join(os.path.dirname(__file__), "docs")
if os.path.exists(docs_path):
    doc_files = [f for f in os.listdir(docs_path) if f.endswith((".pdf", ".txt", ".md"))]
    print(f"  OK — {len(doc_files)} documents found in docs/")
    for f in doc_files:
        print(f"       - {f}")
else:
    print("  FAIL — docs/ folder not found")
    failures.append("docs/ folder not found")

print("\n[TEST 6] Building hybrid (FAISS + BM25) RAG index...")
try:
    from core.rag import build_index
    n = build_index()
    print(f"  OK — indexed {n} chunks")
except Exception as e:
    print(f"  FAIL — RAG build error: {e}")
    failures.append("RAG build error")

print("\n[TEST 7] Testing RAG search...")
try:
    from core.rag import search
    hits = search("pressure limit safety")
    chars = sum(len(h["text"]) for h in hits)
    if hits and chars > 50:
        print(f"  OK — retrieved {len(hits)} chunks, {chars} chars")
        print(f"  Preview: {hits[0]['text'][:100]}...")
    else:
        print("  FAIL — no results returned")
        failures.append("no results returned")
except Exception as e:
    print(f"  FAIL — Search error: {e}")
    failures.append("Search error")

print("\n[TEST 8] Testing sandboxed calc tool...")
try:
    from core.tools import calculate
    result = calculate("18.4 > 15")
    print(f"  OK — calculate('18.4 > 15') = {result}")
except Exception as e:
    print(f"  FAIL — calc error: {e}")
    failures.append("calc error")

print("\n[TEST 9] Quick Qwen2.5-7B response...")
try:
    response = ollama.chat(
        model="qwen2.5:7b",
        messages=[{"role": "user", "content": "Say 'AEGIS online' and nothing else."}],
    )
    answer = response["message"]["content"].strip()
    print(f"  OK — LLM responded: {answer}")
except Exception as e:
    print(f"  FAIL — LLM error: {e}")
    failures.append("LLM error")

print("\n[TEST 10] Safety gate ignores a wrong limit stated in the query...")
try:
    from core.agent import safety_check_node
    state = {
        "trace": [],
        "plan": {"safety_check": {"type": "pressure", "reading": 18.4,
                                  "safe_limit": 20.0, "critical_limit": None}},
    }
    out = safety_check_node(state)
    assert out["safety"]["status"] == "CRITICAL", out["safety"]
    # answer_node quotes these back to the operator, so they must be config's, not the query's.
    assert out["safety_input"]["safe_limit"] == 15.0, out["safety_input"]
    assert out["safety_input"]["critical_limit"] == 18.0, out["safety_input"]
    assert "SOP-MNT-402" in out["safety_input"]["source"], out["safety_input"]
    print("  OK — query's 20 bar overridden by config 15/18, verdict CRITICAL with source")
except Exception as e:
    print(f"  FAIL — safety override error: {e}")
    failures.append("safety override error")

print("\n[TEST 11] Model keep-alive is a type every Ollama client accepts...")
try:
    from core import KEEP_ALIVE, _keep_alive_seconds
    from langchain_ollama import ChatOllama, OllamaEmbeddings
    # OllamaEmbeddings types keep_alive as int-only and rejects Ollama's "60m"
    # duration string, while ChatOllama and the raw client take either — so the
    # index build failed with a ValidationError while every other call worked.
    ChatOllama(model="probe", keep_alive=KEEP_ALIVE)
    OllamaEmbeddings(model="probe", keep_alive=KEEP_ALIVE)
    assert isinstance(KEEP_ALIVE, int) and KEEP_ALIVE > 0, KEEP_ALIVE
    for spelling, want in [("60m", 3600), ("2h", 7200), ("45s", 45), ("900", 900), ("junk", 3600)]:
        os.environ["OLLAMA_KEEP_ALIVE"] = spelling
        assert _keep_alive_seconds() == want, f"{spelling} -> {_keep_alive_seconds()}"
    os.environ.pop("OLLAMA_KEEP_ALIVE", None)
    print(f"  OK — models stay loaded {KEEP_ALIVE}s; chat, embedding and vision clients all accept it")
except Exception as e:
    print(f"  FAIL — keep-alive error: {e}")
    failures.append("keep-alive")

print("\n" + "=" * 60)
if failures:
    print(f"  {len(failures)} check(s) FAILED: {'; '.join(failures)}")
    print("=" * 60)
    sys.exit(1)
print("  All tests complete!")
print("  Run the full agent with:")
print('  python -c "from core.agent import run_aegis; print(run_aegis(\'Pressure is 18.4 bar. Safety violation per SOP-MNT-402?\')[\'answer\'])"')
print("=" * 60)

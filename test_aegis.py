# test_aegis.py — Quick smoke test to verify the whole stack works end to end.
# Run with: python test_aegis.py

import sys
import os

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
    print("  Make sure Ollama is running and OLLAMA_HOST in .env is correct")
    sys.exit(1)

print("\n[TEST 3] Checking required models...")
required = ["qwen2.5:7b", "moondream", "nomic-embed-text"]
for req in required:
    found = any(req in name for name in model_names)
    status = "OK" if found else "MISSING"
    print(f"  {status} — {req}")
    if not found:
        print(f"       Run: ollama pull {req}")

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

print("\n[TEST 6] Building hybrid (FAISS + BM25) RAG index...")
try:
    from core.rag import build_index
    n = build_index()
    print(f"  OK — indexed {n} chunks")
except Exception as e:
    print(f"  FAIL — RAG build error: {e}")

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
except Exception as e:
    print(f"  FAIL — Search error: {e}")

print("\n[TEST 8] Testing sandboxed calc tool...")
try:
    from core.tools import calculate
    result = calculate("18.4 > 15")
    print(f"  OK — calculate('18.4 > 15') = {result}")
except Exception as e:
    print(f"  FAIL — calc error: {e}")

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

print("\n" + "=" * 60)
print("  All tests complete!")
print("  Run the full agent with:")
print('  python -c "from core.agent import run_aegis; print(run_aegis(\'Pressure is 18.4 bar. Safety violation per SOP-MNT-402?\')[\'answer\'])"')
print("=" * 60)

# test_ui.py — The console must never show a Streamlit error screen.
# Renders ui/app.py headlessly (Streamlit's AppTest, no browser, no Ollama needed)
# against the states a real plant install eventually reaches: power cut mid-write,
# corrupted or hand-edited files, chats from older builds, Ollama stopped.
# Run with: python test_ui.py

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "ui", "app.py")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from streamlit.testing.v1 import AppTest  # noqa: E402

failures = []


def sandbox():
    """Point every data path at a fresh temp dir; never touches real data/ or logs/."""
    tmp = tempfile.mkdtemp()
    os.environ.update({"CHATS_DIR": f"{tmp}/chats", "AUDIT_LOG": f"{tmp}/audit.jsonl",
                       "INDEX_DIR": f"{tmp}/emb"})
    for var in ("OLLAMA_HOST", "DOCS_DIR"):
        os.environ.pop(var, None)
    os.makedirs(f"{tmp}/chats")
    return tmp


def render():
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    return at


def check(name, at, expect=None):
    errors = [e.value for e in at.exception]
    shown = [a.value for a in list(at.error) + list(at.warning) + list(at.success)]
    missing = expect is not None and not any(expect in a for a in shown)
    ok = not errors and not missing
    print(f"  {'OK  ' if ok else 'FAIL'} — {name}")
    if errors:
        print(f"         Streamlit error: {errors[0][:300]}")
    if missing:
        print(f"         expected a message containing {expect!r}; shown: {[a[:80] for a in shown]}")
    if not ok:
        failures.append(name)


print("=" * 60)
print("  AEGIS — console resilience test")
print("=" * 60)

print("\n[1] Damaged files on disk — page must still load")
states = {
    "fresh install, nothing saved yet": lambda t: None,
    "audit log cut off mid-write (power loss)": lambda t: open(f"{t}/audit.jsonl", "w").write(
        '{"ts": "2026-01-01T00:00:00", "event": "plan", "data": {}, "prev_hash": "0", "hash": "1"}\n{"ts": "20'),
    "audit log is garbage": lambda t: open(f"{t}/audit.jsonl", "wb").write(b"not json\n\x00\xff\n"),
    "chat file truncated": lambda t: open(f"{t}/chats/20260101T000000_deadbeef.json", "w").write("{trunc"),
    "chat file is valid JSON, wrong shape": lambda t: open(
        f"{t}/chats/20260101T000000_deadbeef.json", "w").write("[1, 2]"),
    "chat index truncated": lambda t: open(f"{t}/chats/index.json", "w").write('[{"id": '),
    "chat index has malformed items": lambda t: open(f"{t}/chats/index.json", "w").write(
        json.dumps({"version": 1, "sessions": [{"title": "no id"}, "junk"]})),
    "Ollama stopped": lambda t: os.environ.__setitem__("OLLAMA_HOST", "http://127.0.0.1:9"),
}
for name, setup in states.items():
    tmp = sandbox()
    setup(tmp)
    check(name, render())
    shutil.rmtree(tmp, ignore_errors=True)

print("\n[2] Operator actions that go wrong — clear message, never a crash")
tmp = sandbox()
from core import history  # noqa: E402  (after the env points at the sandbox)

legacy_id = "20250101T000000_01de55aa"
json.dump({"id": legacy_id, "title": "chat from an older build", "updated_at": "2025-01-01T00:00:00",
           "entries": [{"answer": "a", "safety": {"status": "CRITICAL"}, "timing": ["not", "a", "dict"],
                        "steps": "garbage", "network_audit": {"clean": True}}]},
          open(f"{tmp}/chats/{legacy_id}.json", "w"))
history.rebuild_index()

at = render(); at.button(key="run").click().run()
check("run with an empty question", at, "Type a question")
at = render(); at.text_area(key="query_input").input("x" * 9000).run(); at.button(key="run").click().run()
check("run with a pasted whole document", at, "limit is")
at = render(); at.text_input(key="history_search").input("([{*+?\\").run()
check("search history with symbols", at)
at = render(); at.button(key=f"chat_{legacy_id}").click().run()
check("open a chat saved by an older build", at)

ghost = history.new_session()
ghost["title"] = "deleted elsewhere"
history.save_session(ghost)
at = render()
os.remove(f"{tmp}/chats/{ghost['id']}.json")
at.button(key=f"chat_{ghost['id']}").click().run()
check("open a chat whose file was deleted meanwhile", at)

at = render(); at.button(key=f"chat_{legacy_id}").click().run()
at.button(key=f"delbtn_{legacy_id}").click().run()
check("delete the chat that is currently open", at)

os.makedirs(f"{tmp}/nodocs")
os.environ["DOCS_DIR"] = f"{tmp}/nodocs"
at = render(); at.button(key="rebuild").click().run()
check("rebuild the index with no documents", at, "knowledge base is empty")
os.environ.pop("DOCS_DIR")

os.environ["OLLAMA_HOST"] = "http://127.0.0.1:9"
at = render(); at.button(key="privacy_btn").click().run()
check("privacy self-audit with Ollama stopped", at)
at = render(); at.text_area(key="query_input").input("Pressure 18.4 bar, safe limit 15. Violation?").run()
at.button(key="run").click().run()
check("run a query with Ollama stopped", at, "ollama serve")
if at.button(key="run").disabled:
    print("  FAIL — Run button stayed locked after a failed query")
    failures.append("run button unlock")
else:
    print("  OK   — Run button unlocks after a failed query")
shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 60)
if failures:
    print(f"  {len(failures)} FAILED: {', '.join(failures)}")
    print("=" * 60)
    sys.exit(1)
print("  All console resilience checks passed.")
print("=" * 60)

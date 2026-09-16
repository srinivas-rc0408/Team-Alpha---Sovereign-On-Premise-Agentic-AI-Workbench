"""Local chat persistence — one JSON file per session under data/chats/.

Plain files on local disk, no database and no network: history survives restarts,
can be read with `cat`, backed up by copying a folder, and destroyed by deleting
one. That transparency is the point in an air-gapped plant — an operator's
question log is exactly the kind of thing that must never end up in a service
someone else can read.

Each session file holds the full record of every run in it: the query, the six
node results, the deterministic safety verdict, the sign-off flag, the reasoning
trace, the network audit, and the audit-log hashes that run produced — enough to
reconstruct the whole report later without re-running the model.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone

from . import audit

# Session ids are used to build file paths from UI-supplied values, so they are
# whitelist-validated rather than sanitised — no traversal shape can survive this.
_ID_RE = re.compile(r"^\d{8}T\d{6}_[0-9a-f]{8}$")

_STEP_KEYS = ("plan", "vision", "context", "calc", "safety", "reflection", "answer")


def chats_dir() -> str:
    return os.getenv("CHATS_DIR", "data/chats")


def _path(chat_id: str) -> str:
    if not _ID_RE.match(chat_id or ""):
        raise ValueError(f"invalid chat id: {chat_id!r}")
    return os.path.join(chats_dir(), f"{chat_id}.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session() -> dict:
    """An empty session. Not written to disk until it has something to save."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return {
        "id": f"{stamp}_{uuid.uuid4().hex[:8]}",
        "created_at": _now(),
        "updated_at": _now(),
        "title": "New chat",
        "entries": [],
    }


def audit_cursor() -> int:
    """Number of audit entries written so far. Take this before an agent run and
    pass it to record_run() to attach exactly that run's hash-chain links."""
    return len(audit.read())


def _hashes_since(cursor: int) -> list[dict]:
    return [
        {"event": e["event"], "ts": e["ts"], "hash": e["hash"], "prev_hash": e["prev_hash"]}
        for e in audit.read()[cursor:]
    ]


def record_run(session: dict, result: dict, cursor: int = None, timing: dict = None) -> dict:
    """Append one completed agent run to `session` and persist it. Returns the session."""
    entry = {
        "ts": _now(),
        "query": result.get("query", ""),
        "image_path": result.get("image_path"),
        "answer": result.get("answer", ""),
        "steps": {k: result.get(k) for k in _STEP_KEYS},
        "safety": result.get("safety"),
        "requires_approval": bool(result.get("requires_approval")),
        "trace": result.get("trace", []),
        "loop_count": result.get("loop_count", 0),
        "network_audit": result.get("network_audit"),
        "timing": timing or {},
        "audit_hashes": _hashes_since(cursor) if cursor is not None else [],
    }
    session["entries"].append(entry)
    session["updated_at"] = entry["ts"]
    if session.get("title", "New chat") == "New chat" and entry["query"]:
        session["title"] = entry["query"][:70] + ("…" if len(entry["query"]) > 70 else "")
    save_chat(session)
    return session


def save_chat(session: dict) -> str:
    """Write the session atomically: a crash mid-write can't leave a truncated
    file where a readable history used to be."""
    os.makedirs(chats_dir(), exist_ok=True)
    path = _path(session["id"])
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_chat(chat_id: str) -> dict | None:
    path = _path(chat_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _summary(session: dict) -> dict:
    last = session["entries"][-1] if session["entries"] else {}
    return {
        "id": session["id"],
        "title": session.get("title", "New chat"),
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        "entry_count": len(session["entries"]),
        "requires_approval": any(e.get("requires_approval") for e in session["entries"]),
        "last_status": (last.get("safety") or {}).get("status"),
    }


def list_chats() -> list[dict]:
    """Session summaries, newest first. ponytail: reads every file each call —
    fine for the thousands a single operator console accumulates; add an index
    file if that ever stops being true."""
    directory = chats_dir()
    if not os.path.isdir(directory):
        return []
    sessions = []
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                sessions.append(_summary(json.load(f)))
        except (json.JSONDecodeError, KeyError, OSError):
            continue  # a corrupt file shouldn't hide the rest of the history
    return sorted(sessions, key=lambda s: s["updated_at"], reverse=True)


def search_chats(keyword: str) -> list[dict]:
    """Case-insensitive substring match over queries and answers, newest first."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return list_chats()
    hits = []
    for summary in list_chats():
        session = load_chat(summary["id"])
        if not session:
            continue
        haystack = " ".join(
            f"{e.get('query', '')} {e.get('answer', '')}" for e in session["entries"]
        ).lower()
        if kw in haystack or kw in summary["title"].lower():
            hits.append(summary)
    return hits


def delete_chat(chat_id: str) -> bool:
    path = _path(chat_id)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


if __name__ == "__main__":  # ponytail: self-check — round-trip, search, traversal guard
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp()
    os.environ["CHATS_DIR"] = tmpdir
    os.environ["AUDIT_LOG"] = os.path.join(tmpdir, "audit.jsonl")

    cursor = audit_cursor()
    audit.log("answer", {"chars": 12})
    s = new_session()
    record_run(s, {
        "query": "Pressure reading is 18.4 bar, safe limit 15 bar.",
        "answer": "Yes — this exceeds the stated limit.",
        "safety": {"status": "CAUTION"},
        "requires_approval": True,
        "trace": ["PLAN", "ANSWER"],
    }, cursor=cursor, timing={"total": 21.5})

    reloaded = load_chat(s["id"])
    assert reloaded["entries"][0]["answer"] == "Yes — this exceeds the stated limit."
    assert reloaded["entries"][0]["audit_hashes"], "run's audit hashes not captured"
    assert reloaded["title"].startswith("Pressure reading")

    assert [c["id"] for c in list_chats()] == [s["id"]]
    assert search_chats("18.4 bar") and not search_chats("no such text")
    assert search_chats("")  # empty keyword falls back to the full list

    for bad in ("../../etc/passwd", "not-an-id", ""):
        try:
            load_chat(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"traversal guard missed {bad!r}")

    assert delete_chat(s["id"]) and not delete_chat(s["id"])
    assert list_chats() == []

    shutil.rmtree(tmpdir)
    print("history ok — round-trip, audit hashes, search, traversal guard, delete")

"""Local chat persistence — one JSON file per session under data/chats/.

Plain files on local disk, no database and no network: history survives restarts,
can be read with `cat`, backed up by copying a folder, and destroyed by deleting
one. That transparency is the point in an air-gapped plant — an operator's
question log is exactly the kind of thing that must never end up in a service
someone else can read.

Layout:
    data/chats/index.json        fast listing/search without opening every session
    data/chats/<id>.json         the full record of one session
    data/chats/media/<id>/       images attached to that session, copied in
    data/chats/exports/          human-readable exports produced on demand

Every session file holds the full record of each run in it: the query, the six
node results, the deterministic safety verdict, the sign-off flag, the reasoning
trace, the network audit, and the audit-log hashes that run produced — enough to
reconstruct the whole report later without re-running the model.

index.json is a derived cache, never the source of truth. If it is missing,
truncated or stale it is rebuilt from the session files, so a crash mid-write
can cost at most the speed of one listing, never the history itself.
"""
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone

from . import audit

# Session ids are used to build file paths from UI-supplied values, so they are
# whitelist-validated rather than sanitised — no traversal shape can survive this.
_ID_RE = re.compile(r"^\d{8}T\d{6}_[0-9a-f]{8}$")

# safety_input rides along with the verdict: the console renders the limits and
# SOP citation a verdict was computed against, so a reloaded run has to show the
# same numbers rather than falling back to whatever the query claimed.
_STEP_KEYS = ("plan", "vision", "context", "calc", "safety", "safety_input", "reflection", "answer")

_CRITICAL = {"CRITICAL", "EXCEEDS", "BELOW_MINIMUM"}
_WARNING = {"CAUTION"}

INDEX_NAME = "index.json"


# ── paths ────────────────────────────────────────────────────────────────────
def chats_dir() -> str:
    return os.getenv("CHATS_DIR", "data/chats")


def media_dir(session_id: str) -> str:
    return os.path.join(chats_dir(), "media", _checked(session_id))


def _checked(chat_id: str) -> str:
    if not _ID_RE.match(chat_id or ""):
        raise ValueError(f"invalid chat id: {chat_id!r}")
    return chat_id


def _path(chat_id: str) -> str:
    return os.path.join(chats_dir(), f"{_checked(chat_id)}.json")


def _index_path() -> str:
    return os.path.join(chats_dir(), INDEX_NAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_atomic(path: str, payload) -> str:
    """Write to a temp file in the same directory, then rename. os.replace is
    atomic on POSIX and on Windows, so a reader never sees a half-written file
    and a crash cannot truncate the previous good copy.

    The temp name carries a uuid as well as the pid: Streamlit serves concurrent
    sessions as threads of ONE process, so a pid-only name lets two simultaneous
    writers share a temp path — and the first to finish deletes the other's file
    out from under it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


# ── sessions ─────────────────────────────────────────────────────────────────
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
    pass it to save_turn() to attach exactly that run's hash-chain links."""
    return len(audit.read())


def _hashes_since(cursor: int) -> list[dict]:
    return [
        {"event": e["event"], "ts": e["ts"], "hash": e["hash"], "prev_hash": e["prev_hash"]}
        for e in audit.read()[cursor:]
    ]


def status_of(entry: dict) -> str:
    """SAFE / WARNING / CRITICAL for the sidebar badge."""
    verdict = (entry.get("safety") or {}).get("status")
    if verdict in _CRITICAL:
        return "CRITICAL"
    if verdict in _WARNING or entry.get("requires_approval"):
        return "WARNING"
    return "SAFE"


def store_media(session_id: str, source_path: str) -> str | None:
    """Copy an uploaded image into the session's own folder so the history stays
    self-contained — the Streamlit temp file it came from is gone on restart."""
    if not source_path or not os.path.exists(source_path):
        return None
    try:
        target_dir = media_dir(session_id)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, f"{uuid.uuid4().hex[:8]}_{os.path.basename(source_path)}")
        shutil.copy2(source_path, target)
        return target
    except OSError:
        return None  # a failed image copy must never lose the run it belongs to


def save_turn(session: dict, result: dict, cursor: int = None, timing: dict = None) -> dict:
    """Append one completed agent run to `session` and persist it immediately."""
    image_path = store_media(session["id"], result.get("image_path"))
    entry = {
        "ts": _now(),
        "query": result.get("query", ""),
        "image_path": image_path,
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
    entry["status"] = status_of(entry)
    session["entries"].append(entry)
    session["updated_at"] = entry["ts"]
    if session.get("title", "New chat") == "New chat" and entry["query"]:
        session["title"] = entry["query"][:70] + ("…" if len(entry["query"]) > 70 else "")
    save_session(session)
    return session


def save_session(session: dict) -> str:
    path = _write_atomic(_path(session["id"]), session)
    _index_upsert(_summary(session))
    return path


def load_session(chat_id: str) -> dict | None:
    path = _path(chat_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            session = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    # Callers render this directly (session["entries"], session["title"]), so a file
    # that parses but isn't a session is treated exactly like one that doesn't parse.
    if not (isinstance(session, dict) and isinstance(session.get("entries"), list)):
        return None
    session.setdefault("id", chat_id)
    session.setdefault("title", "Untitled chat")
    return session


def delete_session(chat_id: str) -> bool:
    path = _path(chat_id)
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
    shutil.rmtree(media_dir(chat_id), ignore_errors=True)
    _index_remove(chat_id)
    return existed


# ── index ────────────────────────────────────────────────────────────────────
def _summary(session: dict) -> dict:
    entries = session.get("entries", [])
    last = entries[-1] if entries else {}
    first_query = next((e.get("query", "") for e in entries if e.get("query")), "")
    return {
        "id": session["id"],
        "title": session.get("title", "New chat"),
        "preview": (first_query[:120] + "…") if len(first_query) > 120 else first_query,
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        "entry_count": len(entries),
        "requires_approval": any(e.get("requires_approval") for e in entries),
        "status": last.get("status") or (status_of(last) if last else "SAFE"),
        "last_status": (last.get("safety") or {}).get("status"),
    }


_SUMMARY_KEYS = ("id", "title", "updated_at", "entry_count")


def _valid_summary(item) -> bool:
    """The sidebar indexes these keys on every render; one malformed item used to
    take the whole console down, so nothing reaches it unchecked."""
    return (isinstance(item, dict) and all(k in item for k in _SUMMARY_KEYS)
            and isinstance(item["id"], str) and bool(_ID_RE.match(item["id"])))


def _read_index() -> list[dict] | None:
    try:
        with open(_index_path(), encoding="utf-8") as f:
            data = json.load(f)
        if (isinstance(data, dict) and isinstance(data.get("sessions"), list)
                and all(_valid_summary(i) for i in data["sessions"])):
            return data["sessions"]
    except (json.JSONDecodeError, OSError, TypeError, UnicodeDecodeError):
        pass
    return None  # missing, corrupt or malformed — caller rebuilds from the session files


def _write_index(sessions: list[dict]) -> None:
    ordered = sorted(sessions, key=lambda s: s.get("updated_at", ""), reverse=True)
    _write_atomic(_index_path(), {"version": 1, "updated_at": _now(), "sessions": ordered})


def rebuild_index() -> list[dict]:
    """Derive the index from the session files on disk — the authoritative copy."""
    directory = chats_dir()
    summaries = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json") or name == INDEX_NAME:
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    session = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue  # one corrupt session must not hide the rest
            # The id inside must be valid AND match the filename: a session is loaded
            # by id, so a mismatch would list a chat that can never be opened, and an
            # invalid id would trip the traversal guard mid-render.
            if not isinstance(session, dict) or session.get("id") != name[:-len(".json")]:
                continue
            try:
                summary = _summary(session)
            except (KeyError, TypeError, AttributeError):
                continue
            if _valid_summary(summary):
                summaries.append(summary)
    _write_index(summaries)
    return sorted(summaries, key=lambda s: s["updated_at"], reverse=True)


# Updating the index is read-modify-write, so two concurrent writers can each
# read the pre-update list and the second write drops the first one's entry.
# ponytail: process-wide lock — covers Streamlit's threads, which is where the
# races actually happen. Two separate AEGIS processes could still interleave, and
# the cost there is a stale listing that rebuild_index() repairs, never lost chats
# (the session files themselves are written independently of this lock).
_INDEX_LOCK = threading.Lock()


def _index_upsert(summary: dict) -> None:
    with _INDEX_LOCK:
        sessions = _read_index()
        if sessions is None:
            rebuild_index()
            return
        sessions = [s for s in sessions if s.get("id") != summary["id"]] + [summary]
        _write_index(sessions)


def _index_remove(chat_id: str) -> None:
    with _INDEX_LOCK:
        sessions = _read_index()
        if sessions is None:
            rebuild_index()
            return
        _write_index([s for s in sessions if s.get("id") != chat_id])


def list_sessions() -> list[dict]:
    """Session summaries, newest first, served from index.json when it is usable."""
    sessions = _read_index()
    if sessions is None:
        return rebuild_index()
    return sorted(sessions, key=lambda s: s.get("updated_at", ""), reverse=True)


def session_count() -> int:
    return len(list_sessions())


def search_sessions(keyword: str) -> list[dict]:
    """Case-insensitive match over title/preview first (index-only, no file reads),
    falling back to the full session text so older or longer chats still match."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return list_sessions()
    hits = []
    for summary in list_sessions():
        if kw in summary.get("title", "").lower() or kw in summary.get("preview", "").lower():
            hits.append(summary)
            continue
        session = load_session(summary["id"])
        if not session:
            continue
        haystack = " ".join(
            f"{e.get('query', '')} {e.get('answer', '')}" for e in session.get("entries", [])
        ).lower()
        if kw in haystack:
            hits.append(summary)
    return hits


def export_session(chat_id: str, out_dir: str = None) -> str | None:
    """Write one readable Markdown file containing the whole session — the form an
    engineer can attach to a work order or hand to an auditor."""
    session = load_session(chat_id)
    if not session:
        return None
    out_dir = out_dir or os.path.join(chats_dir(), "exports")
    os.makedirs(out_dir, exist_ok=True)

    lines = [
        f"# AEGIS session — {session.get('title', chat_id)}",
        "",
        f"- Session id: `{session['id']}`",
        f"- Created: {session.get('created_at', '')}",
        f"- Last updated: {session.get('updated_at', '')}",
        f"- Runs: {len(session.get('entries', []))}",
        "",
        "> Generated locally by AEGIS. No part of this session left the machine.",
        "",
    ]
    for i, e in enumerate(session.get("entries", []), 1):
        safety = e.get("safety") or {}
        lines += [
            f"## Run {i} — {e.get('ts', '')}",
            "",
            f"**Status:** {e.get('status', status_of(e))}",
            "",
            "### Query", "", e.get("query", "_(none)_"), "",
        ]
        if e.get("image_path"):
            lines += [f"**Attached image:** `{e['image_path']}`", ""]
        lines += ["### Answer", "", e.get("answer", "_(none)_"), ""]
        if safety:
            lines += [
                "### Deterministic safety verdict", "",
                f"- Status: **{safety.get('status')}**",
                f"- Severity: {safety.get('severity')}",
                f"- Overage: {safety.get('overage')}",
                f"- Action: {safety.get('action')}",
                "",
            ]
        lines += [
            f"**Requires supervisor sign-off:** {'yes' if e.get('requires_approval') else 'no'}",
            "",
            "### Reasoning trace", "",
            *(f"- {t}" for t in e.get("trace", [])),
            "",
            "### Timings", "",
            *(f"- {k}: {v}s" for k, v in (e.get("timing") or {}).items()),
            "",
            "### Audit hash chain", "",
            *(f"- `{h['event']}` → `{h['hash'][:16]}…` (prev `{h['prev_hash'][:16]}…`)"
              for h in e.get("audit_hashes", [])),
            "",
        ]
        net = e.get("network_audit") or {}
        if net:
            lines += [
                "### Network audit", "",
                f"- External connections: {len(net.get('external_connections', []))} "
                f"({'clean' if net.get('clean') else 'LEAK DETECTED'})",
                "",
            ]
    path = os.path.join(out_dir, f"{session['id']}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# Names the UI and earlier code already use.
record_run = save_turn
save_chat = save_session
load_chat = load_session
list_chats = list_sessions
search_chats = search_sessions
delete_chat = delete_session


if __name__ == "__main__":  # ponytail: self-check — round-trip, index, search, traversal, export
    import tempfile

    tmpdir = tempfile.mkdtemp()
    os.environ["CHATS_DIR"] = tmpdir
    os.environ["AUDIT_LOG"] = os.path.join(tmpdir, "audit.jsonl")

    cursor = audit_cursor()
    audit.log("answer", {"chars": 12})
    s = new_session()
    save_turn(s, {
        "query": "Pressure reading is 18.4 bar, safe limit 15 bar.",
        "answer": "Yes — this exceeds the stated limit.",
        "safety": {"status": "CAUTION", "severity": "medium", "overage": 3.4, "action": "Monitor."},
        "requires_approval": True,
        "trace": ["PLAN", "ANSWER"],
    }, cursor=cursor, timing={"total": 21.5})

    reloaded = load_session(s["id"])
    assert reloaded["entries"][0]["answer"] == "Yes — this exceeds the stated limit."
    assert reloaded["entries"][0]["audit_hashes"], "run's audit hashes not captured"
    assert reloaded["entries"][0]["status"] == "WARNING", reloaded["entries"][0]["status"]
    assert reloaded["title"].startswith("Pressure reading")

    # index.json exists, is used, and survives corruption by rebuilding
    assert os.path.exists(_index_path()), "index.json not written"
    assert [c["id"] for c in list_sessions()] == [s["id"]]
    open(_index_path(), "w").write("{ this is not json")
    assert [c["id"] for c in list_sessions()] == [s["id"]], "corrupt index not rebuilt"
    assert _read_index() is not None, "index not repaired after rebuild"

    assert search_sessions("18.4 bar") and not search_sessions("no such text")
    assert search_sessions("")  # empty keyword falls back to the full list
    assert session_count() == 1

    exported = export_session(s["id"])
    assert exported and os.path.exists(exported)
    body = open(exported, encoding="utf-8").read()
    assert "18.4 bar" in body and "CAUTION" in body and "Audit hash chain" in body

    for bad in ("../../etc/passwd", "..", "not-an-id", "", "20260916T000000_zzzzzzzz/../x"):
        try:
            load_session(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"traversal guard missed {bad!r}")

    # Anything that reaches the sidebar must be a well-formed summary with a valid id:
    # the UI indexes chat["id"], chat["title"], chat["updated_at"] on every render.
    ids = [c["id"] for c in list_sessions()]
    d = chats_dir()
    open(os.path.join(d, "20260916T000000_aaaaaaaa.json"), "w").write("[1, 2]")          # JSON, wrong shape
    json.dump({"id": "../evil", "title": "x", "entries": []},
              open(os.path.join(d, "20260916T000000_bbbbbbbb.json"), "w"))              # hostile id inside
    json.dump({"id": "20260916T000000_dddddddd", "title": "x", "entries": []},
              open(os.path.join(d, "20260916T000000_cccccccc.json"), "w"))              # id != filename
    json.dump({"id": "stray", "title": "x"}, open(os.path.join(d, "notes.json"), "w"))  # stray file
    assert [c["id"] for c in rebuild_index()] == ids, "malformed session files leaked into the index"
    _write_atomic(_index_path(), {"version": 1, "sessions": [{"title": "no id"}, {"id": "../x"}, "junk"]})
    assert [c["id"] for c in list_sessions()] == ids, "malformed index items reached the UI"
    # load_session hands its result straight to the UI, which reads session["entries"].
    assert load_session("20260916T000000_aaaaaaaa") is None, "non-dict session returned"
    open(os.path.join(d, "20260916T000000_eeeeeeee.json"), "wb").write(b"\xff\xfe\x00garbage")
    assert load_session("20260916T000000_eeeeeeee") is None, "binary garbage raised"
    os.remove(os.path.join(d, "20260916T000000_eeeeeeee.json"))
    for name in ("20260916T000000_aaaaaaaa.json", "20260916T000000_bbbbbbbb.json",
                 "20260916T000000_cccccccc.json", "notes.json"):
        os.remove(os.path.join(d, name))

    assert delete_session(s["id"]) and not delete_session(s["id"])
    assert list_sessions() == []

    shutil.rmtree(tmpdir)
    print("history ok — round-trip, index rebuild, search, export, traversal guard, delete")

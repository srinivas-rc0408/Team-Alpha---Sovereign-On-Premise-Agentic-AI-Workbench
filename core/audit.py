"""SHA-256 hash-chained audit log. Append-only JSONL, tamper-evident.

Each entry's hash covers the previous hash, so altering any past line breaks
every hash after it — verify() finds the first broken link.
"""
import hashlib
import json
import os
import threading
from datetime import datetime, timezone

GENESIS = "0" * 64

# The console calls read() and verify() on every rerun — every click and keystroke
# — and the log only grows (~9 entries per query). Uncached, a year of plant use
# (~130k entries) cost ~0.85s verify + ~0.4s read per click and ~0.3s per log().
# Streamlit serves each browser session on its own thread, hence the lock.
_lock = threading.Lock()
_read_cache: dict = {}    # path -> (inode, bytes consumed, entries)
_verify_cache: dict = {}  # path -> (identity, (ok, n), inode, verified bytes, prefix sha256, prev hash)


def _hash(prev_hash: str, payload: dict) -> str:
    body = prev_hash + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _path(path=None):
    return path or os.getenv("AUDIT_LOG", "logs/audit.jsonl")


def _parse(raw) -> dict | None:
    """One log line -> entry, or None for a line that isn't a whole entry: a torn
    write from power loss or Ctrl+C mid-append, or plain garbage.

    Every reader funnels through here because all of them sit on hot paths — the UI
    verifies on every render and the agent logs on every step — so a single torn
    line used to crash the console on every load and fail every query. Readers
    decide what a bad line means; none of them may raise on one."""
    try:
        entry = json.loads(raw)
    except ValueError:  # includes UnicodeDecodeError on binary garbage
        return None
    if isinstance(entry, dict) and all(k in entry for k in ("event", "hash", "prev_hash")):
        return entry
    return None


def _last_hash(path: str) -> str:
    """Chain new entries to the last intact one, reading backwards from the end so
    an append costs the same on day one and year three. The torn line itself stays
    in the file, so verify() still reports the break — the log never silently heals."""
    if not os.path.exists(path):
        return GENESIS
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        carry = b""  # the incomplete first line of the block last read
        while pos > 0:
            step = min(8192, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + carry).split(b"\n")
            # lines[0] may continue into the block before; it's only whole at pos 0.
            carry, complete = (b"", lines) if pos == 0 else (lines[0], lines[1:])
            for raw in reversed(complete):
                if raw.strip() and (entry := _parse(raw)) is not None:
                    return entry["hash"]
    return GENESIS


def log(event: str, data: dict, path: str = None) -> dict:
    path = _path(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    prev = _last_hash(path)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "data": data,
        "prev_hash": prev,
    }
    entry = {**payload, "hash": _hash(prev, payload)}
    # A torn write leaves the file without its trailing newline. Appending straight
    # onto it would fuse this entry into the broken line and lose it, so start a
    # fresh line first. One byte read, only at the file's end.
    starts_clean = True
    if os.path.exists(path) and os.path.getsize(path):
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            starts_clean = f.read(1) == b"\n"
    with open(path, "a") as f:
        f.write(("" if starts_clean else "\n") + json.dumps(entry) + "\n")
    return entry


def read(path: str = None) -> list[dict]:
    """Return parsed log entries in order, hash and prev_hash intact, for display.

    Incremental: the log is append-only, so only bytes past the last read are
    parsed. Only newline-terminated lines are consumed, so a torn tail is picked up
    once it's completed or followed. A shrunk or replaced file is reparsed whole.
    ponytail: an in-place rewrite that grows the file isn't reparsed for display —
    verify() is the integrity check and does catch it."""
    path = _path(path)
    with _lock:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            _read_cache.pop(path, None)
            return []
        inode, offset, entries = _read_cache.get(path, (None, 0, []))
        if inode != st.st_ino or st.st_size < offset:
            offset, entries = 0, []
        if st.st_size > offset:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(st.st_size - offset)
            end = chunk.rfind(b"\n") + 1
            for raw in chunk[:end].split(b"\n"):
                if raw.strip() and (entry := _parse(raw)) is not None:
                    entries.append(entry)
            offset += end
        _read_cache[path] = (st.st_ino, offset, entries)
        return list(entries)


def verify(path: str = None) -> tuple[bool, int]:
    """Return (ok, entries_checked). ok is False if the chain is broken — a forged
    entry or an unreadable line; entries_checked is where the break was found.

    Unchanged file (identity includes ctime, which no user-space call can set):
    cached answer. Grown file: the already-verified prefix is re-hashed as raw
    bytes — one SHA-256 pass, far cheaper than re-parsing and re-hashing every
    entry — and if it's byte-identical, only the new lines are chain-checked. Any
    edit to an old line changes that digest and forces a full re-check, so
    tampering is caught exactly as before; a query's verify just stops costing a
    full pass over a year of history."""
    path = _path(path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return True, 0
    key = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    with _lock:
        cached = _verify_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]

    start = (0, hashlib.sha256(), GENESIS, 0)
    if cached and cached[1][0] and cached[2] == st.st_ino and st.st_size >= cached[3]:
        _, (_, n), _, done, digest, prev = cached
        prefix = _prefix_sha256(path, done)
        if prefix.hexdigest() == digest:
            start = (done, prefix, prev, n)
    result, done, digest, prev = _verify_from(path, *start)
    with _lock:
        _verify_cache[path] = (key, result, st.st_ino, done, digest, prev)
    return result


def _prefix_sha256(path: str, length: int):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while length > 0:
            chunk = f.read(min(1 << 20, length))
            if not chunk:
                break
            h.update(chunk)
            length -= len(chunk)
    return h


def _verify_from(path: str, offset: int, digest, prev: str, n: int):
    """Chain-check complete lines from byte `offset`, continuing a verified prefix
    whose running SHA-256 is `digest`. Returns ((ok, n), bytes verified, prefix
    hex digest, last hash). A torn tail with no newline yet is a break, as any
    unreadable line is — reported, never raised."""
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                return (False, n), offset, digest.hexdigest(), prev
            if line.strip():
                entry = _parse(line)
                # An unreadable line breaks the chain — integrity can't be vouched
                # for past it — reported the same way as a forged entry.
                if entry is None:
                    return (False, n), offset, digest.hexdigest(), prev
                stored = entry.pop("hash")
                if entry["prev_hash"] != prev or _hash(prev, entry) != stored:
                    return (False, n), offset, digest.hexdigest(), prev
                prev = stored
                n += 1
            digest.update(line)
            offset += len(line)
    return (True, n), offset, digest.hexdigest(), prev


if __name__ == "__main__":  # ponytail: chain self-check — proves tampering is caught
    import tempfile

    p = tempfile.mktemp(suffix=".jsonl")
    log("a", {"x": 1}, p)
    log("b", {"x": 2}, p)
    log("c", {"x": 3}, p)
    assert verify(p) == (True, 3)
    assert [e["event"] for e in read(p)] == ["a", "b", "c"]

    lines = open(p).read().splitlines()
    e = json.loads(lines[1])
    e["data"]["x"] = 999  # forge a past entry, keep its old hash
    lines[1] = json.dumps(e)
    open(p, "w").write("\n".join(lines) + "\n")
    ok, at = verify(p)
    assert not ok, "tampering not detected"
    os.remove(p)

    # A torn final line (power loss / Ctrl+C mid-write) must break the chain, never
    # crash: every reader sits on the UI's render path and the agent's log path.
    t = tempfile.mktemp(suffix=".jsonl")
    log("a", {"x": 1}, t)
    good = log("b", {"x": 2}, t)
    with open(t, "a") as f:
        f.write('{"ts": "2026-09-16T10:00:00", "event": "ans')
    assert [e["event"] for e in read(t)] == ["a", "b"]
    assert verify(t) == (False, 2), verify(t)
    after = log("c", {"x": 3}, t)          # the agent can keep logging
    assert after["prev_hash"] == good["hash"]
    assert [e["event"] for e in read(t)] == ["a", "b", "c"]
    open(t, "w").write("not json\n[1, 2]\n")  # garbage, and valid JSON of the wrong shape
    assert read(t) == [] and verify(t) == (False, 0)
    os.remove(t)

    # Caches stay correct: the UI calls read()/verify() on every rerun, so they are
    # cached — but appends, truncation and same-size tampering must all show through.
    import time
    c = tempfile.mktemp(suffix=".jsonl")
    for i in range(3):
        log("e", {"i": i}, c)
    assert verify(c) == (True, 3) and len(read(c)) == 3
    log("e", {"i": 3}, c)                                   # append after caching
    assert verify(c) == (True, 4) and [e["data"]["i"] for e in read(c)] == [0, 1, 2, 3]
    time.sleep(0.05)                                        # past the fs timestamp tick
    raw = open(c).read()
    open(c, "w").write(raw.replace('"i": 1}', '"i": 7}'))   # same size, forged
    assert verify(c)[0] is False, "same-size tamper hidden by the verify cache"
    # Incremental verify: forge an OLD line, then append — the new tail verifies on
    # its own, so only the prefix check can catch this.
    open(c, "w").write(raw)
    assert verify(c) == (True, 4)
    time.sleep(0.05)
    open(c, "w").write(raw.replace('"i": 0}', '"i": 5}'))
    log("e", {"i": 4}, c)
    assert verify(c)[0] is False, "old-line forgery hidden by incremental verify"
    open(c, "w").write("")                                  # truncated / rotated
    assert read(c) == [] and verify(c) == (True, 0)
    os.remove(c)

    # The tail-read previous hash must match a full scan, across block boundaries.
    b = tempfile.mktemp(suffix=".jsonl")
    for i in range(400):
        last = log("e", {"pad": "x" * 60, "i": i}, b)
    with open(b, "a") as f:
        f.write('{"torn')
    assert _last_hash(b) == last["hash"]
    os.remove(b)

    print("audit ok — tamper detected at entry", at, "· torn/garbage lines break the chain without crashing")

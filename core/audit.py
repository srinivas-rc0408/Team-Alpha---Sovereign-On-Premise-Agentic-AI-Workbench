"""SHA-256 hash-chained audit log. Append-only JSONL, tamper-evident.

Each entry's hash covers the previous hash, so altering any past line breaks
every hash after it — verify() finds the first broken link.
"""
import hashlib
import json
import os
from datetime import datetime, timezone

GENESIS = "0" * 64


def _hash(prev_hash: str, payload: dict) -> str:
    body = prev_hash + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _path(path=None):
    return path or os.getenv("AUDIT_LOG", "logs/audit.jsonl")


def _last_hash(path: str) -> str:
    if not os.path.exists(path):
        return GENESIS
    last = GENESIS
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                last = json.loads(line)["hash"]
    return last


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
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read(path: str = None) -> list[dict]:
    """Return parsed log entries in order, hash and prev_hash intact, for display."""
    path = _path(path)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def verify(path: str = None) -> tuple[bool, int]:
    """Return (ok, entries_checked). ok is False if the chain was tampered with."""
    path = _path(path)
    if not os.path.exists(path):
        return True, 0
    prev = GENESIS
    n = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            stored = entry.pop("hash")
            if entry["prev_hash"] != prev or _hash(prev, entry) != stored:
                return False, n
            prev = stored
            n += 1
    return True, n


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
    print("audit ok — tamper detected at entry", at)

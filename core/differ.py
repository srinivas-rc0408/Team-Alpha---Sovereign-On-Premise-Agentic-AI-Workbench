"""Document revision comparison — the manual old-vs-new SOP/P&ID comparison
plant engineers do by hand, automated. Pure stdlib diffing (difflib) for the
structural diff; the LLM only summarizes and flags what changed, grounded in
that diff — it never invents a change that isn't actually there.
"""
import difflib
import os

from langchain_ollama import ChatOllama


def _extract_text(path: str) -> str:
    ext = path.lower().rsplit(".", 1)[-1]
    if ext == "pdf":
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _sections(text: str) -> list[str]:
    """Split on blank lines: a reasonable proxy for "section" in an SOP without
    depending on any particular numbering convention."""
    return [s.strip() for s in text.split("\n\n") if s.strip()]


def diff_documents(old_path: str, new_path: str) -> dict:
    """Section-level diff between two document revisions. Returns
    {"added": [...], "removed": [...], "modified": [{"old": ..., "new": ...}]}."""
    old_secs = _sections(_extract_text(old_path))
    new_secs = _sections(_extract_text(new_path))
    sm = difflib.SequenceMatcher(None, old_secs, new_secs)

    added, removed, modified = [], [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            added.extend(new_secs[j1:j2])
        elif tag == "delete":
            removed.extend(old_secs[i1:i2])
        elif tag == "replace":
            # A replace block can pair genuinely-unrelated sections positionally
            # (SequenceMatcher only knows they're both "not equal", not how similar
            # they are to each other) — only pair sections that are actually similar,
            # so "section removed" + "unrelated section added" isn't misreported as
            # one section being modified into a different one.
            old_block, new_block = old_secs[i1:i2], new_secs[j1:j2]
            candidates = sorted(
                (
                    (difflib.SequenceMatcher(None, o, n).ratio(), oi, ni)
                    for oi, o in enumerate(old_block) for ni, n in enumerate(new_block)
                ),
                reverse=True,
            )
            used_old, used_new = set(), set()
            for ratio, oi, ni in candidates:
                if ratio <= 0.6 or oi in used_old or ni in used_new:
                    continue
                modified.append({"old": old_block[oi], "new": new_block[ni]})
                used_old.add(oi)
                used_new.add(ni)
            removed.extend(o for oi, o in enumerate(old_block) if oi not in used_old)
            added.extend(n for ni, n in enumerate(new_block) if ni not in used_new)
    return {"added": added, "removed": removed, "modified": modified}


def _llm():
    return ChatOllama(
        model=os.getenv("LLM_MODEL", "qwen2.5:7b"),
        base_url=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        temperature=0,
    )


def diff_report(old_path: str, new_path: str) -> str:
    """diff_documents() plus a plain-language summary that flags safety-critical
    changes and whether they look like they need Management of Change review."""
    d = diff_documents(old_path, new_path)
    if not (d["added"] or d["removed"] or d["modified"]):
        return "No differences detected between the two documents."

    parts = []
    if d["added"]:
        parts.append("ADDED:\n" + "\n---\n".join(d["added"]))
    if d["removed"]:
        parts.append("REMOVED:\n" + "\n---\n".join(d["removed"]))
    if d["modified"]:
        parts.append("MODIFIED:\n" + "\n---\n".join(
            f"OLD: {m['old']}\nNEW: {m['new']}" for m in d["modified"]
        ))
    diff_text = "\n\n".join(parts)

    prompt = (
        "You are reviewing a revision diff between two refinery SOP/P&ID documents, shown "
        "below as ADDED/REMOVED/MODIFIED sections. Summarize the changes in plain language "
        "for an engineer. Explicitly call out any change to a safety-critical value (pressure/"
        "temperature/vibration limits, inspection intervals, escalation steps) and state "
        "whether it looks like it needs Management of Change (MOC) review. Base this ONLY on "
        f"the diff below — never describe a change that isn't shown here.\n\n{diff_text}\n\n"
        "SUMMARY:"
    )
    return _llm().invoke(prompt).content.strip()


if __name__ == "__main__":  # ponytail: self-check — a pure-diffing test, no LLM/network needed
    import tempfile

    old = tempfile.mktemp(suffix=".txt")
    new = tempfile.mktemp(suffix=".txt")
    open(old, "w").write("Section 1\nSafe limit: 15 bar.\n\nSection 2\nUnrelated text.")
    open(new, "w").write("Section 1\nSafe limit: 12 bar.\n\nSection 3\nNew content added.")

    d = diff_documents(old, new)
    assert d["modified"] == [{"old": "Section 1\nSafe limit: 15 bar.", "new": "Section 1\nSafe limit: 12 bar."}]
    assert d["removed"] == ["Section 2\nUnrelated text."]
    assert d["added"] == ["Section 3\nNew content added."]

    os.remove(old)
    os.remove(new)
    print("differ ok — added/removed/modified sections correctly identified")

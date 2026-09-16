"""Aegis — Streamlit console for the air-gapped refinery agent."""
import os
import shutil
import subprocess
import sys
import tempfile

# `streamlit run ui/app.py` puts this file's own directory on sys.path, not the
# project root, so the `core` package import below fails unless we add it back.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from core import agent, audit, doc_diff, history, offline_check, rag

st.set_page_config(page_title="Aegis — Refinery AI", page_icon="🛡️", layout="wide")

NAVY, TEAL, ORANGE = "#1B2A38", "#0E7C86", "#E8590C"

st.markdown(
    f"""
    <style>
    section[data-testid="stSidebar"] {{ background-color: {NAVY}; }}
    section[data-testid="stSidebar"] * {{ color: #E6EDF3; }}
    .aegis-header {{
        background: {NAVY}; padding: 1.1rem 1.5rem; border-radius: 8px;
        border-left: 5px solid {ORANGE}; margin-bottom: 1.2rem;
    }}
    .aegis-header h1 {{ color: #FFFFFF; margin: 0; font-size: 2.1rem; letter-spacing: .06em; }}
    .aegis-header p {{ color: #B8C4D0; margin: .25rem 0 0; font-size: .9rem; }}
    .stButton > button[kind="primary"] {{ background-color: {TEAL}; border-color: {TEAL}; }}
    .stButton > button[kind="primary"]:hover {{ background-color: #0b626a; border-color: #0b626a; }}
    .offline-badge {{
        background: #0d3b2e; color: #7EE7C7; border: 1px solid #1f7a5c;
        padding: .45rem .6rem; border-radius: 6px; font-size: .82rem; font-weight: 600;
    }}
    .offline-badge.leak {{ background: #4a1216; color: #FFB3B8; border-color: #a12d38; }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="aegis-header"><h1>🛡️ AEGIS</h1>'
    "<p>Team Alpha &nbsp;|&nbsp; SIH26117 &nbsp;|&nbsp; 100% Air-Gapped &nbsp;|&nbsp; "
    "Air-gapped Engineering Intelligence System</p></div>",
    unsafe_allow_html=True,
)

PIPELINE_STEPS = ["plan", "vision", "rag", "calc", "safety_check", "reflect", "answer"]
STEP_LABELS = {
    "plan": "Plan", "vision": "Vision", "rag": "RAG", "calc": "Calc",
    "safety_check": "Safety Check", "reflect": "Reflect", "answer": "Answer",
}

EXAMPLES = [
    "Pressure reading is 18.4 bar. Safe limit is 15 bar per SOP-402. Is this a violation?",
    "Vibration on pump P-201 is 8.5 mm/s RMS. Check against SOP limits.",
    "Wall thickness reading of 5.2mm vs minimum 6.35mm per Section 3.2",
]

if "session" not in st.session_state:
    st.session_state["session"] = history.new_session()


STATUS_ICON = {"CRITICAL": "🔴", "WARNING": "🟠", "SAFE": "🟢"}


def _load_session(chat_id: str):
    """Restore a past session: its last run repopulates the report panes."""
    session = history.load_session(chat_id)
    if not session:
        st.toast(f"Chat {chat_id} is no longer on disk.")
        return
    st.session_state["session"] = session
    last = session["entries"][-1] if session["entries"] else None
    if last:
        st.session_state["result"] = {
            "query": last["query"],
            "answer": last["answer"],
            "safety": last.get("safety"),
            "requires_approval": last.get("requires_approval", False),
            "trace": last.get("trace", []),
            "loop_count": last.get("loop_count", 0),
            "network_audit": last.get("network_audit"),
            "context": (last.get("steps") or {}).get("context"),
            "image_path": last.get("image_path"),
        }
        st.session_state["step_times"] = last.get("timing", {})
        st.session_state["restored_from"] = session["title"]
    st.session_state["signed_off"] = False


# ─────────────────────────────── sidebar ───────────────────────────────
with st.sidebar:
    offline = offline_check.verify_offline()
    badge_class = "offline-badge" if offline["offline"] else "offline-badge leak"
    badge_text = (
        f"🔒 OFFLINE MODE — {len(offline['external_connections'])} external connections"
        if offline["offline"]
        else f"⚠️ {len(offline['external_connections'])} EXTERNAL CONNECTION(S) DETECTED"
    )
    st.markdown(f'<div class="{badge_class}">{badge_text}</div>', unsafe_allow_html=True)
    st.caption(f"{offline['queries_audited']} past queries network-audited · all clean"
               if offline["checks"]["history"]["ok"]
               else f"⚠️ {offline['checks']['history']['leaking_queries']} past queries leaked")
    for warning in offline.get("warnings", []):
        st.caption(f"⚠️ {warning}")

    if st.button("🔎 Privacy self-audit", use_container_width=True,
                 help="Re-run all four offline checks now and show the raw network log"):
        st.session_state["privacy_audit"] = offline_check.verify_offline()

    audit_result = st.session_state.get("privacy_audit")
    if audit_result:
        with st.expander("Privacy self-audit result", expanded=True):
            if audit_result["offline"]:
                st.success("✅ No data left this machine.")
            else:
                st.error("❌ EXTERNAL CONNECTION DETECTED — see below.")

            for name, check in audit_result["checks"].items():
                st.write(f"{'✅' if check['ok'] else '❌'} **{name}**")

            st.caption("Connections opened by AEGIS's own process tree:")
            local = audit_result.get("local_connections") or []
            external = audit_result.get("external_connections") or []
            if local:
                st.code("\n".join(local), language=None)
                st.caption("All loopback — Ollama on 11434, this console on 8501.")
            else:
                st.code("(no sockets open at this instant)", language=None)
            if external:
                st.error("External:\n" + "\n".join(external))
            else:
                st.caption("External connections: none, now or in any recorded query.")
            st.caption(f"Endpoints: " + ", ".join(
                f"{k}={v['value']}" for k, v in audit_result["checks"]["endpoints"]["endpoints"].items()
            ))

    st.divider()
    st.header("💬 Chat History")
    if st.button("➕ New Chat", use_container_width=True):
        st.session_state["session"] = history.new_session()
        for key in ("result", "step_times", "signed_off", "restored_from"):
            st.session_state.pop(key, None)
        # Deliberately no st.rerun(): the click already triggered this run, and the
        # rest of the script renders the cleared state correctly. Calling rerun here
        # would abort before the search box below is instantiated, and Streamlit
        # discards widget state for widgets absent from the last completed run —
        # silently wiping the user's search term while the box still displays it.

    # Keyed so the value lives in session_state: without a key, a st.rerun() (New
    # Chat, delete, load) resets the Python-side value to "" while the browser
    # still shows the typed text — the list would silently stop matching the box.
    keyword = st.text_input("🔍 Search history", placeholder="pressure, P-201, SOP-402…",
                            key="history_search")
    chats = history.search_sessions(keyword) if keyword.strip() else history.list_sessions()

    total = history.session_count()
    st.caption(f"**{total}** conversation{'s' if total != 1 else ''} stored locally"
               + (f" · {len(chats)} matching" if keyword.strip() else ""))

    current_id = st.session_state["session"]["id"]
    if not chats:
        st.caption("No saved chats yet — run a query to start one."
                   if not keyword.strip() else "No chats match that search.")

    # Scrollable so a long history never pushes the rest of the sidebar off-screen;
    # "content" lets a short list size itself instead of leaving dead space.
    with st.container(height=340 if len(chats) > 4 else "content"):
        for chat in chats[:200]:
            marker = "▸ " if chat["id"] == current_id else ""
            icon = STATUS_ICON.get(chat.get("status", "SAFE"), "🟢")
            row, delete_col = st.columns([5, 1])
            if row.button(f"{marker}{icon} {chat['title'][:40]}", key=f"chat_{chat['id']}",
                          use_container_width=True, help=chat.get("preview") or chat["title"]):
                _load_session(chat["id"])
                st.rerun()
            if delete_col.button("🗑", key=f"del_{chat['id']}", help="Delete this chat"):
                history.delete_session(chat["id"])
                if chat["id"] == current_id:
                    st.session_state["session"] = history.new_session()
                    for key in ("result", "step_times", "signed_off", "restored_from"):
                        st.session_state.pop(key, None)
                st.rerun()
            row.caption(f"{chat['updated_at'][:16].replace('T', ' ')} · "
                        f"{chat['entry_count']} run(s) · {chat.get('status', 'SAFE')}")

    st.divider()
    st.header("Knowledge base")
    if st.button("Rebuild index from docs/"):
        with st.spinner("Embedding documents locally…"):
            try:
                st.success(f"Indexed {rag.build_index()} chunks.")
            except Exception as e:
                st.error(agent.friendly_error(e))

    st.divider()
    st.caption("System health")
    ok, n = audit.verify()
    st.metric("Audit entries", n)
    st.write("Chain integrity:", "✅ intact" if ok else "❌ TAMPERED")
    docker_up = bool(shutil.which("docker")) and subprocess.run(
        ["docker", "info"], capture_output=True, timeout=3
    ).returncode == 0
    st.write("Docker (network=none):", "✅ available" if docker_up else "⚠️ in-process eval fallback")
    st.caption(f"Chats stored in `{history.chats_dir()}`")

# ─────────────────────────────── main ───────────────────────────────
with st.expander("ℹ️ How AEGIS works"):
    st.markdown(
        "```\n"
        "Plan → Vision (if image) → RAG → Calc → Safety Check → Reflect ──┬─→ Answer\n"
        "         ^                                                       │\n"
        "         └───────────────── retry, capped at 2x ─────────────────┘\n"
        "```\n"
        "- **Plan** — the LLM decides which steps this query actually needs.\n"
        "- **Vision** — moondream describes an attached photo (skipped if none).\n"
        "- **RAG** — hybrid FAISS + BM25 search over your indexed SOPs.\n"
        "- **Calc** — arithmetic runs in a Docker sandbox (`network=none`), never in the LLM's head.\n"
        "- **Safety Check** — deterministic code (`core/safety_rules.py`), not an LLM judgment.\n"
        "- **Reflect** — self-critiques the evidence; can loop back to RAG for another pass.\n"
        "- **Answer** — the final response, with three independent gates on the sign-off requirement.\n"
        "\nEvery step is logged to a SHA-256 hash-chained audit trail, every query runs a "
        "network audit proving nothing left the machine, and every run is saved locally to "
        "`data/chats/`."
    )

tab_ask, tab_compare = st.tabs(["Ask AEGIS", "Document Comparison"])

with tab_ask:
    if st.session_state.get("restored_from"):
        st.info(f"📂 Restored from history: **{st.session_state['restored_from']}**")

    st.caption("Try an example:")
    for col, ex in zip(st.columns(len(EXAMPLES)), EXAMPLES):
        # Streamlit ellipsises the label to the column width, so the full query
        # goes in the tooltip rather than being unreadable on the button.
        if col.button(ex[:46] + ("…" if len(ex) > 46 else ""), key=f"ex_{ex[:20]}",
                      help=ex, use_container_width=True):
            st.session_state["query_input"] = ex

    query = st.text_area(
        "Ask about SOPs, equipment, or a gauge reading", height=100, key="query_input"
    )
    img = st.file_uploader("Attach equipment/gauge photo (optional)", type=["png", "jpg", "jpeg"])

    MAX_QUERY_CHARS = 8000

    run_clicked = st.button("Run AEGIS Analysis", type="primary")
    if run_clicked and not query.strip():
        st.warning("Type a question first, or pick one of the examples above.")
    elif run_clicked and len(query) > MAX_QUERY_CHARS:
        # A pasted whole document would blow past the model's context and time out
        # after minutes of work; refuse fast and say what to do instead.
        st.warning(
            f"That query is {len(query):,} characters — the limit is {MAX_QUERY_CHARS:,}. "
            "Shorten the question, and put the source document in `docs/` so AEGIS can "
            "retrieve from it instead."
        )
    elif run_clicked:
        image_path = None
        if img:
            try:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(img.name)[1])
                tmp.write(img.read())
                tmp.close()
                image_path = tmp.name
                st.image(img, width=320)
            except Exception as e:
                # A corrupt or unreadable upload must not sink the text query.
                st.warning(f"Could not read that image ({e}) — continuing without it.")
                image_path = None

        st.caption("AEGIS is processing — all computation on-premise…")
        progress_slots = {step: st.empty() for step in PIPELINE_STEPS}
        for step in PIPELINE_STEPS:
            progress_slots[step].markdown(f"⏳ {STEP_LABELS[step]}")

        result, step_times = None, {}
        cursor = history.audit_cursor()
        try:
            last_t = 0.0
            for node_name, elapsed, state in agent.run_streaming(query, image_path):
                if node_name == "__final__":
                    result = state
                    step_times["total"] = elapsed
                    break
                step_times[node_name] = round(elapsed - last_t, 2)
                last_t = elapsed
                if node_name in progress_slots:
                    progress_slots[node_name].markdown(
                        f"✅ {STEP_LABELS[node_name]} ({step_times[node_name]}s)"
                    )
        except Exception as e:
            # Never show an operator a traceback — friendly_error ends in the exact
            # command that fixes it, and the raw detail stays available on demand.
            st.error(agent.friendly_error(e))
            with st.expander("Technical detail (for support)"):
                st.exception(e)

        if result:
            st.session_state["result"] = result
            st.session_state["step_times"] = step_times
            st.session_state["signed_off"] = False
            st.session_state.pop("restored_from", None)
            try:
                history.save_turn(
                    st.session_state["session"], result, cursor=cursor, timing=step_times
                )
            except OSError as e:
                # The answer is already on screen; a failed write must not hide it.
                st.warning(f"Could not save this run to history: {e}")

    result = st.session_state.get("result")
    if result:
        st.subheader("Answer")
        st.markdown(result.get("answer", "_no answer_"))

        times = st.session_state.get("step_times", {})
        if times:
            st.caption(
                f"Total: {times.get('total', 0)}s · "
                + " · ".join(f"{STEP_LABELS.get(k, k)}: {v}s" for k, v in times.items() if k != "total")
            )

        safety = result.get("safety")
        if safety:
            badge = {"CRITICAL": st.error, "EXCEEDS": st.error, "BELOW_MINIMUM": st.error,
                     "CAUTION": st.warning}.get(safety["status"], st.success)
            badge(f"Deterministic safety check: **{safety['status']}** — {safety['action']} "
                  f"(rule engine, not an LLM judgment)")

        if result.get("requires_approval"):
            if st.session_state.get("signed_off"):
                st.success(f"✅ Signed off by {st.session_state['approver']}")
            else:
                st.warning("⚠️ Flagged as safety-critical — requires supervisor sign-off before acting on it.")
                approver = st.text_input("Supervisor name", key="approver_input")
                if st.button("Sign off") and approver.strip():
                    audit.log("sign_off", {"query": result["query"], "approver": approver.strip()})
                    st.session_state["approver"] = approver.strip()
                    st.session_state["signed_off"] = True
                    st.rerun()

        with st.expander("Reasoning trace"):
            for step in result.get("trace", []):
                st.text(step)
            loops = result.get("loop_count", 0)
            if loops:
                st.caption(f"Reflect sent the agent back for more evidence {loops}x (cap: {agent.MAX_LOOPS}).")
        if result.get("context"):
            with st.expander("Retrieved SOP context (hybrid dense + BM25)"):
                st.text(result["context"])

        net = result.get("network_audit")
        if net:
            with st.expander("Network audit — air-gap proof", expanded=not net["clean"]):
                if net["clean"]:
                    st.success("✅ Zero external connections opened by AEGIS during this query.")
                else:
                    st.error("❌ External connection(s) detected — see below.")
                    st.write(net["external_connections"])
                st.caption(
                    f"System-wide bytes during window: {net['system_wide_bytes_sent']} sent / "
                    f"{net['system_wide_bytes_recv']} recv (includes any other traffic on the "
                    "machine — not AEGIS-specific; the connection list above is what's scoped)."
                )
                st.text("Connections opened by AEGIS's own process tree:")
                st.text("\n".join(net["connections"]) or "(none)")

        with st.expander("Audit hash chain"):
            for i, e in enumerate(audit.read()[-20:]):
                arrow = "  │\n  ▼\n" if i > 0 else ""
                st.text(f"{arrow}[{e['event']}] {e['hash'][:12]}… (prev {e['prev_hash'][:12]}…)")

        if st.session_state["session"]["entries"]:
            if st.button("📄 Export this session", help="Write a readable Markdown copy to data/chats/exports/"):
                path = history.export_session(st.session_state["session"]["id"])
                if path:
                    st.success(f"Exported to `{path}`")
                    with open(path, encoding="utf-8") as f:
                        st.download_button("Download export", f.read(),
                                           file_name=os.path.basename(path), mime="text/markdown")
                else:
                    st.warning("Nothing to export yet.")

with tab_compare:
    st.caption("Compare two SOP/P&ID revisions and flag safety-critical changes.")
    c1, c2 = st.columns(2)
    old_file = c1.file_uploader("Old version", type=["pdf", "txt"], key="old_doc")
    new_file = c2.file_uploader("New version", type=["pdf", "txt"], key="new_doc")

    if st.button("Compare", type="primary") and old_file and new_file:
        old_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(old_file.name)[1])
        old_tmp.write(old_file.read())
        old_tmp.close()
        new_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(new_file.name)[1])
        new_tmp.write(new_file.read())
        new_tmp.close()

        with st.spinner("Diffing documents and assessing safety impact…"):
            try:
                st.session_state["diff_sections"] = doc_diff.diff_documents(old_tmp.name, new_tmp.name)
                st.session_state["diff_report"] = doc_diff.compare_documents(old_tmp.name, new_tmp.name)
            except Exception as e:
                st.error(agent.friendly_error(e))

    d = st.session_state.get("diff_sections")
    report = st.session_state.get("diff_report")
    if d and report:
        c1, c2 = st.columns(2)
        c1.metric("Sections changed", report["changes_count"])
        c2.metric("Safety-critical changes", len(report["safety_critical_changes"]))

        st.subheader("Safety impact assessment")
        st.markdown(report["summary"])

        if report["safety_critical_changes"]:
            st.markdown("**Flagged safety-critical changes** (deterministic keyword scan)")
            for f in report["safety_critical_changes"]:
                tone = st.error if f["risk_level"] == "HIGH" else st.warning
                tone(
                    f"**{f['risk_level']}** · {f['section'] or 'unlabelled section'}\n\n"
                    f"− {f['old_value'] or '_(not present in old revision)_'}\n\n"
                    f"\\+ {f['new_value'] or '_(removed in new revision)_'}"
                )

        if d["modified"]:
            st.markdown("**Modified sections**")
            for m in d["modified"]:
                mc1, mc2 = st.columns(2)
                mc1.markdown(f":orange-background[{m['old']}]")
                mc2.markdown(f":orange-background[{m['new']}]")
        if d["removed"]:
            st.markdown("**Removed sections**")
            for r in d["removed"]:
                st.markdown(f":red-background[{r}]")
        if d["added"]:
            st.markdown("**Added sections**")
            for a in d["added"]:
                st.markdown(f":green-background[{a}]")
        if not report["changes_count"]:
            st.success("No differences detected.")

st.divider()
st.caption("Built for SIH 2026 · Problem Statement SIH26117 · Team Alpha · all data stored locally")

"""Aegis — Streamlit demo for the air-gapped refinery agent."""
import os
import shutil
import subprocess
import sys
import tempfile

# `streamlit run ui/app.py` puts this file's own directory on sys.path, not the
# project root, so the `core` package import below fails unless we add it back.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from core import agent, audit, differ, rag

st.set_page_config(page_title="Aegis — Refinery AI", page_icon="🛡️", layout="wide")

st.markdown(
    """
    <style>
    .stButton > button[kind="primary"] { background-color: #0070C0; border-color: #0070C0; }
    .stButton > button[kind="primary"]:hover { background-color: #005a99; border-color: #005a99; }
    h1 { color: #0070C0; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🛡️ AEGIS")
st.caption("Air-gapped Engineering Intelligence System · Team Alpha · SIH26117 · 100% on-premise · zero internet")

PIPELINE_STEPS = ["plan", "vision", "rag", "calc", "safety_check", "reflect", "answer"]
STEP_LABELS = {
    "plan": "Plan", "vision": "Vision", "rag": "RAG", "calc": "Calc",
    "safety_check": "Safety Check", "reflect": "Reflect", "answer": "Answer",
}

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
        "\nEvery step is logged to a SHA-256 hash-chained audit trail, and every query runs a "
        "network audit proving nothing left the machine."
    )

with st.sidebar:
    st.header("Knowledge base")
    if st.button("Rebuild index from docs/"):
        with st.spinner("Embedding documents locally…"):
            try:
                st.success(f"Indexed {rag.build_index()} chunks.")
            except Exception as e:
                st.error(str(e))
    st.divider()
    ok, n = audit.verify()
    st.metric("Audit entries", n)
    st.write("Chain integrity:", "✅ intact" if ok else "❌ TAMPERED")

    st.divider()
    st.caption("Sandbox (Python REPL tool)")
    docker_up = bool(shutil.which("docker")) and subprocess.run(
        ["docker", "info"], capture_output=True, timeout=3
    ).returncode == 0
    st.write("Docker (network=none):", "✅ available" if docker_up else "⚠️ falling back to in-process eval")

tab_ask, tab_compare = st.tabs(["Ask AEGIS", "Document Comparison"])

with tab_ask:
    EXAMPLES = [
        "Pressure reading is 18.4 bar. Safe limit is 15 bar, critical limit is 18 bar. Is this a safety violation?",
        "What is the pressure alarm setpoint for the column top on the crude unit?",
        "What is the response if column top pressure exceeds 2.0 bar(g)?",
        "Compare wall thickness: 5mm measured, 6.35mm minimum. Is this acceptable?",
    ]
    st.caption("Try an example:")
    cols = st.columns(len(EXAMPLES))
    for col, ex in zip(cols, EXAMPLES):
        if col.button(ex[:40] + ("…" if len(ex) > 40 else ""), key=f"ex_{ex[:20]}"):
            st.session_state["query_input"] = ex

    query = st.text_area(
        "Ask about SOPs, equipment, or a gauge reading", height=100, key="query_input"
    )
    img = st.file_uploader("Attach equipment/gauge photo (optional)", type=["png", "jpg", "jpeg"])

    if st.button("Run AEGIS Analysis", type="primary") and query.strip():
        image_path = None
        if img:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(img.name)[1])
            tmp.write(img.read())
            tmp.close()
            image_path = tmp.name
            st.image(img, width=320)

        st.caption("AEGIS is processing — all computation on-premise…")
        progress_slots = {step: st.empty() for step in PIPELINE_STEPS}
        for step in PIPELINE_STEPS:
            progress_slots[step].markdown(f"⏳ {STEP_LABELS[step]}")

        result = None
        step_times = {}
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
                    progress_slots[node_name].markdown(f"✅ {STEP_LABELS[node_name]} ({step_times[node_name]}s)")
        except RuntimeError as e:
            st.error(str(e))
            result = None

        if result:
            st.session_state["result"] = result
            st.session_state["step_times"] = step_times
            st.session_state["signed_off"] = False

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
            entries = audit.read()[-20:]
            for i, e in enumerate(entries):
                arrow = "  │\n  ▼\n" if i > 0 else ""
                st.text(f"{arrow}[{e['event']}] {e['hash'][:12]}… (prev {e['prev_hash'][:12]}…)")

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
                d = differ.diff_documents(old_tmp.name, new_tmp.name)
                summary = differ.diff_report(old_tmp.name, new_tmp.name)
                st.session_state["diff_result"] = d
                st.session_state["diff_summary"] = summary
            except RuntimeError as e:
                st.error(str(e))

    d = st.session_state.get("diff_result")
    if d:
        st.subheader("Safety impact assessment")
        st.markdown(st.session_state.get("diff_summary", ""))

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
        if not (d["added"] or d["removed"] or d["modified"]):
            st.success("No differences detected.")

st.divider()
st.caption("Built for SIH 2026 · Problem Statement SIH26117 · Team Alpha")

"""Aegis — Streamlit demo for the air-gapped refinery agent."""
import os
import shutil
import subprocess
import tempfile

import streamlit as st

from core import agent, audit, rag

st.set_page_config(page_title="Aegis — Refinery AI", page_icon="🛡️", layout="wide")
st.title("🛡️ Aegis")
st.caption("Air-gapped industrial AI assistant for oil refineries · fully offline · tamper-evident audit · SIH26117")

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

query = st.text_area("Ask about SOPs, equipment, or a gauge reading", height=100)
img = st.file_uploader("Attach equipment/gauge photo (optional)", type=["png", "jpg", "jpeg"])

if st.button("Run", type="primary") and query.strip():
    image_path = None
    if img:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(img.name)[1])
        tmp.write(img.read())
        tmp.close()
        image_path = tmp.name
        st.image(img, width=320)
    with st.spinner("Reasoning locally (Plan → Vision → RAG → Calc → Reflect → Answer)…"):
        try:
            result = agent.run(query, image_path)
        except RuntimeError as e:
            st.error(str(e))
            result = None
    if result:
        st.session_state["result"] = result
        st.session_state["signed_off"] = False

result = st.session_state.get("result")
if result:
    st.subheader("Answer")
    st.markdown(result.get("answer", "_no answer_"))

    if result.get("requires_approval"):
        if st.session_state.get("signed_off"):
            st.success(f"✅ Signed off by {st.session_state['approver']}")
        else:
            st.warning("⚠️ Reflect flagged this as safety-critical — requires supervisor sign-off before acting on it.")
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

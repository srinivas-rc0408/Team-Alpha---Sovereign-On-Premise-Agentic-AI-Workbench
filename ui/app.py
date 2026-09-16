"""Aegis — Streamlit console for the air-gapped refinery agent.

Presentation only. Every verdict, limit and citation rendered here comes from
`core/` already decided; this file never recomputes a safety judgment, and the
structured verdict card reads `result["safety"]` / `result["safety_input"]`
rather than parsing the answer prose.
"""
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile

# `streamlit run ui/app.py` puts this file's own directory on sys.path, not the
# project root, so the `core` package import below fails unless we add it back.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import streamlit.components.v1 as components

from core import agent, audit, doc_diff, history, offline_check, rag

# initial_sidebar_state is pinned open, not left to "auto": on Streamlit 1.63 a
# collapsed sidebar never mounts its expand control (verified with all custom CSS
# disabled, at every hover position), so a collapse strands chat history, the
# privacy audit and the index rebuild until the page is reloaded. Opening
# expanded means every launch and every refresh recovers it.
st.set_page_config(page_title="Aegis — Refinery AI", page_icon="🛡️", layout="wide",
                   initial_sidebar_state="expanded")

# Streamlit's built-in "connection lost" dialog reads: "Streamlit server is not
# responding. Are you connected to the internet?" — which is exactly backwards
# for an air-gapped product: losing the connection means the LOCAL server was
# stopped (window closed / Ctrl+C), never an internet problem, and telling an
# operator to check their internet undermines the whole guarantee. That text is
# baked into Streamlit's frontend with no Python setting, so we rewrite it in the
# DOM. The observer is installed from a 0-height helper iframe (same-origin, so it
# can reach window.parent) and keeps running after the socket drops, so it still
# catches the dialog when it appears. Guarded so repeated reruns install it once.
_CONNECTION_MESSAGE_FIX = """
<script>
(function () {
  try {
    var doc = window.parent.document;
    if (!doc || doc.__aegisConnFix) return;
    doc.__aegisConnFix = true;
    var MAP = [
      ["Streamlit server is not responding. Are you connected to the internet?",
       "The AEGIS server isn't running. Restart it with run.bat (Windows) or ./run.sh — AEGIS never uses the internet."],
      ["Are you connected to the internet?",
       "AEGIS runs 100% offline, so this is never an internet problem."],
      ["Streamlit server is not responding.",
       "The AEGIS server isn't running \\u2014 restart it with run.bat or ./run.sh."],
      ["Connection error", "AEGIS server offline"]
    ];
    function fixNode(node) {
      if (node.nodeType === 3) {
        var v = node.nodeValue;
        if (!v) return;
        for (var i = 0; i < MAP.length; i++) {
          if (v.indexOf(MAP[i][0]) !== -1) { v = v.split(MAP[i][0]).join(MAP[i][1]); }
        }
        if (v !== node.nodeValue) node.nodeValue = v;
      } else if (node.nodeType === 1) {
        var tw = doc.createTreeWalker(node, NodeFilter.SHOW_TEXT, null, false);
        var t; while ((t = tw.nextNode())) fixNode(t);
      }
    }
    fixNode(doc.body);
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type === 'characterData') fixNode(m.target);
        for (var j = 0; j < m.addedNodes.length; j++) fixNode(m.addedNodes[j]);
      }
    }).observe(doc.body, { childList: true, subtree: true, characterData: true });
  } catch (e) { /* can't reach parent doc: leave Streamlit's default text */ }
})();
</script>
"""
components.html(_CONNECTION_MESSAGE_FIX, height=0)

# ────────────────────────────── design tokens ──────────────────────────────
# Mirrored in .streamlit/config.toml [theme] so Streamlit's own internals
# (uploader chrome, tooltips, select menus) match natively instead of being
# overridden here one selector at a time.
BG, BG_SIDE, SURFACE, BORDER = "#000000", "#0A0A0A", "#111111", "#1A1A1A"
ACCENT, ACCENT_2 = "#00D4AA", "#0070C0"
TEXT, TEXT_DIM = "#FFFFFF", "#888888"
OK, WARN, CRIT = "#00FF88", "#FFB800", "#FF4444"

# Per-step colours, shared by the live tracker, the timing bar and the audit
# trail badges so one step reads as one colour everywhere in the console.
# Answer is green, not white: a white segment on a near-black track reads as
# unfilled bar rather than as the longest stage of the run.
STEP_COLORS = {
    "plan": "#0070C0", "vision": "#A855F7", "rag": "#3DDCFF", "calc": "#FF8A3D",
    "safety_check": "#00D4AA", "reflect": "#FFB800", "answer": "#00FF88",
    "bump_loop": "#555555",
}

CSS = f"""
<style>
:root {{
  --bg: {BG}; --bg-side: {BG_SIDE}; --surface: {SURFACE}; --border: {BORDER};
  --accent: {ACCENT}; --accent-2: {ACCENT_2};
  --text: {TEXT}; --text-dim: {TEXT_DIM};
  --ok: {OK}; --warn: {WARN}; --crit: {CRIT};
  --font: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', Roboto, sans-serif;
  --mono: 'JetBrains Mono', 'Fira Code', 'SF Mono', ui-monospace, Menlo, monospace;
  --ease: cubic-bezier(.22,.61,.36,1);
}}

/* ── strip Streamlit chrome ──
   The header is emptied, never hidden: stExpandSidebarButton lives inside it, so
   `visibility:hidden` on the header leaves a collapsed sidebar with no way to
   reopen it — history, privacy audit and the index rebuild all become
   unreachable until a page reload. Hide the chrome, keep the control. */
#MainMenu, [data-testid="stAppDeployButton"], [data-testid="stToolbar"],
[data-testid="stDecoration"], footer {{ display: none !important; }}
/* The header keeps its height and its pointer events; only its chrome is hidden
   above. Both matter: zeroing the height collapses its flex children to 0x0, and
   pointer-events:none stops the header hover that is what makes Streamlit render
   the expand-sidebar control at all. Get either wrong and a collapsed sidebar
   can never be reopened — history, privacy audit and index rebuild all stranded
   behind a reload. It sits over empty space at the top of the page. */
header[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stExpandSidebarButton"] {{ visibility: visible; }}

/* The sidebar is permanent on desktop.
   Streamlit 1.63 collapses it with translateX(-300px) and then never mounts its
   expand control — verified with all of this CSS disabled, at every hover
   position, and the collapsed state persists across reloads. That strands chat
   history, the privacy audit and the index rebuild with no way back. Repinning
   the real toggle fails too: it lives inside nested transformed/clipped boxes.
   So the collapse affordance is removed where the trap applies. The console is
   docked at desk width and every control in that panel is part of the job, so a
   permanent panel costs nothing. Below 769px Streamlit overlays the sidebar and
   manages it itself, so its own controls are left alone there. */
@media (min-width: 769px) {{
  [data-testid="stSidebarCollapseButton"] {{ display: none !important; }}
}}
[data-testid="stExpandSidebarButton"] button,
[data-testid="stSidebarCollapseButton"] button {{
  color: var(--text-dim); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px;
}}
[data-testid="stExpandSidebarButton"] button:hover,
[data-testid="stSidebarCollapseButton"] button:hover {{
  color: var(--accent); border-color: var(--accent); background: #00D4AA0D;
}}

html, body, [data-testid="stApp"] {{ background: var(--bg); }}
/* stIconMaterial is excluded deliberately: Streamlit renders its chevrons and
   upload glyphs as Material Symbols ligatures, so forcing a text font onto them
   prints the literal ligature name ("keyboard_arrow_right") on screen. */
[data-testid="stApp"], [data-testid="stApp"] p, [data-testid="stApp"] li,
[data-testid="stApp"] label,
[data-testid="stApp"] span:not([data-testid="stIconMaterial"]) {{ font-family: var(--font); }}

[data-testid="stMainBlockContainer"] {{
  max-width: 900px; padding: 2.5rem 1rem 4rem;
  animation: fadeUp .3s var(--ease) both;
}}
@keyframes fadeUp {{ from {{ opacity: 0; transform: translateY(6px); }} to {{ opacity: 1; transform: none; }} }}

/* Streamlit stacks every element in a flex column with a fixed gap; the default
   is too tight for a 900px reading column, so the rhythm is set once here. */
[data-testid="stMain"] [data-testid="stVerticalBlock"] {{ gap: 1rem; }}

/* ── sidebar ── */
[data-testid="stSidebar"] {{ background: var(--bg-side); border-right: 1px solid var(--border); }}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: .55rem; }}
[data-testid="stSidebarUserContent"] {{ padding-top: 1.75rem; }}
[data-testid="stSidebar"] * {{ color: var(--text); }}
[data-testid="stSidebar"] hr {{ border-color: var(--border); margin: 1.25rem 0; }}

.sb-label {{
  font-size: .68rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
  color: var(--text-dim); margin: .25rem 0 .4rem;
}}

/* ── typography ── */
.hero {{ text-align: center; padding: .5rem 0 0; }}
.hero-mark {{ display: inline-flex; align-items: center; gap: .7rem; color: var(--text); }}
.hero-mark svg {{ width: 34px; height: 34px; color: var(--accent); }}
.hero h1 {{
  font-size: 2.5rem; font-weight: 700; letter-spacing: .15em; margin: 0;
  color: var(--text); line-height: 1;
}}
.hero .sub {{
  font-size: .85rem; font-weight: 400; color: var(--text-dim); margin: .85rem 0 0;
  letter-spacing: .02em;
}}
.sec-head {{
  font-size: .72rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
  color: var(--text-dim); margin: .25rem 0 .1rem;
}}
[data-testid="stMain"] p, [data-testid="stMain"] li {{ font-size: .95rem; line-height: 1.6; }}
code, kbd, .mono {{ font-family: var(--mono) !important; font-size: .82rem; }}

/* ── cards ── */
.card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 1.5rem;
}}

/* ── status pills ── */
.pill {{
  display: inline-flex; align-items: center; gap: .45rem;
  padding: .3rem .7rem; border-radius: 999px;
  font-size: .75rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
  border: 1px solid transparent; white-space: nowrap;
}}
.pill svg {{ width: 13px; height: 13px; }}
.pill.ok   {{ background: #00FF8814; color: var(--ok);   border-color: #00FF8833; }}
.pill.warn {{ background: #FFB80014; color: var(--warn); border-color: #FFB80033; }}
.pill.crit {{
  background: #FF444414; color: var(--crit); border-color: #FF444433;
  animation: critPulse 2.4s ease-in-out infinite;
}}
@keyframes critPulse {{
  0%, 100% {{ box-shadow: 0 0 0 0 #FF444400; }}
  50%      {{ box-shadow: 0 0 14px 1px #FF444433; }}
}}
.dot {{ width: 7px; height: 7px; border-radius: 50%; flex: 0 0 auto; }}

/* ── offline badge ── */
.offline {{
  display: flex; align-items: center; gap: .55rem;
  background: #0D1F17; border: 1px solid #00FF8833; border-radius: 10px;
  padding: .6rem .75rem; font-size: .78rem; font-weight: 600; color: var(--ok);
}}
.offline .dot {{ background: var(--ok); box-shadow: 0 0 6px var(--ok); }}
.offline.leak {{ background: #1F0D0D; border-color: #FF444455; color: var(--crit); }}
.offline.leak .dot {{ background: var(--crit); box-shadow: 0 0 6px var(--crit); }}
.sb-note {{ font-size: .72rem; color: var(--text-dim); margin: .5rem 0 .55rem; line-height: 1.45; }}
/* Streamlit gives a markdown block no outer margin, so sidebar rhythm comes from
   the note's own margins plus the vertical-block gap — not from empty spacers. */
[data-testid="stSidebar"] [data-testid="stMarkdown"] {{ margin-bottom: 0; }}

/* ── pipeline diagram (how it works) ── */
.pipe {{ display: flex; align-items: center; flex-wrap: wrap; gap: .35rem; margin: .25rem 0 1rem; }}
.pipe .chip {{
  display: inline-flex; align-items: center; gap: .4rem;
  background: #0D0D0D; border: 1px solid var(--border); border-radius: 8px;
  padding: .35rem .6rem; font-size: .76rem; color: var(--text-dim);
}}
.pipe .chip svg {{ width: 13px; height: 13px; }}
.pipe .arrow {{ color: #333; flex: 0 0 auto; display: inline-flex; }}
.pipe .arrow svg {{ width: 13px; height: 13px; }}

/* ── live run tracker ── */
.track {{
  display: flex; align-items: flex-start; gap: 0; margin: .25rem 0 .5rem;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 1.15rem 1.25rem .95rem;
}}
.track .step {{ flex: 1 1 0; text-align: center; position: relative; min-width: 0; }}
/* Connector sits behind the dots, drawn from each step to the previous one. */
.track .step:not(:first-child)::before {{
  content: ""; position: absolute; top: 9px; right: 50%; left: -50%;
  height: 1px; background: var(--border);
}}
.track .step.done::before {{ background: #00FF8855; }}
.track .step .node {{
  position: relative; z-index: 1; width: 19px; height: 19px; margin: 0 auto;
  border-radius: 50%; display: flex; align-items: center; justify-content: center;
  background: var(--surface); border: 1px solid #2A2A2A; color: transparent;
}}
.track .step .node svg {{ width: 11px; height: 11px; }}
.track .step.done .node {{
  background: #00FF881A; border-color: var(--ok); color: var(--ok);
  animation: pop .28s var(--ease);
}}
@keyframes pop {{ 0% {{ transform: scale(.55); }} 60% {{ transform: scale(1.18); }} 100% {{ transform: scale(1); }} }}
.track .step.active .node {{
  border-color: var(--accent); background: #00D4AA1A;
  animation: ping 1.1s ease-in-out infinite;
}}
@keyframes ping {{
  0%, 100% {{ box-shadow: 0 0 0 0 #00D4AA55; }}
  50%      {{ box-shadow: 0 0 0 5px #00D4AA00; }}
}}
.track .step.skip .node {{ border-style: dashed; border-color: #262626; }}
.track .step .name {{
  display: block; margin-top: .5rem; font-size: .68rem; letter-spacing: .04em;
  color: #666; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.track .step.done .name {{ color: var(--text-dim); }}
.track .step.active .name {{ color: var(--accent); font-weight: 600; }}
.track .step .t {{ display: block; font-size: .64rem; color: #4A4A4A; font-family: var(--mono); }}
.track-status {{
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: .78rem; color: var(--text-dim); margin: .1rem .15rem .6rem;
}}
.track-status .now {{ color: var(--accent); font-weight: 600; }}
.track-status .el {{ font-family: var(--mono); font-size: .75rem; color: #666; }}

/* ── timing bar ── */
.tbar {{ display: flex; height: 6px; border-radius: 3px; overflow: hidden; background: #0D0D0D; margin: .1rem 0 .5rem; }}
.tbar span {{ display: block; height: 100%; transition: opacity .2s var(--ease); }}
.tbar span:hover {{ opacity: .75; }}
.tlegend {{ display: flex; flex-wrap: wrap; gap: .1rem 1rem; font-size: .72rem; color: var(--text-dim); }}
.tlegend i {{ display: inline-block; width: 7px; height: 7px; border-radius: 2px; margin-right: .4rem; }}
.tlegend b {{ font-family: var(--mono); font-weight: 400; color: #666; }}
.ttotal {{ font-family: var(--mono); font-size: .8rem; color: var(--text); }}

/* ── verdict + field rows ── */
.verdict {{ display: flex; align-items: center; gap: .8rem; margin-bottom: 1.1rem; flex-wrap: wrap; }}
.rows {{ display: flex; flex-direction: column; }}
.row {{
  display: grid; grid-template-columns: 170px 1fr; gap: 1rem;
  padding: .7rem 0; border-top: 1px solid var(--border); font-size: .88rem;
}}
.row:first-child {{ border-top: 0; padding-top: 0; }}
.row .k {{ color: var(--text-dim); font-size: .78rem; letter-spacing: .03em; padding-top: .1rem; }}
.row .v {{
  color: var(--text); line-height: 1.55;
  display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
}}
.row .v .num {{ font-family: var(--mono); font-size: .84rem; }}
@media (max-width: 640px) {{ .row {{ grid-template-columns: 1fr; gap: .2rem; }} }}

/* ── audit chain ── */
.chain {{ position: relative; padding-left: 1.1rem; }}
.chain::before {{
  content: ""; position: absolute; left: 3px; top: .5rem; bottom: .5rem;
  width: 1px; background: var(--border);
}}
.chain .e {{ position: relative; padding: .4rem 0; font-size: .78rem; }}
.chain .e::before {{
  content: ""; position: absolute; left: -1.1rem; top: .78rem;
  width: 7px; height: 7px; border-radius: 50%; background: #2A2A2A;
  outline: 3px solid var(--bg);
}}
.chain .badge {{
  display: inline-block; padding: .1rem .45rem; border-radius: 5px;
  font-size: .68rem; font-weight: 600; letter-spacing: .04em; margin-right: .5rem;
}}
.chain .h {{ font-family: var(--mono); font-size: .72rem; color: #5A5A5A; }}

/* ── diff ── */
.diff {{
  border-radius: 8px; padding: .65rem .8rem; margin: .3rem 0;
  font-size: .86rem; line-height: 1.55; border: 1px solid transparent;
}}
.diff.add {{ background: #00FF881A; border-color: #00FF8833; color: #C8FFE4; }}
.diff.del {{ background: #FF44441A; border-color: #FF444433; color: #FFD2D2; }}
.diff.mod {{ background: #FFB8001A; border-color: #FFB80033; color: #FFE9BC; }}
.diff .lbl {{
  display: block; font-size: .66rem; letter-spacing: .1em; text-transform: uppercase;
  opacity: .65; margin-bottom: .2rem;
}}

/* ── buttons ──
   Descendant, never `.stButton > button`: a button carrying help= is wrapped in
   stTooltipHoverTarget spans, so the child combinator silently drops every
   tooltipped control (which here is most of them). */
[data-testid="stSidebar"] .stButton button,
[data-testid="stMain"] .stButton button {{
  font-family: var(--font); font-size: .85rem; font-weight: 500;
  border-radius: 8px; transition: all .2s var(--ease);
}}
.stButton button[kind="secondary"] {{
  background: transparent; color: var(--text-dim); border: 1px solid var(--border);
}}
.stButton button[kind="secondary"]:hover {{
  border-color: var(--accent); color: var(--accent); background: #00D4AA0D;
}}
.stButton button[kind="secondary"]:active {{ transform: translateY(0); }}
.stButton button[kind="primary"] {{
  background: var(--accent); color: #000; border: 1px solid var(--accent);
  font-weight: 600; padding: .75rem 2rem;
}}
.stButton button[kind="primary"]:hover {{
  background: #1AE5BE; border-color: #1AE5BE; transform: scale(1.005);
}}
.stButton button[kind="primary"]:disabled {{
  background: #0D3D33; border-color: #0D3D33; color: #4D8878; transform: none;
}}
.stButton button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

/* Example queries as cards: st.button is what makes them clickable, so the card
   is the button rather than a div wrapping one. The stretch chain equalises
   height across the row — queries differ in length and ragged cards read as a
   bug, not a rhythm. */
[class*="st-key-ex_"], [class*="st-key-ex_"] [data-testid="stElementContainer"],
[class*="st-key-ex_"] .stButton,
[class*="st-key-ex_"] .stTooltipIcon, [class*="st-key-ex_"] .stTooltipHoverTarget {{ height: 100%; }}
[data-testid="stHorizontalBlock"]:has([class*="st-key-ex_"]) {{ align-items: stretch; }}
[data-testid="stHorizontalBlock"]:has([class*="st-key-ex_"]) [data-testid="stColumn"] > div {{ height: 100%; }}
[class*="st-key-ex_"] .stButton button {{
  background: var(--surface); border: 1px solid var(--border); color: var(--text-dim);
  border-radius: 10px; padding: .85rem .9rem; height: 100%; min-height: 84px;
  font-size: .82rem; line-height: 1.45; text-align: left; white-space: normal;
  align-items: flex-start; justify-content: flex-start;
}}
[class*="st-key-ex_"] .stButton button:hover {{
  border-color: var(--accent); color: var(--text);
  box-shadow: 0 0 0 1px #00D4AA33, 0 6px 18px -8px #00D4AA66;
  transform: translateY(-1px);
}}
[class*="st-key-ex_"] .stButton button p {{ font-size: .82rem !important; }}

/* Chat history rows: delete reveals on hover, and on keyboard focus so it stays
   reachable without a pointer. */
[class*="st-key-chatrow_"] {{ border-radius: 8px; transition: background .2s var(--ease); }}
[class*="st-key-chatrow_"]:hover {{ background: #141414; }}
/* justify-content, not just text-align: Streamlit centres button content with
   flex, so text-align alone leaves the label centred at any sidebar width. */
[class*="st-key-chatrow_"] .stButton button {{
  background: transparent; border: 0; text-align: left; padding: .35rem .5rem;
  color: var(--text-dim); font-size: .8rem;
  justify-content: flex-start; min-height: 0;
}}
[class*="st-key-chatrow_"] .stButton button p {{
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;
}}
[class*="st-key-chatrow_"] .stButton button:hover {{ color: var(--text); background: transparent; }}
[class*="st-key-del_"] .stButton button {{
  opacity: 0; color: #5A5A5A; padding: .35rem .4rem; transition: opacity .2s var(--ease), color .2s var(--ease);
}}
[class*="st-key-chatrow_"]:hover [class*="st-key-del_"] .stButton button,
[class*="st-key-del_"] .stButton button:focus-visible {{ opacity: 1; }}
[class*="st-key-del_"] .stButton button:hover {{ color: var(--crit); }}
.chat-meta {{
  font-size: .68rem; color: #5A5A5A; font-family: var(--mono);
  padding: 0 .5rem .3rem; display: flex; align-items: center; gap: .4rem;
}}

/* ── inputs ── */
[data-testid="stTextArea"] textarea, [data-testid="stTextInput"] input {{
  background: var(--bg-side) !important; border: 1px solid var(--border) !important;
  border-radius: 10px !important; color: var(--text) !important;
  font-family: var(--font); font-size: .92rem; padding: 1rem !important;
  transition: border-color .2s var(--ease), box-shadow .2s var(--ease);
  resize: none !important;
}}
[data-testid="stTextArea"] textarea:focus, [data-testid="stTextInput"] input:focus {{
  border-color: var(--accent) !important; box-shadow: 0 0 0 3px #00D4AA1F !important;
}}
/* #777 not the #555 in the spec: placeholder is body text and an operator reads
   it under plant lighting, so it holds the 4.5:1 floor. */
[data-testid="stTextArea"] textarea::placeholder, [data-testid="stTextInput"] input::placeholder {{
  color: #777777 !important;
}}
[data-testid="stTextArea"] [data-testid="stWidgetLabel"] p,
[data-testid="stTextInput"] [data-testid="stWidgetLabel"] p,
[data-testid="stFileUploader"] [data-testid="stWidgetLabel"] p {{
  font-size: .78rem !important; color: var(--text-dim) !important; font-weight: 500;
}}
[data-testid="InputInstructions"] {{ display: none; }}

[data-testid="stFileUploaderDropzone"] {{
  background: var(--bg-side); border: 1px dashed #2A2A2A; border-radius: 10px;
  transition: border-color .2s var(--ease), background .2s var(--ease);
}}
[data-testid="stFileUploaderDropzone"]:hover {{ border-color: var(--accent); background: #00D4AA08; }}
[data-testid="stFileUploaderDropzone"] button {{
  background: transparent; border: 1px solid var(--border); color: var(--text-dim);
  border-radius: 7px;
}}
[data-testid="stFileUploaderDropzone"] button:hover {{ border-color: var(--accent); color: var(--accent); }}
[data-testid="stFileUploaderDropzoneInstructions"] span,
[data-testid="stFileUploaderDropzoneInstructions"] small {{ color: var(--text-dim); }}

/* ── tabs ── */
[data-testid="stTabs"] [data-baseweb="tab-list"] {{
  background: transparent; border-bottom: 1px solid var(--border); gap: 1.5rem;
}}
[data-testid="stTabs"] [data-baseweb="tab"] {{
  background: transparent; padding: .6rem 0; color: var(--text-dim);
  transition: color .2s var(--ease);
}}
[data-testid="stTabs"] [data-baseweb="tab"] p {{
  font-size: .85rem !important; font-weight: 500; letter-spacing: .02em;
}}
[data-testid="stTabs"] [data-baseweb="tab"]:hover {{ color: var(--text); }}
[data-testid="stTabs"] [aria-selected="true"] {{ color: var(--text) !important; }}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {{ background: var(--accent); height: 2px; }}
[data-testid="stTabs"] [data-baseweb="tab-border"] {{ display: none; }}

/* ── expanders ── */
[data-testid="stExpander"] {{
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  overflow: hidden;
}}
[data-testid="stExpander"] summary {{ padding: .7rem .9rem; transition: background .2s var(--ease); }}
[data-testid="stExpander"] summary:hover {{ background: #161616; }}
[data-testid="stExpander"] summary p {{ font-size: .82rem !important; color: var(--text-dim); }}
[data-testid="stExpander"] summary:hover p {{ color: var(--text); }}
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {{ padding: 0 .9rem .9rem; }}

/* ── alerts / metrics / misc ── */
[data-testid="stAlert"] {{ border-radius: 10px; border: 1px solid var(--border); background: var(--surface); }}
[data-testid="stAlert"] p {{ font-size: .86rem !important; }}
[data-testid="stMetric"] {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: .75rem .9rem;
}}
[data-testid="stMetricValue"] {{ font-size: 1.35rem; font-family: var(--mono); }}
[data-testid="stMetricLabel"] p {{ font-size: .72rem !important; color: var(--text-dim) !important; }}
[data-testid="stCaptionContainer"] p, [data-testid="stCaptionContainer"] {{
  color: var(--text-dim) !important; font-size: .76rem !important;
}}
[data-testid="stCode"], pre {{ background: #0D0D0D !important; border: 1px solid var(--border); border-radius: 8px; }}
[data-testid="stCode"] code, pre code {{ font-family: var(--mono) !important; font-size: .78rem !important; }}
[data-testid="stMain"] hr {{ border-color: var(--border); margin: 2rem 0 1.25rem; }}
[data-testid="stImage"] img {{ border-radius: 10px; border: 1px solid var(--border); }}
[data-testid="stSpinner"] i {{ border-top-color: var(--accent) !important; }}
::-webkit-scrollbar {{ width: 8px; height: 8px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: #222; border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: #333; }}

.foot {{
  text-align: center; font-size: .75rem; color: #444; padding: .5rem 0 0;
  letter-spacing: .02em;
}}

@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{ animation: none !important; transition-duration: .01ms !important; }}
}}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# ────────────────────────────────── icons ──────────────────────────────────
# Authored inline so the console stays air-gapped (no icon CDN) and every glyph
# shares one 24-grid and 1.75 stroke — emoji can't hold a consistent weight.
_ICON_PATHS = {
    "shield": '<path d="M12 3l7 3v5.5c0 4.3-2.9 8.2-7 9.5-4.1-1.3-7-5.2-7-9.5V6l7-3z"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7a4 4 0 018 0v3.5"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "trash": '<path d="M4 7h16M10 7V5h4v2M6 7l1 13h10l1-13"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    "alert": '<path d="M12 4l9 16H3l9-16z"/><path d="M12 10v4M12 17.5v.01"/>',
    "eye": '<path d="M2 12s3.8-6.5 10-6.5S22 12 22 12s-3.8 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.8"/>',
    "upload": '<path d="M12 16V4M7.5 8.5L12 4l4.5 4.5"/><path d="M4 16v2.5A1.5 1.5 0 005.5 20h13a1.5 1.5 0 001.5-1.5V16"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M10 2.5v3M14 2.5v3M10 18.5v3M14 18.5v3M2.5 10h3M2.5 14h3M18.5 10h3M18.5 14h3"/>',
    "database": '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6"/><path d="M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3"/>',
    "sigma": '<path d="M17.5 5.5H7l6 6.5-6 6.5h10.5"/>',
    "loop": '<path d="M4 9.5A6 6 0 0110 4h8"/><path d="M15 1.5L18.5 4 15 6.5"/><path d="M20 14.5A6 6 0 0114 20H6"/><path d="M9 22.5L5.5 20 9 17.5"/>',
    "message": '<path d="M20.5 12.5c0 4-3.8 7-8.5 7-1 0-2-.15-2.9-.4L4 21l1.6-3.6A6.6 6.6 0 013.5 12.5c0-4 3.8-7 8.5-7s8.5 3 8.5 7z"/>',
    "file": '<path d="M13.5 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8.5L13.5 3z"/><path d="M13.5 3v5.5H19"/>',
    "download": '<path d="M12 4v12M7.5 11.5L12 16l4.5-4.5"/><path d="M4 18v1.5A1.5 1.5 0 005.5 21h13a1.5 1.5 0 001.5-1.5V18"/>',
    "arrow": '<path d="M5 12h13M13.5 7l5 5-5 5"/>',
    "refresh": '<path d="M20 11.5A8 8 0 006.5 6L3 9"/><path d="M3 4.5V9h4.5"/><path d="M4 12.5A8 8 0 0017.5 18l3.5-3"/><path d="M21 19.5V15h-4.5"/>',
    "activity": '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
}


def icon(name: str, size: int = 16, color: str = "currentColor") -> str:
    """One 24-grid, one stroke weight. Returns an inline SVG string."""
    return (
        f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
        f'stroke="{color}" stroke-width="1.75" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{_ICON_PATHS.get(name, "")}</svg>'
    )


# ─────────────────────────────── constants ───────────────────────────────
PIPELINE_STEPS = ["plan", "vision", "rag", "calc", "safety_check", "reflect", "answer"]
STEP_LABELS = {
    "plan": "Plan", "vision": "Vision", "rag": "RAG", "calc": "Calc",
    "safety_check": "Safety", "reflect": "Reflect", "answer": "Answer",
    "bump_loop": "Retry",
}
STEP_ICONS = {
    "plan": "cpu", "vision": "eye", "rag": "database", "calc": "sigma",
    "safety_check": "shield", "reflect": "loop", "answer": "message",
}
# A node that returns {} contributed nothing to the answer; the tracker draws it
# hollow rather than claiming work that never happened.
STEP_EVIDENCE = {
    "plan": "plan", "vision": "vision", "rag": "context", "calc": "calc",
    "safety_check": "safety", "reflect": "reflection", "answer": "answer",
}

EXAMPLES = [
    "Pressure reading is 18.4 bar. Safe limit is 15 bar per SOP-402. Is this a violation?",
    "Vibration on pump P-201 is 8.5 mm/s RMS. Check against SOP limits.",
    "Wall thickness reading of 5.2mm vs minimum 6.35mm per Section 3.2",
]

# core/safety_rules.py's six verdicts, mapped to the three tones an operator
# needs to tell apart at a glance.
CRITICAL_VERDICTS = {"CRITICAL", "EXCEEDS", "BELOW_MINIMUM"}
WARNING_VERDICTS = {"CAUTION"}

MAX_QUERY_CHARS = 8000
RECENT_N = 4

if "session" not in st.session_state:
    st.session_state["session"] = history.new_session()


# ─────────────────────────────── renderers ───────────────────────────────
def tone_of(status: str) -> str:
    """Verdict → visual tone. Unknown verdicts read as caution, never as safe."""
    if status in CRITICAL_VERDICTS:
        return "crit"
    if status in WARNING_VERDICTS:
        return "warn"
    if status in ("NORMAL", "OK"):
        return "ok"
    return "warn"


def pill(text: str, tone: str, icon_name: str = None) -> str:
    glyph = icon(icon_name, 13) if icon_name else ""
    return f'<span class="pill {tone}">{glyph}{html.escape(text)}</span>'


def _fmt(value) -> str:
    """Trim the float noise (18.400000000000002) without hiding real precision."""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def render_tracker(placeholder, done: dict, active: str, elapsed: float, skipped: set):
    """The live run tracker — the one authored moment in the console.

    A query takes ~15-60s on refinery hardware. A spinner says only "wait"; this
    says which stage is running, what already finished, and how long each took,
    so an operator (or a judge at a demo) can watch the reasoning proceed.
    """
    cells = []
    for step in PIPELINE_STEPS:
        if step in done:
            state, glyph = ("skip", "") if step in skipped else ("done", icon("check", 11))
            secs = f'<span class="t">{done[step]:g}s</span>' if step not in skipped else '<span class="t">skipped</span>'
        elif step == active:
            state, glyph, secs = "active", "", '<span class="t">running</span>'
        else:
            state, glyph, secs = "", "", '<span class="t">&nbsp;</span>'
        cells.append(
            f'<div class="step {state}"><div class="node">{glyph}</div>'
            f'<span class="name">{STEP_LABELS[step]}</span>{secs}</div>'
        )
    # "Running · Answer" not bare "Answer": a lone step name in accent colour
    # reads as a section heading for the block below it, not as live status.
    label = f"Running · {STEP_LABELS.get(active, 'Finishing')}" if active else "Complete"
    placeholder.markdown(
        f'<div class="track-status"><span class="now">{html.escape(label)}</span>'
        f'<span class="el">{elapsed:.1f}s elapsed</span></div>'
        f'<div class="track">{"".join(cells)}</div>',
        unsafe_allow_html=True,
    )


def render_timing(times: dict):
    """Proportional bar + legend. A skipped step keeps a visible sliver so the
    operator can see it was considered and not silently dropped."""
    steps = [(k, v) for k, v in times.items() if k != "total" and k in STEP_LABELS]
    if not steps:
        return
    total = sum(v for _, v in steps) or 1
    segs, legend = [], []
    for name, secs in steps:
        color = STEP_COLORS.get(name, "#555")
        width = max((secs / total) * 100, 0.8)
        # A step that took no measurable time was skipped by the plan, not run in
        # 0s — saying "skipped" is the honest label and reads faster than "0s".
        shown = f"{secs:g}s" if secs >= 0.05 else "skipped"
        opacity = "1" if secs >= 0.05 else ".3"
        segs.append(
            f'<span title="{html.escape(f"{STEP_LABELS.get(name, name)} · {shown}")}" '
            f'style="width:{width:.2f}%;background:{color};opacity:{opacity}"></span>'
        )
        legend.append(
            f'<span><i style="background:{color};opacity:{opacity}"></i>'
            f'{STEP_LABELS.get(name, name)} <b>{shown}</b></span>'
        )
    st.markdown(
        f'<div class="ttotal">{times.get("total", 0):g}s total</div>'
        f'<div class="tbar">{"".join(segs)}</div>'
        f'<div class="tlegend">{"".join(legend)}</div>',
        unsafe_allow_html=True,
    )


_VERDICT_LINE = re.compile(r"^\*\*[A-Z_]+\*\* — .*?\n\n", re.DOTALL)


def answer_prose(result: dict) -> str:
    """core/agent.py prefixes safety answers with a code-rendered verdict line so
    the text is self-contained in exports and history. The console renders that
    verdict as a structured card from the same data, so the line is stripped here
    to avoid saying it twice — never by re-deriving it, only by removing it."""
    text = result.get("answer", "") or "_no answer_"
    if result.get("safety"):
        return _VERDICT_LINE.sub("", text, count=1).strip() or text
    return text


def render_verdict(result: dict):
    """Structured verdict card, read from the rule engine's own output.

    Every value here is `result["safety"]` / `result["safety_input"]` verbatim —
    the same numbers the deterministic comparison used. Nothing is parsed back
    out of the answer text, so the card cannot drift from the verdict.
    """
    safety = result.get("safety")
    if not safety:
        return
    si = result.get("safety_input") or {}
    status = safety["status"]
    tone = tone_of(status)
    glyph = {"crit": "alert", "warn": "alert", "ok": "check"}[tone]

    rows = [("Status", pill(status, tone, glyph) + ' <span style="color:#666;font-size:.78rem">rule engine, not an LLM judgment</span>')]

    if si.get("reading") is not None:
        limits = [f'safe {_fmt(si.get("safe_limit"))}']
        if si.get("critical_limit") is not None:
            limits.append(f'critical {_fmt(si["critical_limit"])}')
        rows.append((
            (si.get("type") or "reading").replace("_", " ").title(),
            f'<span class="num">{_fmt(si["reading"])}</span>'
            f'<span style="color:#666"> measured · {" / ".join(limits)}</span>',
        ))
    if si.get("source"):
        rows.append(("Standard reference", f'<span style="color:#BBB">{html.escape(str(si["source"]))}</span>'))
    rows.append(("Required action", html.escape(safety["action"])))
    if safety.get("overage") is not None:
        over = safety["overage"]
        word = "over the safe limit" if over > 0 else "within the safe limit"
        rows.append(("Margin", f'<span class="num">{_fmt(abs(over))}</span> <span style="color:#666">{word}</span>'))
    rows.append((
        "Supervisor sign-off",
        pill("Required", "warn", "lock") if result.get("requires_approval")
        else '<span style="color:#666">Not required</span>',
    ))

    body = "".join(
        f'<div class="row"><div class="k">{html.escape(k)}</div><div class="v">{v}</div></div>'
        for k, v in rows
    )
    st.markdown(f'<div class="card"><div class="rows">{body}</div></div>', unsafe_allow_html=True)


def _load_session(chat_id: str):
    """Restore a past session: its last run repopulates the report panes."""
    session = history.load_session(chat_id)
    if not session:
        st.toast(f"Chat {chat_id} is no longer on disk.")
        return
    st.session_state["session"] = session
    last = session["entries"][-1] if session["entries"] else None
    if last:
        steps = last.get("steps") or {}
        st.session_state["result"] = {
            "query": last["query"],
            "answer": last["answer"],
            "safety": last.get("safety"),
            # Persisted alongside the verdict so a restored run renders the same
            # limits and citation it was decided against, not just the status.
            "safety_input": steps.get("safety_input"),
            "requires_approval": last.get("requires_approval", False),
            "trace": last.get("trace", []),
            "loop_count": last.get("loop_count", 0),
            "network_audit": last.get("network_audit"),
            "context": steps.get("context"),
            "image_path": last.get("image_path"),
        }
        st.session_state["step_times"] = last.get("timing", {})
        st.session_state["restored_from"] = session["title"]
    st.session_state["signed_off"] = False


@st.cache_data(ttl=30, show_spinner=False)
def _docker_available() -> bool:
    """Is a Docker daemon reachable for the sandboxed calculator? Cached for 30s
    because it shells out to `docker info`, and re-running that on every Streamlit
    rerun (every click and keystroke) adds latency for a status that rarely
    changes — and can block up to the timeout when the daemon is installed but
    down. The calc tool falls back to a restricted in-process evaluator either way."""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=3).returncode == 0
    except Exception:
        return False


# ─────────────────────────────── sidebar ───────────────────────────────
with st.sidebar:
    offline = offline_check.verify_offline()
    note = (
        f'{offline["queries_audited"]} past queries network-audited · all clean'
        if offline["checks"]["history"]["ok"]
        else f'{offline["checks"]["history"]["leaking_queries"]} past queries leaked'
    )
    if offline["offline"]:
        badge = (
            f'<div class="offline"><span class="dot"></span>{icon("lock", 13)}'
            f'<span>OFFLINE · {len(offline["external_connections"])} external</span></div>'
        )
    else:
        badge = (
            f'<div class="offline leak"><span class="dot"></span>{icon("alert", 13)}'
            f'<span>{len(offline["external_connections"])} EXTERNAL CONNECTION(S)</span></div>'
        )
    warnings = "".join(
        f'<p class="sb-note">⚠ {html.escape(w)}</p>' for w in offline.get("warnings", [])
    )
    # Badge and note ship as one markdown block: as separate blocks the badge
    # wrapped to two lines inside a container sized for one, and the note below
    # overlapped it by 7px. One block, one flow, no overlap to tune.
    st.markdown(
        f'{badge}<p class="sb-note">{html.escape(note)}</p>{warnings}',
        unsafe_allow_html=True,
    )

    if st.button("Privacy self-audit", use_container_width=True, key="privacy_btn",
                 help="Re-run all four offline checks now and show the raw network log"):
        st.session_state["privacy_audit"] = offline_check.verify_offline()

    audit_result = st.session_state.get("privacy_audit")
    if audit_result:
        with st.expander("Self-audit result", expanded=True):
            if audit_result["offline"]:
                st.markdown(pill("No data left this machine", "ok", "check"), unsafe_allow_html=True)
            else:
                st.markdown(pill("External connection detected", "crit", "alert"), unsafe_allow_html=True)

            for name, check in audit_result["checks"].items():
                tone = "ok" if check["ok"] else "crit"
                st.markdown(
                    f'<div style="display:flex;gap:.5rem;align-items:center;padding:.2rem 0;font-size:.8rem">'
                    f'<span class="dot" style="background:var(--{tone if tone == "ok" else "crit"})"></span>'
                    f'{html.escape(name)}</div>',
                    unsafe_allow_html=True,
                )

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
            st.caption("Endpoints: " + ", ".join(
                f"{k}={v['value']}" for k, v in audit_result["checks"]["endpoints"]["endpoints"].items()
            ))

    st.divider()
    st.markdown(f'<div class="sb-label">{icon("message", 12)} Chat history</div>', unsafe_allow_html=True)

    if st.button("New chat", use_container_width=True, key="new_chat"):
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
    keyword = st.text_input("Search history", placeholder="pressure, P-201, SOP-402…",
                            key="history_search", label_visibility="collapsed")
    chats = history.search_sessions(keyword) if keyword.strip() else history.list_sessions()

    total = history.session_count()
    count_note = f"{total} conversation{'s' if total != 1 else ''} stored locally"
    if keyword.strip():
        count_note += f" · {len(chats)} matching"
    st.markdown(f'<p class="sb-note">{count_note}</p>', unsafe_allow_html=True)

    current_id = st.session_state["session"]["id"]
    if not chats:
        st.markdown(
            f'<p class="sb-note">'
            f'{"No chats match that search." if keyword.strip() else "No saved chats yet — run a query to start one."}'
            f"</p>",
            unsafe_allow_html=True,
        )

    def _render_chat_row(chat):
        tone = {"CRITICAL": "crit", "WARNING": "warn"}.get(chat.get("status", "SAFE"), "ok")
        color = {"crit": CRIT, "warn": WARN, "ok": OK}[tone]
        # Titles can repeat (the same question asked in two chats), so the timestamp
        # is what tells rows apart — shown on its own line under the title.
        ts = chat["updated_at"][:16].replace("T", " ")
        with st.container(key=f"chatrow_{chat['id']}"):
            row, delete_col = st.columns([6, 1], gap="small")
            marker = "▸ " if chat["id"] == current_id else ""
            if row.button(f"{marker}{chat['title'][:34]}", key=f"chat_{chat['id']}",
                          use_container_width=True, help=chat.get("preview") or chat["title"]):
                _load_session(chat["id"])
                st.rerun()
            with delete_col.container(key=f"del_{chat['id']}"):
                if st.button("✕", key=f"delbtn_{chat['id']}", help="Delete this chat"):
                    history.delete_session(chat["id"])
                    if chat["id"] == current_id:
                        st.session_state["session"] = history.new_session()
                        for key in ("result", "step_times", "signed_off", "restored_from"):
                            st.session_state.pop(key, None)
                    st.rerun()
            st.markdown(
                f'<div class="chat-meta"><span class="dot" style="background:{color}"></span>'
                f'{html.escape(ts)} · {chat["entry_count"]} run(s)</div>',
                unsafe_allow_html=True,
            )

    if keyword.strip():
        # Search shows every match, scrollable so a long result set can't push the
        # rest of the sidebar off-screen.
        with st.container(height=340 if len(chats) > RECENT_N else "content"):
            for chat in chats[:200]:
                _render_chat_row(chat)
    else:
        # Default view: only the few most recent (newest first), so the sidebar
        # stays legible. The rest live behind an expander, not lost.
        for chat in chats[:RECENT_N]:
            _render_chat_row(chat)
        if len(chats) > RECENT_N:
            with st.expander(f"All {len(chats)} conversations"):
                with st.container(height=340):
                    for chat in chats[RECENT_N:200]:
                        _render_chat_row(chat)

    st.divider()
    st.markdown(f'<div class="sb-label">{icon("database", 12)} Knowledge base</div>', unsafe_allow_html=True)
    if st.button("Rebuild index from docs/", use_container_width=True, key="rebuild"):
        with st.spinner("Embedding documents locally…"):
            try:
                st.success(f"Indexed {rag.build_index()} chunks.")
            except Exception as e:
                st.error(agent.friendly_error(e))

    st.divider()
    st.markdown(f'<div class="sb-label">{icon("activity", 12)} System health</div>', unsafe_allow_html=True)
    ok_chain, n_entries = audit.verify()
    docker_ok = _docker_available()
    health = [
        ("Audit entries", f"{n_entries}", "ok"),
        ("Chain integrity", "Intact" if ok_chain else "TAMPERED", "ok" if ok_chain else "crit"),
        ("Docker sandbox", "Available" if docker_ok else "In-process fallback", "ok" if docker_ok else "warn"),
    ]
    color_map = {"ok": OK, "warn": WARN, "crit": CRIT}
    st.markdown(
        '<div style="display:flex;flex-direction:column;gap:.45rem;margin-top:.2rem">'
        + "".join(
            f'<div style="display:flex;justify-content:space-between;align-items:center;font-size:.78rem">'
            f'<span style="color:var(--text-dim)">{html.escape(k)}</span>'
            f'<span style="display:flex;align-items:center;gap:.4rem">'
            f'<span class="dot" style="background:{color_map[t]}"></span>{html.escape(v)}</span></div>'
            for k, v, t in health
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<p class="sb-note" style="margin-top:.7rem">Chats stored in '
        f'<code style="color:#777">{html.escape(str(history.chats_dir()))}</code></p>',
        unsafe_allow_html=True,
    )

# ─────────────────────────────── main ───────────────────────────────
st.markdown(
    f'<div class="hero"><div class="hero-mark">{icon("shield", 34)}<h1>AEGIS</h1></div>'
    f'<p class="sub">Team Alpha &nbsp;·&nbsp; SIH26117 &nbsp;·&nbsp; 100% Air-Gapped '
    f'&nbsp;·&nbsp; Engineering Intelligence System</p></div>',
    unsafe_allow_html=True,
)

with st.expander("How AEGIS works"):
    chips = []
    for i, step in enumerate(PIPELINE_STEPS):
        if i:
            chips.append(f'<span class="arrow">{icon("arrow", 13)}</span>')
        chips.append(
            f'<span class="chip">{icon(STEP_ICONS[step], 13)}{STEP_LABELS[step]}</span>'
        )
    st.markdown(f'<div class="pipe">{"".join(chips)}</div>', unsafe_allow_html=True)
    st.markdown(
        "- **Plan** — the LLM decides which steps this query actually needs.\n"
        "- **Vision** — Qwen2.5-VL reads gauge displays and P&ID/drawing text in an attached photo (skipped if none).\n"
        "- **RAG** — hybrid FAISS + BM25 search over your indexed SOPs.\n"
        "- **Calc** — arithmetic runs in a Docker sandbox (`network=none`), never in the LLM's head.\n"
        "- **Safety** — deterministic code (`core/safety_rules.py`), not an LLM judgment. Limits in "
        "`config/safety_limits.json` override anything the LLM extracted.\n"
        "- **Reflect** — self-critiques the evidence; can loop back to RAG for another pass (capped at 2).\n"
        "- **Answer** — the final response, with three independent gates on the sign-off requirement.\n"
        "\nEvery step is logged to a SHA-256 hash-chained audit trail, every query runs a "
        "network audit proving nothing left the machine, and every run is saved locally to "
        "`data/chats/`."
    )

tab_ask, tab_compare = st.tabs(["Ask AEGIS", "Document Comparison"])

with tab_ask:
    if st.session_state.get("restored_from"):
        st.markdown(
            f'<p class="sb-note" style="display:flex;align-items:center;gap:.5rem;margin-bottom:.2rem">'
            f'{icon("clock", 13)} Restored from history: '
            f'<strong style="color:#BBB">{html.escape(st.session_state["restored_from"])}</strong></p>',
            unsafe_allow_html=True,
        )

    st.markdown('<div class="sec-head">Try an example</div>', unsafe_allow_html=True)
    for i, (col, ex) in enumerate(zip(st.columns(len(EXAMPLES), gap="small"), EXAMPLES)):
        with col.container(key=f"ex_{i}"):
            if st.button(ex, key=f"exbtn_{i}", help=ex, use_container_width=True):
                st.session_state["query_input"] = ex

    query = st.text_area(
        "Ask about SOPs, equipment, or a gauge reading", height=110, key="query_input",
        placeholder="Pressure on V-101 reads 18.4 bar — is that a violation?",
    )
    img = st.file_uploader("Attach equipment or gauge photo (optional)", type=["png", "jpg", "jpeg"])

    run_clicked = st.button("Run AEGIS Analysis", type="primary", use_container_width=True, key="run")

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
                st.image(img, width=300)
            except Exception as e:
                # A corrupt or unreadable upload must not sink the text query.
                st.warning(f"Could not read that image ({e}) — continuing without it.")
                image_path = None

        tracker = st.empty()
        render_tracker(tracker, {}, PIPELINE_STEPS[0], 0.0, set())

        result, step_times, skipped = None, {}, set()
        cursor = history.audit_cursor()
        try:
            last_t = 0.0
            for node_name, elapsed, state in agent.run_streaming(query, image_path):
                if node_name == "__final__":
                    result = state
                    step_times["total"] = elapsed
                    render_tracker(tracker, step_times, "", elapsed, skipped)
                    break
                step_times[node_name] = round(elapsed - last_t, 2)
                last_t = elapsed
                # A node that returned {} left no evidence in the state — show it
                # hollow rather than ticking work that never happened.
                evidence_key = STEP_EVIDENCE.get(node_name)
                if evidence_key and not state.get(evidence_key):
                    skipped.add(node_name)
                nxt = next(
                    (s for s in PIPELINE_STEPS if s not in step_times),
                    "",
                ) if node_name in PIPELINE_STEPS else node_name
                render_tracker(tracker, step_times, nxt, elapsed, skipped)
        except Exception as e:
            # Never show an operator a traceback — friendly_error ends in the exact
            # command that fixes it, and the raw detail stays available on demand.
            tracker.empty()
            st.error(agent.friendly_error(e))
            with st.expander("Technical detail (for support)"):
                st.exception(e)

        if result:
            st.session_state["result"] = result
            st.session_state["step_times"] = step_times
            st.session_state["signed_off"] = False
            st.session_state.pop("restored_from", None)
            saved_ok = True
            try:
                history.save_turn(
                    st.session_state["session"], result, cursor=cursor, timing=step_times
                )
            except OSError as e:
                # The answer is already on screen; a failed write must not hide it.
                saved_ok = False
                st.warning(f"Could not save this run to history: {e}")
            if saved_ok:
                # The sidebar is rendered at the top of the script, BEFORE this save,
                # so without a rerun it would keep showing history as of before this
                # run — the just-finished chat wouldn't appear until the next click.
                # Rerun so the sidebar refreshes now; the result persists in
                # session_state and re-renders below on the fresh pass.
                st.rerun()

    result = st.session_state.get("result")
    if result:
        st.markdown('<hr/>', unsafe_allow_html=True)
        render_verdict(result)

        st.markdown('<div class="sec-head" style="margin-top:.5rem">Answer</div>', unsafe_allow_html=True)
        st.markdown(answer_prose(result))

        times = st.session_state.get("step_times", {})
        if times:
            st.markdown('<div class="sec-head" style="margin-top:.5rem">Timing</div>', unsafe_allow_html=True)
            render_timing(times)

        if result.get("requires_approval"):
            if st.session_state.get("signed_off"):
                st.markdown(
                    f'<div class="card" style="border-color:#00FF8833;background:#00FF880A;'
                    f'display:flex;align-items:center;gap:.7rem;padding:1rem 1.25rem">'
                    f'<span style="color:var(--ok);display:flex">{icon("check", 17)}</span>'
                    f'<span style="font-size:.9rem">Signed off by '
                    f'<strong>{html.escape(st.session_state["approver"])}</strong></span></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f'<div class="card" style="border-color:#FFB80033;background:#FFB8000A;'
                    f'display:flex;align-items:center;gap:.7rem;padding:1rem 1.25rem;margin-bottom:.5rem">'
                    f'<span style="color:var(--warn);display:flex">{icon("lock", 17)}</span>'
                    f'<span style="font-size:.9rem">Flagged safety-critical — a named supervisor '
                    f'must sign off before anyone acts on this.</span></div>',
                    unsafe_allow_html=True,
                )
                approver = st.text_input("Supervisor name", key="approver_input",
                                         placeholder="Name of the authorising supervisor")
                if st.button("Authorize & sign off", key="signoff", type="primary") and approver.strip():
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
                    st.markdown(
                        pill("Zero external connections during this query", "ok", "check"),
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(pill("External connection detected", "crit", "alert"), unsafe_allow_html=True)
                    st.write(net["external_connections"])
                st.caption(
                    f"System-wide bytes during window: {net['system_wide_bytes_sent']} sent / "
                    f"{net['system_wide_bytes_recv']} recv (includes any other traffic on the "
                    "machine — not AEGIS-specific; the connection list above is what's scoped)."
                )
                st.text("Connections opened by AEGIS's own process tree:")
                st.text("\n".join(net["connections"]) or "(none)")

        entries = audit.read()[-20:]
        with st.expander(f"Audit trail · {n_entries} entries · chain {'intact' if ok_chain else 'TAMPERED'}"):
            rows = []
            for e in entries:
                color = STEP_COLORS.get(e["event"], "#555")
                rows.append(
                    f'<div class="e"><span class="badge" style="background:{color}1F;color:{color}">'
                    f'{html.escape(e["event"])}</span>'
                    f'<span class="h">{html.escape(e["hash"][:16])}… ← {html.escape(e["prev_hash"][:12])}…</span></div>'
                )
            st.markdown(f'<div class="chain">{"".join(rows)}</div>', unsafe_allow_html=True)

        if st.session_state["session"]["entries"]:
            if st.button("Export this session", key="export",
                         help="Write a readable Markdown copy to data/chats/exports/"):
                path = history.export_session(st.session_state["session"]["id"])
                if path:
                    st.success(f"Exported to `{path}`")
                    with open(path, encoding="utf-8") as f:
                        st.download_button("Download export", f.read(),
                                           file_name=os.path.basename(path), mime="text/markdown")
                else:
                    st.warning("Nothing to export yet.")

with tab_compare:
    st.markdown(
        '<p class="sb-note" style="font-size:.85rem;margin-bottom:.6rem">Compare two SOP or P&amp;ID '
        "revisions and flag safety-critical changes.</p>",
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns(2, gap="medium")
    old_file = c1.file_uploader("Old version", type=["pdf", "txt"], key="old_doc")
    new_file = c2.file_uploader("New version", type=["pdf", "txt"], key="new_doc")

    compare_clicked = st.button("Compare documents", type="primary",
                                use_container_width=True, key="compare")
    if compare_clicked and not (old_file and new_file):
        st.warning("Attach both revisions — an old version and a new one — to compare them.")
    elif compare_clicked:
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
        st.markdown('<hr/>', unsafe_allow_html=True)
        m1, m2 = st.columns(2, gap="medium")
        m1.metric("Sections changed", report["changes_count"])
        m2.metric("Safety-critical changes", len(report["safety_critical_changes"]))

        st.markdown('<div class="sec-head" style="margin-top:.5rem">Safety impact assessment</div>',
                    unsafe_allow_html=True)
        st.markdown(report["summary"])

        if report["safety_critical_changes"]:
            st.markdown('<div class="sec-head" style="margin-top:.5rem">Flagged changes '
                        "<span style='text-transform:none;letter-spacing:0'>(deterministic keyword scan)</span></div>",
                        unsafe_allow_html=True)
            for f in report["safety_critical_changes"]:
                tone = "crit" if f["risk_level"] == "HIGH" else "warn"
                st.markdown(
                    f'<div class="card" style="padding:1rem 1.25rem;margin-bottom:.5rem">'
                    f'<div class="verdict" style="margin-bottom:.7rem">{pill(f["risk_level"], tone, "alert")}'
                    f'<span style="color:#888;font-size:.82rem">'
                    f'{html.escape(f["section"] or "unlabelled section")}</span></div>'
                    f'<div class="diff del"><span class="lbl">Old</span>'
                    f'{html.escape(f["old_value"] or "(not present in old revision)")}</div>'
                    f'<div class="diff add"><span class="lbl">New</span>'
                    f'{html.escape(f["new_value"] or "(removed in new revision)")}</div></div>',
                    unsafe_allow_html=True,
                )

        if d["modified"]:
            st.markdown('<div class="sec-head" style="margin-top:.5rem">Modified sections</div>',
                        unsafe_allow_html=True)
            for m in d["modified"]:
                st.markdown(
                    f'<div class="diff mod"><span class="lbl">Was</span>{html.escape(m["old"])}</div>'
                    f'<div class="diff mod"><span class="lbl">Now</span>{html.escape(m["new"])}</div>',
                    unsafe_allow_html=True,
                )
        if d["removed"]:
            st.markdown('<div class="sec-head" style="margin-top:.5rem">Removed sections</div>',
                        unsafe_allow_html=True)
            for r in d["removed"]:
                st.markdown(f'<div class="diff del">{html.escape(r)}</div>', unsafe_allow_html=True)
        if d["added"]:
            st.markdown('<div class="sec-head" style="margin-top:.5rem">Added sections</div>',
                        unsafe_allow_html=True)
            for a in d["added"]:
                st.markdown(f'<div class="diff add">{html.escape(a)}</div>', unsafe_allow_html=True)
        if not report["changes_count"]:
            st.markdown(pill("No differences detected", "ok", "check"), unsafe_allow_html=True)

st.markdown('<hr/>', unsafe_allow_html=True)
st.markdown(
    '<p class="foot">Built for SIH 2026 · Problem Statement SIH26117 · Team Alpha · '
    "100% air-gapped, all data stored locally</p>",
    unsafe_allow_html=True,
)

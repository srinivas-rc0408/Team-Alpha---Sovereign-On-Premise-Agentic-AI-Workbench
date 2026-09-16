#!/usr/bin/env bash
#
# Start AEGIS. Fully offline: the only thing this contacts is the Ollama daemon
# on this machine. Run ./install.sh once first.
#
set -euo pipefail

# dirname+pwd rather than `readlink -f`: stock macOS/BSD readlink has no -f flag,
# so that idiom silently resolves to "." there and can run against the wrong directory.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT="${AEGIS_PORT:-8501}"

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
else
  BOLD=''; GREEN=''; YELLOW=''; RED=''; DIM=''; OFF=''
fi
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
die()  { printf '\n%sERROR:%s %s\n\n' "$RED" "$OFF" "$1" >&2; exit 1; }

printf '%s\n' "${BOLD}🛡️  AEGIS — starting${OFF}"

# ── venv ─────────────────────────────────────────────────────────────────────
[[ -x venv/bin/python ]] || die "no virtual environment found. Run ./install.sh first."
# shellcheck disable=SC1091
source venv/bin/activate
ok "venv active ($(python --version 2>&1))"

# ── Ollama ───────────────────────────────────────────────────────────────────
command -v ollama >/dev/null 2>&1 || die "Ollama is not installed. Run ./install.sh first."
if ollama list >/dev/null 2>&1; then
  ok "Ollama is running"
else
  warn "Ollama is not running — starting it"
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^ollama\.service'; then
    sudo systemctl start ollama 2>/dev/null || true
  fi
  if ! ollama list >/dev/null 2>&1; then
    mkdir -p logs
    nohup ollama serve >logs/ollama.log 2>&1 &
    disown || true
  fi
  for _ in $(seq 1 30); do ollama list >/dev/null 2>&1 && break; sleep 1; done
  ollama list >/dev/null 2>&1 || die "Ollama did not start. Try 'ollama serve' manually (log: logs/ollama.log)."
  ok "Ollama started"
fi

# ── Offline verification ─────────────────────────────────────────────────────
# Ollama is the last thing that was allowed to need the network (during install).
# From this point everything must hold with the network unplugged — so prove it
# before opening the console rather than claiming it in the UI afterwards.
python - <<'PY' || die "offline verification failed — refusing to start. See the failing check above."
from core import offline_check

try:
    r = offline_check.assert_offline()
except offline_check.OfflineViolation as e:
    print(e)
    raise SystemExit(1)
for name in r["checks"]:
    print(f"  ✓ {name}")
for w in r.get("warnings", []):
    print(f"  ! {w}")
print(f"  ✓ offline verified — 0 external connections, "
      f"{r['queries_audited']} past queries audited clean")
PY

# ── Index ────────────────────────────────────────────────────────────────────
if [[ ! -f data/embeddings/index.faiss ]]; then
  warn "no RAG index found — building it from docs/"
  python -c "from core.rag import build_index; print(f'  indexed {build_index()} chunks')" \
    || die "index build failed. Add documents to docs/ and retry."
fi
ok "knowledge base ready"

# ── Port ─────────────────────────────────────────────────────────────────────
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  die "port ${PORT} is already in use. Stop the other process, or run: AEGIS_PORT=8502 ./run.sh"
fi

# Belt-and-braces alongside .streamlit/config.toml — neither alone should be trusted.
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

URL="http://127.0.0.1:${PORT}"

# Streamlit runs headless (see .streamlit/config.toml), so open the browser here
# once the server is actually accepting connections. Backgrounded so a machine
# with no browser (a headless plant server) still starts normally.
(
  for _ in $(seq 1 20); do
    if command -v curl >/dev/null 2>&1 && curl -sf -o /dev/null "$URL"; then break; fi
    sleep 1
  done
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
  elif command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1
  fi
) &

cat <<EOF

${GREEN}${BOLD}AEGIS is live → ${URL}${OFF}

${DIM}Bound to loopback only — not reachable from the plant LAN.
Chats: data/chats/ · Index: data/embeddings/ · Audit log: logs/audit.jsonl
Press Ctrl+C to stop.${OFF}

EOF

exec streamlit run ui/app.py \
  --server.address 127.0.0.1 \
  --server.port "${PORT}" \
  --server.headless true \
  --browser.gatherUsageStats false

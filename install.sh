#!/usr/bin/env bash
#
# AEGIS one-time setup. This is the ONLY step that needs internet: it installs
# Ollama if missing and pulls ~6.5GB of model weights. Everything after this
# runs fully offline.
#
# Safe to re-run: every step checks whether it is already done before doing it.
#
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

MODELS=(qwen2.5:7b moondream nomic-embed-text)
STEPS=9
STEP=0

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
else
  BOLD=''; GREEN=''; YELLOW=''; RED=''; DIM=''; OFF=''
fi

step() { STEP=$((STEP + 1)); printf '\n%s[%d/%d] %s%s\n' "$BOLD" "$STEP" "$STEPS" "$1" "$OFF"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
die()  { printf '\n%sERROR:%s %s\n\n' "$RED" "$OFF" "$1" >&2; exit 1; }

trap 'die "setup failed at line $LINENO. Fix the error above and re-run ./install.sh — completed steps will be skipped."' ERR

printf '%s\n' "${BOLD}AEGIS — Air-Gapped Engineering Intelligence System${OFF}"
printf '%s\n' "${DIM}One-time setup. Needs internet for this run only.${OFF}"

# ── 1. Ollama present? ───────────────────────────────────────────────────────
step "Checking for Ollama"
if command -v ollama >/dev/null 2>&1; then
  ok "Ollama already installed ($(command -v ollama))"
else
  warn "Ollama not found — installing"
  case "$(uname -s)" in
    Linux)
      command -v curl >/dev/null 2>&1 || die "curl is required to install Ollama. Install it (Arch: sudo pacman -S curl · Ubuntu: sudo apt install curl) and re-run."
      curl -fsSL https://ollama.com/install.sh | sh || die "Ollama install script failed. Install manually from https://ollama.com/download and re-run."
      ;;
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        brew install ollama || die "brew install ollama failed."
      else
        die "Install Ollama from https://ollama.com/download (or install Homebrew first), then re-run ./install.sh"
      fi
      ;;
    *) die "Unsupported OS '$(uname -s)'. Install Ollama manually from https://ollama.com/download, then re-run." ;;
  esac
  command -v ollama >/dev/null 2>&1 || die "Ollama still not on PATH after install. Open a new shell and re-run ./install.sh"
  ok "Ollama installed"
fi

# ── 2. Ollama service running? ───────────────────────────────────────────────
step "Starting the Ollama service"
wait_for_ollama() {
  for _ in $(seq 1 30); do
    ollama list >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

if ollama list >/dev/null 2>&1; then
  ok "Ollama is already running"
else
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^ollama\.service'; then
    sudo systemctl start ollama || warn "systemctl start ollama failed — falling back to a background process"
  fi
  if ! ollama list >/dev/null 2>&1; then
    mkdir -p logs
    nohup ollama serve >logs/ollama.log 2>&1 &
    disown || true
  fi
  wait_for_ollama || die "Ollama did not come up within 30s. Start it manually with 'ollama serve' (see logs/ollama.log) and re-run ./install.sh"
  ok "Ollama is running"
fi

# ── 3. Models ────────────────────────────────────────────────────────────────
step "Pulling models (~6.5GB total — the only step that needs internet)"
if command -v df >/dev/null 2>&1; then
  FREE_GB=$(df -Pk "$HOME" | awk 'NR==2 {print int($4/1048576)}')
  [[ "$FREE_GB" -lt 10 ]] && warn "only ${FREE_GB}GB free in \$HOME — models need ~8GB"
fi

installed_models="$(ollama list 2>/dev/null || true)"
for model in "${MODELS[@]}"; do
  base="${model%%:*}"
  if printf '%s' "$installed_models" | awk '{print $1}' | grep -qx -e "$model" -e "${base}:latest"; then
    ok "$model already present — skipping download"
  else
    printf '  %s↓%s pulling %s …\n' "$YELLOW" "$OFF" "$model"
    ollama pull "$model" || die "failed to pull '$model'. Check your internet connection and re-run ./install.sh (already-downloaded models are skipped)."
    ok "$model pulled"
  fi
done

# ── 4. venv ──────────────────────────────────────────────────────────────────
step "Creating the Python virtual environment"
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || die "python3 not found. Install it (Arch: sudo pacman -S python · Ubuntu: sudo apt install python3 python3-venv) and re-run."
if [[ -x venv/bin/python ]]; then
  ok "venv already exists ($("$PWD/venv/bin/python" --version 2>&1))"
else
  "$PY" -m venv venv || die "could not create the venv. On Ubuntu you may need: sudo apt install python3-venv"
  ok "venv created ($(./venv/bin/python --version 2>&1))"
fi

# ── 5. Dependencies ──────────────────────────────────────────────────────────
step "Installing Python dependencies"
./venv/bin/python -m pip install --upgrade pip --quiet || warn "pip self-upgrade failed — continuing"
# Download progress stays visible on a first install; the "already satisfied" wall
# of text on a re-run does not. pipefail still trips the ERR trap if pip fails,
# and pip's actual error lines are not filtered.
./venv/bin/pip install -r requirements.txt 2>&1 \
  | { grep -vE '^Requirement already satisfied:' || true; }
ok "dependencies installed"

# ── 6. Local storage ─────────────────────────────────────────────────────────
step "Creating local storage folders"
mkdir -p data/embeddings data/chats logs docs temp
ok "data/embeddings, data/chats, logs, docs, temp ready (all local, all gitignored)"

# ── 7. RAG index ─────────────────────────────────────────────────────────────
step "Building the RAG index from docs/"
if compgen -G "docs/*.pdf" >/dev/null || compgen -G "docs/*.txt" >/dev/null || compgen -G "docs/*.md" >/dev/null; then
  ./venv/bin/python -c "from core.rag import build_index; print(f'  indexed {build_index()} chunks')" \
    || die "index build failed. Is Ollama running and is nomic-embed-text pulled? Re-run ./install.sh"
  ok "knowledge base indexed into data/embeddings/"
else
  warn "no documents in docs/ — drop your SOPs there and run 'make index' (or rebuild from the app sidebar)"
fi

# ── 8. Verify ────────────────────────────────────────────────────────────────
step "Running the verification suite"
./venv/bin/python test_aegis.py || die "test_aegis.py reported a failure — see the output above."

# ── 9. Done ──────────────────────────────────────────────────────────────────
step "Setup complete"
cat <<EOF

${GREEN}${BOLD}✅ AEGIS is ready — run ./run.sh to start${OFF}

  ${BOLD}./run.sh${OFF}     start the app at http://127.0.0.1:8501
  ${BOLD}make test${OFF}    re-run the verification suite
  ${BOLD}make index${OFF}   re-index docs/ after adding SOPs

${DIM}From here on AEGIS needs no internet. You can unplug the network and it
will keep working — models, index, chats and audit log are all on this machine.${OFF}

EOF

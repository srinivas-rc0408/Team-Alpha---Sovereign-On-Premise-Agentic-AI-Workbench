.PHONY: install run test index clean reset-chats help

help:
	@echo "AEGIS"
	@echo "  make install      one-time setup (installs Ollama, pulls models, builds the index)"
	@echo "  make run          start the app on http://127.0.0.1:8501 (offline)"
	@echo "  make test         run the verification suite + every module self-check"
	@echo "  make index        re-index docs/ after adding SOPs"
	@echo "  make clean        remove caches and temp/ (keeps venv, chats, index, audit log)"
	@echo "  make reset-chats  DELETE all saved chat history in data/chats/"

install:
	@chmod +x install.sh run.sh
	@./install.sh

run:
	@chmod +x run.sh
	@./run.sh

test:
	./venv/bin/python test_aegis.py
	@echo ""
	@echo "--- module self-checks ---"
	./venv/bin/python -m core.safety_rules
	./venv/bin/python -m core.tools
	./venv/bin/python -m core.audit
	./venv/bin/python -m core.network_monitor
	./venv/bin/python -m core.doc_diff
	./venv/bin/python -m core.history
	./venv/bin/python -m core.offline_check

index:
	./venv/bin/python -c "from core.rag import build_index; print(f'Indexed {build_index()} chunks')"

# Caches and scratch only — never venv, data/ or logs/, so `make clean` can't
# cost anyone their chat history, index or audit trail.
clean:
	find . -path ./venv -prune -o -name '__pycache__' -type d -print0 2>/dev/null | xargs -0 rm -rf --
	find . -path ./venv -prune -o -name '*.pyc' -print0 2>/dev/null | xargs -0 rm -f --
	rm -rf temp/*
	@echo "cleaned caches and temp/ (venv, data/ and logs/ untouched)"

reset-chats:
	@rm -f data/chats/*.json
	@echo "chat history cleared (data/chats/). Audit log at logs/audit.jsonl is untouched."

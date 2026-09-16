.PHONY: setup test demo index

setup:
	python3 -m venv venv
	./venv/bin/pip install -r requirements.txt
	ollama pull qwen2.5:7b
	ollama pull moondream
	ollama pull nomic-embed-text

test:
	./venv/bin/python test_aegis.py

demo:
	./venv/bin/streamlit run ui/app.py --server.address 127.0.0.1

index:
	./venv/bin/python -c "from core.rag import build_index; print(f'Indexed {build_index()} chunks')"

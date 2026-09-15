"""Hybrid (dense + BM25) knowledge base over docs/, embedded locally with
nomic-embed-text. Fully offline: index lives on disk, no network at query time.

Dense-only retrieval misses exact SOP identifiers and jargon (e.g. "SOP-MNT-402",
"18 bar") that don't cluster well in embedding space. BM25 catches those; the two
rankings are merged with reciprocal rank fusion.
"""
import os
import pickle

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi


def _embeddings():
    return OllamaEmbeddings(
        model=os.getenv("EMBED_MODEL", "nomic-embed-text"),
        base_url=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
    )


def _index_dir():
    return os.getenv("INDEX_DIR", "data/embeddings")


def _load_docs(docs_dir):
    docs = []
    for root, _, files in os.walk(docs_dir):
        for name in files:
            path = os.path.join(root, name)
            ext = name.lower().rsplit(".", 1)[-1]
            try:
                if ext == "pdf":
                    docs.extend(PyPDFLoader(path).load())
                elif ext in ("txt", "md"):
                    docs.extend(TextLoader(path, encoding="utf-8").load())
            except Exception as e:  # one bad file shouldn't sink the whole index
                print(f"skip {path}: {e}")
    return docs


def _bm25_path():
    return os.path.join(_index_dir(), "bm25.pkl")


def _tokenize(text: str) -> list[str]:
    return text.lower().split()


def build_index(docs_dir=None) -> int:
    """(Re)build the hybrid (FAISS + BM25) index from docs_dir. Returns the chunk count."""
    docs_dir = docs_dir or os.getenv("DOCS_DIR", "docs")
    docs = _load_docs(docs_dir)
    if not docs:
        raise ValueError(f"No .pdf/.txt/.md documents found in {docs_dir}")
    chunks = RecursiveCharacterTextSplitter(
        chunk_size=1000, chunk_overlap=150
    ).split_documents(docs)
    vs = FAISS.from_documents(chunks, _embeddings())
    os.makedirs(_index_dir(), exist_ok=True)
    vs.save_local(_index_dir())

    bm25 = BM25Okapi([_tokenize(c.page_content) for c in chunks])
    with open(_bm25_path(), "wb") as f:
        pickle.dump({"bm25": bm25, "chunks": chunks}, f)

    _STORE["vs"] = vs
    _STORE["bm25"] = bm25
    _STORE["chunks"] = chunks
    return len(chunks)


_STORE = {"vs": None, "bm25": None, "chunks": None}


def _store():
    if _STORE["vs"] is None:
        path = _index_dir()
        if not os.path.exists(os.path.join(path, "index.faiss")):
            return None
        _STORE["vs"] = FAISS.load_local(
            path, _embeddings(), allow_dangerous_deserialization=True
        )
        # trusted: bm25.pkl is only ever produced by build_index() on this same
        # machine, next to the FAISS index it already trusts via the flag above.
        with open(_bm25_path(), "rb") as f:
            bm25_data = pickle.load(f)
        _STORE["bm25"] = bm25_data["bm25"]
        _STORE["chunks"] = bm25_data["chunks"]
    return _STORE["vs"]


def _rrf_merge(dense_docs, bm25_ranked, k: int, rrf_k: int = 60) -> list:
    """Reciprocal rank fusion: score = sum(1 / (rrf_k + rank)) across both rankings."""
    scores = {}
    docs_by_key = {}

    def key(d):
        return (d.metadata.get("source"), d.metadata.get("page"), d.page_content[:80])

    for rank, d in enumerate(dense_docs):
        k_ = key(d)
        docs_by_key[k_] = d
        scores[k_] = scores.get(k_, 0.0) + 1.0 / (rrf_k + rank)
    for rank, d in enumerate(bm25_ranked):
        k_ = key(d)
        docs_by_key[k_] = d
        scores[k_] = scores.get(k_, 0.0) + 1.0 / (rrf_k + rank)

    ranked_keys = sorted(scores, key=scores.get, reverse=True)[:k]
    return [docs_by_key[k_] for k_ in ranked_keys]


def search(query: str, k: int = 4) -> list[dict]:
    vs = _store()
    if vs is None:
        return []
    dense = vs.similarity_search(query, k=k * 2)
    bm25_scores = _STORE["bm25"].get_scores(_tokenize(query))
    bm25_ranked = [
        _STORE["chunks"][i]
        for i in sorted(range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True)[: k * 2]
    ]
    merged = _rrf_merge(dense, bm25_ranked, k)
    return [
        {
            "text": d.page_content,
            "source": os.path.basename(d.metadata.get("source", "?")),
            "page": d.metadata.get("page"),
        }
        for d in merged
    ]

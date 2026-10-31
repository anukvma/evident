"""Local sentence embeddings (BAAI/bge-small-en-v1.5) for dense retrieval and semantic decoy similarity.

The model is loaded from paths.EMBED_MODEL only; nothing is downloaded at run time (the pipeline has no network access).
bge models expect an instruction prefix on short queries and none on passages.
"""
from __future__ import annotations

import numpy as np

from . import paths

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
_MODEL = None


def model():
    global _MODEL
    if _MODEL is None:
        if not (paths.EMBED_MODEL / "config.json").exists():
            raise FileNotFoundError(
                f"embedding model not found at {paths.EMBED_MODEL}. Download it once (outside the sandbox):\n"
                f"  huggingface-cli download BAAI/bge-small-en-v1.5 --local-dir {paths.EMBED_MODEL}")
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(str(paths.EMBED_MODEL), device="cpu")
    return _MODEL


def encode(texts, query=False, batch_size=64) -> np.ndarray:
    """L2-normalised embeddings, so a dot product is the cosine similarity."""
    texts = [(QUERY_PREFIX + t) if query else t for t in texts]
    return np.asarray(model().encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                     show_progress_bar=False), dtype=np.float32)


def schema_text(name, cols):
    """Passage for one table: its name and column names, split into words (no data values)."""
    import re
    split = lambda s: re.sub(r"[_\W]+", " ", re.sub(r"([a-z])([A-Z])", r"\1 \2", str(s))).strip().lower()  # noqa: E731
    return f"table {split(name)}: " + ", ".join(split(c) for c in cols)

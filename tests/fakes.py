import hashlib
import math
import re

from app.core import embeddings


class FakeEmbedder:
    """Whitespace tokens and hashed bag-of-words vectors: deterministic and model-free."""

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def embed_passages(self, texts):
        return [self._vector(text) for text in texts]

    embed_queries = embed_passages

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * embeddings.EMBEDDING_DIMENSION
        for word in re.findall(r"[a-z]+", text.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % len(vector)] += 1
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

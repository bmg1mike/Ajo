import threading
from collections.abc import Callable, Sequence
from typing import Protocol

# Must match the chunks.embedding column width.
EMBEDDING_DIMENSION = 384


class Embedder(Protocol):
    """Embeds text for retrieval. Vectors are L2-normalized, so dot product is cosine."""

    def count_tokens(self, text: str) -> int: ...
    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


class E5Embedder:
    """Local multilingual-e5 embeddings. e5 models require `query: `/`passage: ` prefixes."""

    def __init__(self, model_name: str, *, batch_size: int = 32) -> None:
        # Imported lazily: loading torch is slow and unnecessary for most callers.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name, device="cpu")
        self._batch_size = batch_size
        dimension = self._model.get_embedding_dimension()
        if dimension != EMBEDDING_DIMENSION:
            raise ValueError(f"{model_name} produces {dimension}-dimensional vectors; expected {EMBEDDING_DIMENSION}")

    @property
    def max_tokens(self) -> int:
        return self._model.max_seq_length

    def count_tokens(self, text: str) -> int:
        return len(self._model.tokenizer(text, add_special_tokens=False, verbose=False)["input_ids"])

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        prefixed = [f"passage: {text}" for text in texts]
        for text in prefixed:
            # The model silently truncates long inputs; a stored passage must be embedded whole.
            if len(self._model.tokenizer(text, verbose=False)["input_ids"]) > self.max_tokens:
                raise ValueError(f"Passage exceeds {self.max_tokens} tokens: {text[:80]!r}")
        return self._encode(prefixed)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode([f"query: {text}" for text in texts])

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(texts, batch_size=self._batch_size, normalize_embeddings=True,
                                     convert_to_numpy=True)
        return vectors.tolist()


class LazyEmbedder:
    """Defers building the real embedder (seconds of model loading) until first use."""

    def __init__(self, factory: Callable[[], Embedder]) -> None:
        self._factory = factory
        self._embedder: Embedder | None = None
        self._lock = threading.Lock()

    def _get(self) -> Embedder:
        with self._lock:
            if self._embedder is None:
                self._embedder = self._factory()
            return self._embedder

    def count_tokens(self, text: str) -> int:
        return self._get().count_tokens(text)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._get().embed_passages(texts)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._get().embed_queries(texts)

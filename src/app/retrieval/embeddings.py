from collections.abc import Sequence


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384


class IssueEmbedder:
    """Generate normalized issue and query embeddings with a local model."""

    def __init__(self, model: object | None = None) -> None:
        if model is None:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(MODEL_NAME)
        self._model = model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            list(texts),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        result = vectors.tolist() if hasattr(vectors, "tolist") else vectors
        return [list(vector) for vector in result]

    def encode_query(self, query: str) -> list[float]:
        return self.encode([query])[0]

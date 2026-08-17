from unittest.mock import MagicMock

from app.retrieval.embeddings import IssueEmbedder


class FakeVectors(list[list[float]]):
    def tolist(self) -> list[list[float]]:
        return list(self)


def test_embedder_requests_normalized_embeddings() -> None:
    model = MagicMock()
    model.encode.return_value = FakeVectors([[0.6, 0.8]])

    result = IssueEmbedder(model).encode(["an issue"])

    assert result == [[0.6, 0.8]]
    model.encode.assert_called_once_with(
        ["an issue"],
        normalize_embeddings=True,
        show_progress_bar=False,
    )


def test_empty_input_does_not_call_model() -> None:
    model = MagicMock()

    assert IssueEmbedder(model).encode([]) == []
    model.encode.assert_not_called()

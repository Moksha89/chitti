from collections.abc import Sequence
from functools import lru_cache
from typing import Any, Protocol


class EmbedderProtocol(Protocol):
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


class Embedder:
    dimensions = 384

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed(self, text: str) -> list[float]:
        return list(next(self._load().embed([text])))


class FakeEmbedder:
    dimensions = 384

    def embed(self, text: str) -> list[float]:
        values = [0.0] * self.dimensions
        for index, char in enumerate(text.encode("utf-8")):
            values[index % self.dimensions] += (char + 1) / 255
        magnitude = sum(value * value for value in values) ** 0.5 or 1
        return [value / magnitude for value in values]


@lru_cache
def get_embedder(model_name: str) -> Embedder:
    return Embedder(model_name)


def vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"

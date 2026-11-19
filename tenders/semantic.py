"""Подбор закупок по смыслу: векторы текстов от локальной модели.

Клиент описывает своими словами, что делает («ремонт мягкой кровли, гидроизоляция»),
модель переводит это описание и тексты закупок в векторы, и закупки с близким
смыслом попадают в подборку, даже если слова не совпали.

Модель работает только на нашем сервере (152-ФЗ): библиотека fastembed (ONNX, без
PyTorch), по умолчанию paraphrase-multilingual-MiniLM-L12-v2 в квантованном виде
(~220 МБ на диске, ~400 МБ памяти во время расчёта). Модель загружается только в
задаче загрузки закупок, веб-кабинет её не держит в памяти: он сравнивает уже
посчитанные векторы.

Включение на сервере:
    /opt/bg-agent/venv/bin/pip install -r requirements-ml.txt
    echo EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 >> /opt/bg-agent/env
"""

from __future__ import annotations

import math
from typing import Iterable, Protocol

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_THRESHOLD = 0.6
MAX_CHARS = 1000     # модели всё равно режут длинный текст; предмет и позиции помещаются


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """Обёртка над fastembed; модель скачивается при первом запуске и кешируется."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: str | None = None, threads: int = 1):
        from fastembed import TextEmbedding   # необязательная зависимость: requirements-ml.txt
        self.model = TextEmbedding(model_name=model, cache_dir=cache_dir, threads=threads)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[round(float(x), 5) for x in v] for v in self.model.embed(texts, batch_size=16)]


def clip(text: str) -> str:
    return " ".join((text or "").split())[:MAX_CHARS]


def cosine(a: Iterable[float] | None, b: Iterable[float] | None) -> float:
    if not a or not b:
        return 0.0
    a, b = list(a), list(b)
    if len(a) != len(b):
        return 0.0          # векторы от разных моделей не сравниваем
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0

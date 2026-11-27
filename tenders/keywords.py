"""Поиск закупок по ключевым словам с учётом словоформ, синонимов и минус-слов.

Работает целиком на нашем сервере, без внешних нейросетей (152-ФЗ):
стемминг Snowball для русского языка + небольшой словарь синонимов отрасли.

Синтаксис запроса (термы через запятую или с новой строки):
    кровля                — любое слово с этой основой: кровли, кровельные...; плюс синонимы (крыша)
    капитальный ремонт    — все слова фразы в тексте, в любом порядке и форме
    асфальт|щебень        — любое из вариантов
    -проект               — минус-слово: закупки с ним не показываем (проектные работы)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import snowballstemmer

_stemmer = snowballstemmer.stemmer("russian")
WORD_RE = re.compile(r"[a-zа-я0-9]+")

# Синонимы и частые сокращения в закупках. Каждая группа — равноправные варианты.
SYNONYMS: list[tuple[str, ...]] = [
    ("кровля", "крыша", "кровельный"),
    ("капремонт", "капитальный ремонт"),
    ("асфальт", "асфальтобетон", "дорожное покрытие"),
    ("автодорога", "автомобильная дорога", "дорога"),
    ("видеонаблюдение", "свн", "камеры видеонаблюдения"),
    ("пожарная сигнализация", "апс", "аупс", "противопожарная сигнализация"),
    ("скуд", "контроль доступа"),
    ("электромонтаж", "электромонтажные работы", "электроустановка"),
    ("водоснабжение", "водопровод"),
    ("водоотведение", "канализация"),
    ("теплоснабжение", "теплосеть", "тепловая сеть"),
    ("отопление", "система отопления"),
    ("вентиляция", "система вентиляции"),
    ("лифт", "подъемник", "лифтовое оборудование"),
    ("благоустройство", "благоустройство территории"),
    ("уборка", "клининг", "содержание территории"),
    ("пнр", "пусконаладочные работы"),
    ("смр", "строительно-монтажные работы", "строительство"),
    ("пир", "проектно-изыскательские работы", "проектирование"),
    ("программное обеспечение", "лицензия на по"),
    ("компьютер", "пк", "персональный компьютер", "ноутбук"),
    ("медизделие", "медицинское изделие"),
    ("гсм", "топливо", "бензин", "дизельное топливо"),
]

MIN_PREFIX = 5   # основа длиннее — засчитываем и более длинные слова (асфальт -> асфальтобетонный)


def stem(word: str) -> str:
    return _stemmer.stemWord(word)


def stems(text: str) -> list[str]:
    return [stem(w) for w in WORD_RE.findall((text or "").lower().replace("ё", "е"))]


@lru_cache(maxsize=1)
def _synonym_index() -> dict[tuple[str, ...], list[tuple[str, ...]]]:
    index: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for group in SYNONYMS:
        variants = [tuple(stems(v)) for v in group]
        for v in variants:
            index.setdefault(v, [])
            index[v] += [x for x in variants if x not in index[v]]
    return index


@dataclass
class Term:
    label: str                                  # как написал пользователь
    variants: list[tuple[str, ...]] = field(default_factory=list)   # основы слов каждого варианта
    minus: bool = False


def parse_query(items: list[str] | str) -> list[Term]:
    if isinstance(items, str):
        items = re.split(r"[,;\n]", items)
    terms = []
    for raw in items:
        label = raw.strip()
        if not label:
            continue
        minus = label.startswith("-")
        body = label.lstrip("-").strip()
        variants: list[tuple[str, ...]] = []
        for alt in body.split("|"):
            v = tuple(stems(alt))
            if v and v not in variants:
                variants.append(v)
                variants += [s for s in _synonym_index().get(v, []) if s not in variants]
        if variants:
            terms.append(Term(label=body, variants=variants, minus=minus))
    return terms


def _word_hit(q: str, words: set[str]) -> bool:
    if q in words:
        return True
    return len(q) >= MIN_PREFIX and any(w.startswith(q) for w in words)


def _term_hit(term: Term, words: set[str]) -> bool:
    return any(all(_word_hit(q, words) for q in variant) for variant in term.variants)


@dataclass
class KeywordResult:
    matched: list[str]          # какие термы нашлись
    excluded_by: str | None     # минус-слово, из-за которого закупка отброшена


def search(text: str, terms: list[Term]) -> KeywordResult:
    words = set(stems(text))
    for t in terms:
        if t.minus and _term_hit(t, words):
            return KeywordResult(matched=[], excluded_by=t.label)
    return KeywordResult(matched=[t.label for t in terms if not t.minus and _term_hit(t, words)],
                         excluded_by=None)

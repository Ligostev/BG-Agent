"""Проверка контрольных сумм ИНН и ОГРН/ОГРНИП."""

import re

_INN10_W = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _checksum(digits: str, weights: tuple[int, ...]) -> int:
    return sum(int(d) * w for d, w in zip(digits, weights)) % 11 % 10


def is_valid_inn(value: str) -> bool:
    """ИНН юрлица (10 цифр) или физлица/ИП (12 цифр)."""
    if not re.fullmatch(r"\d{10}|\d{12}", value or ""):
        return False
    if len(value) == 10:
        return _checksum(value, _INN10_W) == int(value[9])
    return (_checksum(value, _INN12_W1) == int(value[10])
            and _checksum(value, _INN12_W2) == int(value[11]))


def is_valid_ogrn(value: str) -> bool:
    """ОГРН юрлица (13 цифр) или ОГРНИП (15 цифр)."""
    if not re.fullmatch(r"\d{13}|\d{15}", value or ""):
        return False
    body, check = int(value[:-1]), int(value[-1])
    mod = 11 if len(value) == 13 else 13
    return body % mod % 10 == check


def classify(query: str) -> str | None:
    """Определяет тип идентификатора: inn_ul, inn_fl, ogrn, ogrnip или None."""
    q = re.sub(r"\D", "", query or "")
    if is_valid_inn(q):
        return "inn_ul" if len(q) == 10 else "inn_fl"
    if is_valid_ogrn(q):
        return "ogrn" if len(q) == 13 else "ogrnip"
    return None

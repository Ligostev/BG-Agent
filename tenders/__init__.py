"""Подбор закупок для клиентов-поставщиков по официальным данным ЕИС.

- eis.py      — сервис отдачи документов ЕИС (getDocsIP) по токену из Госуслуг;
- notice.py   — разбор XML-извещений 44-ФЗ в Notice;
- match.py    — профиль клиента и отбор подходящих закупок;
- imports.py  — разбор Excel/CSV-выгрузок из поиска ЕИС (прошлые контракты клиента).

Пакет не зависит ни от кабинета, ни от Telegram.
"""

from .eis import EisClient, EisError
from .match import Match, Profile, match_notice
from .notice import Notice, parse_notice

__all__ = ["EisClient", "EisError", "Match", "Notice", "Profile", "match_notice", "parse_notice"]

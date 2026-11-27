"""Блокировки счетов и ограничения на открытие счетов.

Два официальных источника:
  * ФНС, сервис «Информирование банков о состоянии обработки электронных
    документов» (service.nalog.ru/bi.do): действующие решения налоговых органов
    о приостановлении операций по счетам. Пока такое решение действует, банк не
    вправе открывать организации или ИП новые счета (п. 12 ст. 76 НК РФ).
  * Банк России, платформа «Знай своего клиента» (cbr.ru, «Проверка по ИНН»):
    отнесена ли компания или ИП к группе высокого уровня риска («красная зона»).
"""

from .check import AccountBlockReport, FnsResult, ZskResult
from .fns import (BlockDecision, CaptchaRequired, FnsBlocksClient, FnsBlocksError,
                  FnsFormatError, parse_decisions)
from .zsk import ZSK_PAGE, ZskClient, ZskError, parse_zsk

__all__ = [
    "AccountBlockReport", "BlockDecision", "CaptchaRequired", "FnsBlocksClient",
    "FnsBlocksError", "FnsFormatError", "FnsResult", "ZSK_PAGE", "ZskClient", "ZskError",
    "ZskResult", "parse_decisions", "parse_zsk",
]

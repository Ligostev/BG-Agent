"""Проверка по реестру недобросовестных поставщиков (zakupki.gov.ru)."""

from .client import RnpClient, RnpEntry, RnpError, parse_results

__all__ = ["RnpClient", "RnpEntry", "RnpError", "parse_results"]

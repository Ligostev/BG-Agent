"""Проверка по реестру юрлиц, привлечённых к ответственности по ст. 19.28 КоАП
(незаконное вознаграждение от имени юрлица). Источник — файл в ЕИС."""

from .check import Koap1928Report, check_koap1928
from .client import Koap1928Client, RegistryUnavailable, find_file_url
from .registry import Registry, RegistryFormatError, Violation, parse_rows, parse_xlsx

__all__ = ["Koap1928Client", "Koap1928Report", "Registry", "RegistryFormatError",
           "RegistryUnavailable", "Violation", "check_koap1928", "find_file_url",
           "parse_rows", "parse_xlsx"]

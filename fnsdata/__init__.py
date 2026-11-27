"""Открытые данные ФНС (nalog.gov.ru/opendata): дисквалифицированные лица и налоговые
правонарушения. Наборы скачиваются целиком и ищутся локально по ИНН/ФИО."""

from .disqualified import DisqualifiedRegistry, Disqualification, parse_disqualified
from .opendata import OpenDataError, OpenDataset, find_data_url
from .taxoffence import TaxOffence, TaxOffenceIndex, build_taxoffence_index

__all__ = ["Disqualification", "DisqualifiedRegistry", "OpenDataError", "OpenDataset",
           "TaxOffence", "TaxOffenceIndex", "build_taxoffence_index", "find_data_url",
           "parse_disqualified"]

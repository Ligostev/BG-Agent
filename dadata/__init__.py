"""DaData: карточка юрлица или ИП по ИНН/ОГРН через официальный API (findById/party)."""

from .client import DaDataAuthError, DaDataClient, DaDataError, DaDataLimitError
from .party import Card, parse_party

__all__ = ["Card", "DaDataAuthError", "DaDataClient", "DaDataError", "DaDataLimitError",
           "parse_party"]

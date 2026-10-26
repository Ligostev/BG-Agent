"""Получение данных о компаниях и ИП из сервиса ФНС «Прозрачный бизнес»."""

from .client import (CaptchaRequired, CaptchaSolver, NotFound, PbNalogClient,
                     PbNalogError, SearchResult)
from .validators import classify, is_valid_inn, is_valid_ogrn

__all__ = ["PbNalogClient", "SearchResult", "CaptchaSolver", "PbNalogError",
           "CaptchaRequired", "NotFound", "classify", "is_valid_inn", "is_valid_ogrn"]

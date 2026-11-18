"""Исполнительные производства и долги из банка данных ФССП."""

from .client import (ALL_REGIONS, CaptchaRequired, CaptchaSolver, FsspClient, FsspError,
                     Proceeding, parse_amount, parse_table)

__all__ = ["FsspClient", "Proceeding", "FsspError", "CaptchaRequired", "CaptchaSolver",
           "ALL_REGIONS", "parse_amount", "parse_table"]

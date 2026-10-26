"""Сводная проверка компании или ИП по нескольким источникам."""

from .checker import CompanyChecker, make_flags, normalize_name

__all__ = ["CompanyChecker", "make_flags", "normalize_name"]

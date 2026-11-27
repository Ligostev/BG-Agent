"""Связанные лица из карточки ФНС: руководители, учредители и их другие компании.

Точная схема ответа pb.nalog.ru не сверена с живым сайтом, поэтому карточка
обходится целиком: любой объект с корректным ИНН считается связанным лицом,
а роль определяется по названиям ключей на пути к нему. Объект с ИНН внутри
другого такого объекта (например, список компаний руководителя) получает
роль «Компания <роль родителя>».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pbnalog.validators import is_valid_inn

INN_KEYS = ("inn", "ИНН", "innfl", "innul", "inn_fl", "inn_ul", "ИННФЛ", "ИННЮЛ")
NAME_KEYS = ("name", "namec", "namep", "fio", "ФИО", "НаимЮЛСокр", "НаимЮЛПолн",
             "НаимПолнЮЛ", "nameul", "name_ex", "fullName")
FIO_KEYS = (("Фамилия", "Имя", "Отчество"), ("lastName", "firstName", "middleName"),
            ("fam", "nam", "otch"))

# Подстрока в ключе -> роль. Порядок важен: первое совпадение выигрывает.
ROLES = [
    (re.compile(r"ruk|upr|head|director|ceo|руковод|долж|СведДолжнФЛ", re.I), "Руководитель"),
    (re.compile(r"uchr|founder|owner|учред|участ", re.I), "Учредитель"),
    (re.compile(r"predsh|predecessor|правопредш", re.I), "Правопредшественник"),
    (re.compile(r"preem|successor|правопреем", re.I), "Правопреемник"),
    (re.compile(r"filial|branch|dochern|subsid|дочерн|филиал", re.I), "Дочерняя компания"),
]


@dataclass
class Party:
    inn: str
    name: str
    roles: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "company" if len(self.inn) == 10 else "person"

    @property
    def role(self) -> str:
        return ", ".join(self.roles) or "Связанное лицо"


def _role(key: str) -> str | None:
    for rx, role in ROLES:
        if rx.search(key):
            return role
    return None


def _name(d: dict) -> str | None:
    for k in NAME_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for keys in FIO_KEYS:
        parts = [d.get(k) for k in keys]
        if parts[0]:
            return " ".join(str(p) for p in parts if p)
    for v in d.values():  # ФИО или название часто лежит во вложенном объекте (СвФЛ, СвНаимЮЛ)
        if isinstance(v, dict):
            name = _name(v)
            if name:
                return name
    return None


def _inn(d: dict) -> str | None:
    for k in INN_KEYS:
        v = d.get(k)
        if v is not None and is_valid_inn(re.sub(r"\D", "", str(v))):
            return re.sub(r"\D", "", str(v))
    return None


def extract_parties(card: Any, exclude: set[str] | None = None, limit: int = 30) -> list[Party]:
    exclude = {x for x in (exclude or set()) if x}
    found: dict[str, Party] = {}

    def add(inn: str, name: str | None, role: str) -> None:
        party = found.get(inn)
        if party is None:
            if len(found) >= limit:
                return
            party = found[inn] = Party(inn=inn, name=name or inn)
        elif party.name == inn and name:
            party.name = name
        if role not in party.roles:
            party.roles.append(role)

    def walk(node: Any, role: str | None, owner: Party | None) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, role, owner)
            return
        if not isinstance(node, dict):
            return
        inn = _inn(node)
        here = owner
        if inn and inn not in exclude:
            if owner is not None:
                r = f"Компания: {owner.roles[0].lower()} {owner.name}" if owner.kind == "person" \
                    else f"Связана с {owner.name}"
            else:
                r = role or "Связанное лицо"
            add(inn, _name(node), r)
            here = found.get(inn, owner)
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                walk(value, _role(str(key)) or role, here)

    walk(card, None, None)
    return list(found.values())

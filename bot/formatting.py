"""Текст ответа бота по отчёту CompanyChecker.check()."""

from html import escape
from typing import Any

# Названия полей в ответах pb.nalog.ru пока не сверены с живым сайтом,
# поэтому для каждого показателя перебираем несколько вариантов.
FIELDS = [
    ("Наименование", ("namec", "namep", "name", "name_ex")),
    ("ИНН", ("inn",)),
    ("ОГРН", ("ogrn",)),
    ("Статус", ("sulst_name", "sipst_name", "status")),
    ("Дата регистрации", ("dtreg", "dtogrn", "registration_date")),
    ("ОКВЭД", ("okved2name", "okved_name", "okved2", "okved")),
    ("Регион", ("regionname", "region")),
    ("Адрес", ("address", "adr", "addr")),
]

MAX_ITEMS = 5


def pick(data: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = data.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def format_result(result: dict) -> str:
    """Карточка из pb.nalog.ru (результат PbNalogClient.lookup)."""
    kind = "Юридическое лицо" if result["kind"] == "ul" else "Индивидуальный предприниматель"
    merged = {**result.get("card", {}), **result["hit"]}
    lines = [f"<b>{escape(kind)}</b>"]
    for label, keys in FIELDS:
        value = pick(merged, keys)
        if value is not None:
            lines.append(f"{label}: {escape(str(value))}")
    return "\n".join(lines)


def _problem(src: dict) -> str:
    if src["status"] == "skipped":
        return "не проверено: " + escape(src["reason"])
    if src["status"] == "not_found":
        return "ничего не найдено"
    if src.get("error_type") in ("CaptchaRequired", "TimeoutError"):
        return "капча не пройдена, попробуйте ещё раз"
    return "сайт не ответил, попробуйте позже"


def format_rnp(src: dict) -> str:
    head = "<b>Реестр недобросовестных поставщиков</b>\n"
    if src["status"] != "ok":
        return head + _problem(src)
    if not src["entries"]:
        return head + "не найден"
    lines = []
    for e in src["entries"][:MAX_ITEMS]:
        state = "действует" if e["active"] else "исключён"
        included = next((v for k, v in e["dates"].items() if "включ" in k.lower()), "")
        lines.append(f"• {escape(e['law'] or 'запись')} № {escape(e['number'])}, {state}"
                     + (f", включён {escape(included)}" if included else ""))
    if len(src["entries"]) > MAX_ITEMS:
        lines.append(f"…и ещё {len(src['entries']) - MAX_ITEMS}")
    return head + "\n".join(lines)


def format_fssp(src: dict) -> str:
    head = "<b>Исполнительные производства ФССП</b>\n"
    if src["status"] != "ok":
        return head + _problem(src)
    rows = src["proceedings"]
    active = [p for p in rows if p["active"]]
    if not rows:
        return head + "не найдены"
    lines = [f"Всего {len(rows)}, действующих {len(active)}"]
    for p in active[:MAX_ITEMS]:
        subject = p["subject"] if len(p["subject"]) <= 200 else p["subject"][:200] + "…"
        lines.append(f"• {escape(p['proceeding'])}: {escape(subject)}")
    if len(active) > MAX_ITEMS:
        lines.append(f"…и ещё {len(active) - MAX_ITEMS}")
    lines.append(f"<i>{escape(src['note'])}</i>")
    return head + "\n".join(lines)


def format_report(report: dict) -> str:
    src = report["sources"]
    pb = src["pbnalog"]
    parts = [format_result(pb["data"]) if pb["status"] == "ok"
             else "<b>Прозрачный бизнес (ФНС)</b>\n" + _problem(pb)]
    if report["flags"]:
        parts.append("<b>⚠️ Обратите внимание</b>\n" + "\n".join(f"• {escape(f)}" for f in report["flags"]))
    parts += [format_rnp(src["rnp"]), format_fssp(src["fssp"]), "Полный отчёт во вложении."]
    return "\n\n".join(parts)

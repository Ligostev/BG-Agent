"""Текст ответа бота по результату PbNalogClient.lookup()."""

from html import escape

from pbnalog.summary import kind_title, summarize


def format_result(result: dict) -> str:
    lines = [f"<b>{escape(kind_title(result['kind']))}</b>"]
    lines += [f"{label}: {escape(value)}" for label, value in summarize(result)]
    lines.append("\nПолный ответ ФНС во вложении.")
    return "\n".join(lines)

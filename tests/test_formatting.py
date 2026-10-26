from bot.formatting import format_result


def test_format_ul():
    text = format_result({
        "kind": "ul",
        "hit": {"namec": "ООО <Ромашка>", "inn": "7707083893", "ogrn": "1027700132195", "token": "t"},
        "card": {"sulst_name": "Действующая"},
    })
    assert "Юридическое лицо" in text
    assert "ООО &lt;Ромашка&gt;" in text
    assert "Статус: Действующая" in text
    assert "token" not in text

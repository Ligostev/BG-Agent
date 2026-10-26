import asyncio

from checks import CompanyChecker, normalize_name
from fssp import CaptchaRequired, Proceeding
from pbnalog import NotFound
from rnp import RnpEntry
from bot.formatting import format_report


class FakePb:
    def __init__(self, hit=None):
        self.hit = hit

    async def lookup(self, q):
        if self.hit is None:
            raise NotFound(q)
        return {"kind": "ul", "hit": self.hit, "card": {}}


class FakeRnp:
    def __init__(self):
        self.asked = []

    async def check(self, inn):
        self.asked.append(inn)
        return [RnpEntry("23000123-22", "https://zakupki.gov.ru/x", law="44-ФЗ", inn=inn,
                         dates={"Включено": "12.03.2022"})]


def proc(debtor, ended="", subject="Задолженность: 1000 руб."):
    return Proceeding(debtor, debtor.split(" 1")[0].split(" Г.")[0], "1/23/77001-ИП от 01.01.2023", "", ended, subject, "ОСП", "", 1000.0)


class FakeFssp:
    def __init__(self, fail=None):
        self.fail = fail
        self.asked = []

    async def search_legal(self, name):
        self.asked.append(("legal", name))
        if self.fail:
            raise self.fail
        return [proc('ООО "РОМАШКА" 123456, Г. МОСКВА'),
                proc('ООО "РОМАШКА" 123456, Г. МОСКВА', ended="01.02.2023, ст. 46"),
                proc('ООО "РОМАШКА-ПЛЮС" Г. ТВЕРЬ')]

    async def search_person(self, last, first, patronymic):
        self.asked.append(("person", last, first, patronymic))
        return []


def test_normalize_name():
    assert normalize_name('Общество с ограниченной ответственностью «Ромашка»') == "РОМАШКА"
    assert normalize_name('ООО "РОМАШКА-ПЛЮС"') == "РОМАШКА ПЛЮС"


def test_full_check_by_ogrn():
    rnp, fssp = FakeRnp(), FakeFssp()
    pb = FakePb({"inn": "7707083893", "ogrn": "1027700132195", "namec": 'ООО "РОМАШКА"', "token": "t"})
    report = asyncio.run(CompanyChecker(pb, rnp, fssp).check("1027700132195"))

    assert report["inn"] == "7707083893" and rnp.asked == ["7707083893"]
    assert fssp.asked == [("legal", 'ООО "РОМАШКА"')]
    procs = report["sources"]["fssp"]["proceedings"]
    assert len(procs) == 2   # «РОМАШКА-ПЛЮС» отброшена
    assert report["flags"] == [
        "В реестре недобросовестных поставщиков: 1 действующая запись",
        "Исполнительные производства: 1 действующее, остаток долга 1 000.00 руб.",
    ]
    text = format_report(report)
    assert "44-ФЗ № 23000123-22, действует, включён 12.03.2022" in text
    assert "Всего 2, действующих 1" in text


def test_sources_fail_independently():
    report = asyncio.run(CompanyChecker(FakePb(), FakeRnp(), FakeFssp()).check("7707083893"))
    src = report["sources"]
    assert src["pbnalog"]["status"] == "not_found"
    assert src["rnp"]["status"] == "ok"          # ИНН есть в запросе
    assert src["fssp"]["status"] == "skipped"    # названия нет

    pb = FakePb({"inn": "7707083893", "namec": "ООО РОМАШКА"})
    report = asyncio.run(CompanyChecker(pb, FakeRnp(), FakeFssp(fail=CaptchaRequired("x"))).check("7707083893"))
    assert report["sources"]["fssp"]["error_type"] == "CaptchaRequired"
    assert "капча не пройдена" in format_report(report)


def test_ip_searched_by_fio():
    fssp = FakeFssp()
    pb = FakePb({"inn": "500100732259", "namec": "Иванов Иван Иванович"})
    asyncio.run(CompanyChecker(pb, FakeRnp(), fssp).check("500100732259"))
    assert fssp.asked == [("person", "ИВАНОВ", "ИВАН", "ИВАНОВИЧ")]

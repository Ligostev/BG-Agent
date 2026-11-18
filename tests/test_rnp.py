import asyncio

import httpx
import pytest

from rnp import RnpClient, parse_results
from rnp.client import BASE_URL, SEARCH_PATH


def entry(number, law, inn, name, dates, extra=""):
    date_html = "".join(f'<div class="col-6"><div class="data-block__title">{k}</div>'
                        f'<div class="data-block__value">{v}</div></div>' for k, v in dates.items())
    return f"""
<div class="search-registry-entry-block box-shadow-search-input">
 <div class="row no-gutters registry-entry__form mr-0">
  <div class="col-8 pr-0 mr-21px">
   <div class="d-flex registry-entry__header-top">
    <div class="registry-entry__header-top__title text-truncate">{law}</div>
   </div>
   <div class="registry-entry__header-mid__number">
    <a href="/epz/dishonestsupplier/view/info.html?id={number}" target="_blank">№ {number}</a>
   </div>
   <div class="registry-entry__body">
    <div class="registry-entry__body-block">
     <div class="registry-entry__body-title">Наименование (ФИО) недобросовестного поставщика</div>
     <div class="registry-entry__body-value">{name}</div>
    </div>
    <div class="registry-entry__body-block">
     <div class="registry-entry__body-title">ИНН (аналог ИНН)</div>
     <div class="registry-entry__body-value"> {inn} </div>
    </div>
    {extra}
   </div>
  </div>
  <div class="col d-flex flex-column registry-entry__right-block b-left ">
   <div class="data-block mt-auto"><div class="row">{date_html}</div></div>
  </div>
 </div>
</div>"""


PAGE = "<html><body>" + entry(
    "23000123-22", "44-ФЗ", "7707083893", 'ООО "РОМАШКА"',
    {"Включено": "12.03.2022", "Обновлено": "12.03.2022"},
    extra='<div class="registry-entry__body-block"><div class="registry-entry__body-title">'
          'Планируемая дата исключения</div><div class="registry-entry__body-value">12.03.2024</div></div>',
) + entry(
    "21000999-19", "223-ФЗ", "7707083893", 'ООО "РОМАШКА"',
    {"Включено": "01.02.2019", "Исключено": "01.02.2021"},
) + entry(
    "22000555-20", "44-ФЗ", "500100732259", "Иванов Иван Иванович", {"Включено": "05.05.2020"},
) + "</body></html>"


def test_parse_results():
    entries = parse_results(PAGE)
    assert [e.number for e in entries] == ["23000123-22", "21000999-19", "22000555-20"]
    first = entries[0]
    assert first.law == "44-ФЗ" and first.inn == "7707083893" and first.name == 'ООО "РОМАШКА"'
    assert first.url == BASE_URL + "/epz/dishonestsupplier/view/info.html?id=23000123-22"
    assert first.dates == {"Включено": "12.03.2022", "Обновлено": "12.03.2022"}
    assert first.active          # планируемая дата исключения не значит, что исключён
    assert not entries[1].active
    assert parse_results("<html>Поиск не дал результатов</html>") == []


def test_check_filters_by_inn():
    seen = []

    def site(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == SEARCH_PATH
        return httpx.Response(200, text=PAGE)

    async def go():
        http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(site))
        async with RnpClient(http=http, min_interval=0) as rnp:
            return await rnp.check("7707083893")

    entries = asyncio.run(go())
    assert [e.number for e in entries] == ["23000123-22", "21000999-19"]
    assert seen[0].url.params["searchString"] == "7707083893"
    with pytest.raises(ValueError):
        asyncio.run(RnpClient(http=httpx.AsyncClient()).check("7707083894"))

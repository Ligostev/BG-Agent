from pbnalog.validators import classify, is_valid_inn, is_valid_ogrn


def test_inn():
    assert is_valid_inn("7707083893")      # ПАО Сбербанк
    assert is_valid_inn("500100732259")
    assert not is_valid_inn("7707083894")
    assert not is_valid_inn("123")


def test_ogrn():
    assert is_valid_ogrn("1027700132195")  # ПАО Сбербанк
    assert is_valid_ogrn("304500116000157")
    assert not is_valid_ogrn("1027700132196")


def test_classify():
    assert classify("7707083893") == "inn_ul"
    assert classify(" 5001 0073 2259 ") == "inn_fl"
    assert classify("1027700132195") == "ogrn"
    assert classify("304500116000157") == "ogrnip"
    assert classify("ромашка") is None

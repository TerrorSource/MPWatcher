from datetime import datetime, time

import app


# --- parse_price_to_cents ----------------------------------------------------

def test_price_from_api_cents():
    assert app.parse_price_to_cents({"priceCents": 125000}, "") == 125000


def test_price_display_with_cents():
    assert app.parse_price_to_cents({}, "€ 12,50") == 1250


def test_price_display_with_thousands_separator():
    assert app.parse_price_to_cents({}, "€ 1.250,00") == 125000


def test_price_display_without_cents_is_euros():
    # "€ 1.250" is 1250 euro, niet 1250 cent
    assert app.parse_price_to_cents({}, "€ 1.250") == 125000


def test_price_display_single_decimal():
    assert app.parse_price_to_cents({}, "€ 9,5") == 950


def test_price_bieden_gratis_is_none():
    assert app.parse_price_to_cents({}, "Bieden") is None
    assert app.parse_price_to_cents({}, "Gratis") is None
    assert app.parse_price_to_cents({}, "") is None


# --- parse_posted_at_to_dt ---------------------------------------------------

def test_posted_at_iso():
    assert app.parse_posted_at_to_dt("2026-01-05T12:30:00", None) == datetime(2026, 1, 5, 12, 30)


def test_posted_at_dutch_date():
    assert app.parse_posted_at_to_dt("5 mei 25", None) == datetime(2025, 5, 5)


def test_posted_at_fallback_first_seen():
    assert app.parse_posted_at_to_dt("onzin", "2026-02-01T08:00:00") == datetime(2026, 2, 1, 8, 0)


def test_posted_at_no_data():
    assert app.parse_posted_at_to_dt(None, None) == datetime.min


# --- is_in_sleep_window ------------------------------------------------------

def test_sleep_window_overnight():
    start, end = time(23, 0), time(7, 0)
    assert app.is_in_sleep_window(time(23, 30), start, end)
    assert app.is_in_sleep_window(time(3, 0), start, end)
    assert not app.is_in_sleep_window(time(12, 0), start, end)


def test_sleep_window_same_day():
    start, end = time(13, 0), time(15, 0)
    assert app.is_in_sleep_window(time(14, 0), start, end)
    assert not app.is_in_sleep_window(time(16, 0), start, end)


# --- blocklist ---------------------------------------------------------------

def test_blocklist_disabled_is_empty():
    settings = {"blocklist_enabled": False, "blocked_sellers": ["Gert"]}
    assert app.get_blocklist(settings) == set()


def test_blocklist_enabled_normalises():
    settings = {"blocklist_enabled": True, "blocked_sellers": [" Gert ", "ARNAUD", ""]}
    assert app.get_blocklist(settings) == {"gert", "arnaud"}


# --- _form_bool / _int_or_none ----------------------------------------------

def test_form_bool():
    assert app._form_bool("ja") is True
    assert app._form_bool(" JA ") is True
    assert app._form_bool("nee") is False
    assert app._form_bool(None) is False


def test_int_or_none():
    assert app._int_or_none("5") == 5
    assert app._int_or_none("") is None
    assert app._int_or_none(None) is None
    assert app._int_or_none("abc") is None


# --- _extract_posted_at ------------------------------------------------------

def test_extract_posted_at_epoch_millis():
    item = {"date": 1767225600000}  # 2026-01-01 (ms)
    assert app._extract_posted_at(item).startswith("2026-01-01")


def test_extract_posted_at_string():
    assert app._extract_posted_at({"date": "2026-03-01"}) == "2026-03-01"


# --- build_search_url --------------------------------------------------------

def test_build_search_url_encodes_term():
    settings = dict(app.DEFAULT_SETTINGS)
    url = app.build_search_url("lego & duplo #5", settings)
    assert "/q/lego+%26+duplo+%235/" in url


def test_build_search_url_radius_postcode():
    settings = dict(app.DEFAULT_SETTINGS, postcode="1234AB", radius_km="10")
    url = app.build_search_url("fiets", settings)
    assert "distanceMeters:10000" in url
    assert "postcode:1234AB" in url

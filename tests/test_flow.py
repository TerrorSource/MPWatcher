"""Integratietests voor de zoek-flow (met gemockte netwerkcalls) en de
v20-functionaliteit: titelfilters, stille eerste run, digest, locatie,
foutafhandeling, CSRF-check en health-cache."""
from datetime import datetime, timedelta

import pytest

import app


def _ad(ad_id, title, cents, seller="", location="", url=None):
    return {
        "ad_id": ad_id, "title": title,
        "price": app.format_cents(cents) if cents is not None else "Bieden",
        "price_cents": cents,
        "url": url or f"https://example.com/{ad_id}", "image_url": "",
        "posted_at": "", "seller": seller, "location": location,
    }


@pytest.fixture()
def telegram_log(monkeypatch):
    """Vangt alle Telegram-berichten op als (method, payload)."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        app, "_telegram_post",
        lambda method, payload, settings, label="": sent.append((method, payload)) or True,
    )
    return sent


@pytest.fixture()
def keyword():
    app.init_db()
    kid = app.add_keyword_row("flow-test", 15, None, None, 5)
    yield app.get_keyword(kid)
    app.delete_keyword_row(kid)
    app.reset_results_for_keyword(kid)


TELEGRAM_SETTINGS = {"telegram_bot_id": "bot", "telegram_chat_id": "chat"}


def _settings(**overrides):
    s = app.load_settings()
    s.update(TELEGRAM_SETTINGS)
    s.update(overrides)
    return s


# --- eerste run stil, daarna melden ------------------------------------------

def test_first_run_is_silent_then_notifies(monkeypatch, telegram_log, keyword):
    monkeypatch.setattr(app, "fetch_market_results", lambda t, s, l: [_ad("a1", "Fiets", 10000)])

    total, new = app.run_search_for_keyword(keyword, _settings(), manual=False)
    assert (total, new) == (1, 1)
    assert telegram_log == [], "eerste run mag geen Telegram sturen"

    app.update_keyword_fields(keyword["id"], last_run_at=datetime.now().isoformat(timespec="seconds"))
    kw = app.get_keyword(keyword["id"])

    monkeypatch.setattr(app, "fetch_market_results", lambda t, s, l: [_ad("a1", "Fiets", 10000), _ad("a2", "Nieuwe fiets", 12000, location="Utrecht")])
    total, new = app.run_search_for_keyword(kw, _settings(), manual=False)
    assert (total, new) == (2, 1)
    assert len(telegram_log) == 1
    method, payload = telegram_log[0]
    assert method == "sendMessage"
    assert "Nieuwe fiets" in payload["text"]
    assert "Plaats = Utrecht" in payload["text"]


# --- titelfilters ------------------------------------------------------------

def test_title_filters():
    assert app.title_passes_filters("Gezocht: startbewijs marathon", None, "gezocht, gevraagd") is False
    assert app.title_passes_filters("Startbewijs marathon Berlijn", None, "gezocht") is True
    assert app.title_passes_filters("Marathon Berlijn", "startbewijs, ticket", None) is False
    assert app.title_passes_filters("Ticket marathon Berlijn", "startbewijs, ticket", None) is True
    assert app.title_passes_filters("Ticket", "", "") is True
    assert app.parse_terms(" Gezocht ,, gevraagd, gezocht ") == ["gezocht", "gevraagd"]
    assert app.normalize_terms("A, b ,A") == "a, b"


def test_exclude_terms_applied_in_search(monkeypatch, telegram_log, keyword):
    app.update_keyword_fields(keyword["id"], exclude_terms="gezocht", last_run_at=datetime.now().isoformat(timespec="seconds"))
    kw = app.get_keyword(keyword["id"])
    monkeypatch.setattr(app, "fetch_market_results", lambda t, s, l: [
        _ad("g1", "Gezocht: ticket marathon", 5000),
        _ad("g2", "Ticket marathon te koop", 5000),
    ])
    total, new = app.run_search_for_keyword(kw, _settings(), manual=False)
    assert (total, new) == (1, 1)
    assert [r["title"] for r in app.get_results_for_keyword(kw["id"])] == ["Ticket marathon te koop"]


# --- blocklist na verrijking + dedup + prijsverlaging -------------------------

def test_blocklist_after_enrichment_and_price_drop(monkeypatch, telegram_log, keyword):
    app.update_keyword_fields(keyword["id"], last_run_at=datetime.now().isoformat(timespec="seconds"))
    kw = app.get_keyword(keyword["id"])
    settings = _settings(blocklist_enabled=True, blocked_sellers=["Gert"], price_drop_alerts=True)

    monkeypatch.setattr(app, "fetch_seller_from_ad_page", lambda url: "Gert" if url.endswith("/b1") else "Piet")
    monkeypatch.setattr(app, "fetch_market_results", lambda t, s, l: [_ad("b1", "Van Gert", 5000), _ad("b2", "Van Piet", 8000)])
    total, new = app.run_search_for_keyword(kw, settings, manual=False)
    assert (total, new) == (2, 1), "geblokkeerde verkoper (alleen via HTML-fallback bekend) moet eruit"
    assert len(telegram_log) == 1

    # Prijsverlaging op bekende advertentie -> aparte melding, geen 'nieuw'
    telegram_log.clear()
    monkeypatch.setattr(app, "fetch_market_results", lambda t, s, l: [_ad("b2", "Van Piet", 6000)])
    total, new = app.run_search_for_keyword(kw, settings, manual=False)
    assert new == 0
    assert len(telegram_log) == 1
    assert "Prijsverlaging" in telegram_log[0][1]["text"]
    assert "€ 80,00 → € 60,00" in telegram_log[0][1]["text"]


# --- digest ------------------------------------------------------------------

def test_digest_when_many_new_ads(monkeypatch, telegram_log, keyword):
    app.update_keyword_fields(keyword["id"], last_run_at=datetime.now().isoformat(timespec="seconds"))
    kw = app.get_keyword(keyword["id"])
    ads = [_ad(f"d{i}", f"Advertentie {i}", 1000 * i, location="Zwolle") for i in range(1, 7)]
    monkeypatch.setattr(app, "fetch_market_results", lambda t, s, l: ads)

    total, new = app.run_search_for_keyword(kw, _settings(telegram_digest_threshold=5), manual=False)
    assert new == 6
    assert len(telegram_log) == 1, "boven de drempel: één samenvattend bericht"
    text = telegram_log[0][1]["text"]
    assert "6 nieuwe advertenties" in text
    assert "Advertentie 6" in text and "Zwolle" in text

    # Drempel 0 = altijd losse meldingen
    app.reset_results_for_keyword(kw["id"])
    telegram_log.clear()
    app.run_search_for_keyword(kw, _settings(telegram_digest_threshold=0), manual=False)
    assert len(telegram_log) == 6


# --- foutafhandeling ---------------------------------------------------------

def test_marketplace_error_recorded_on_manual_search(monkeypatch, keyword):
    def boom(term, settings, limit):
        raise app.MarketplaceError("www.marktplaats.nl weigert de zoekopdracht (HTTP 429)")
    monkeypatch.setattr(app, "fetch_market_results", boom)

    client = app.app.test_client()
    resp = client.post(f"/keyword/{keyword['id']}/manual", follow_redirects=True)
    assert resp.status_code == 200
    assert "mislukt" in resp.get_data(as_text=True)
    assert "HTTP 429" in app.get_keyword(keyword["id"])["last_error"]

    # Succesvolle run wist de fout weer
    monkeypatch.setattr(app, "fetch_market_results", lambda t, s, l: [])
    client.post(f"/keyword/{keyword['id']}/manual")
    assert app.get_keyword(keyword["id"])["last_error"] == ""


def test_add_keyword_runs_silent_seed(monkeypatch, telegram_log):
    app.init_db()
    monkeypatch.setattr(app, "fetch_market_results", lambda t, s, l: [_ad("s1", "Seed", 1000), _ad("s2", "Seed 2", 2000)])
    client = app.app.test_client()
    resp = client.post("/keyword/add", data={"term": "seed-test", "exclude_terms": "Gezocht, gevraagd"}, follow_redirects=True)
    assert "2 bestaande advertenties stil opgeslagen" in resp.get_data(as_text=True)
    kw = [k for k in app.load_keywords() if k["term"] == "seed-test"][0]
    assert kw["exclude_terms"] == "gezocht, gevraagd"
    assert kw["last_run_at"] != "Nooit"
    assert app.get_result_counts()[kw["id"]] == 2
    assert telegram_log == []
    client.post(f"/keyword/{kw['id']}/delete")


# --- volgende run / status ---------------------------------------------------

def test_compute_next_run_and_format():
    now = datetime(2026, 9, 13, 12, 0, 0)
    settings = dict(app.DEFAULT_SETTINGS, default_interval_minutes=15)
    kw = {"interval_minutes": 15, "last_run_at": (now - timedelta(minutes=5)).isoformat()}
    assert app.compute_next_run(kw, settings, now) == now + timedelta(minutes=10)
    assert app.format_next_run(now + timedelta(minutes=10), now) == "over 10 min"
    assert app.format_next_run(now - timedelta(minutes=1), now) == "nu"
    assert app.format_next_run(None, now) == "wacht op eerste run"
    # Slaapstand: interval < 1 uur wordt 1 uur
    night = datetime(2026, 9, 13, 2, 0, 0)
    sleepy = dict(settings, sleep_mode=True, sleep_start="23:00", sleep_end="07:00")
    kw_night = {"interval_minutes": 15, "last_run_at": night.isoformat()}
    assert app.compute_next_run(kw_night, sleepy, night) == night + timedelta(hours=1)


def test_scheduler_status_when_worker_disabled():
    assert app.get_scheduler_status()["state"] == "off"


def test_index_shows_status_and_filters(keyword):
    app.update_keyword_fields(keyword["id"], last_error="HTTP 403 test", exclude_terms="gezocht")
    html = app.app.test_client().get("/").get_data(as_text=True)
    assert "Scheduler:" in html
    assert "laatste zoekactie mislukt" in html
    assert 'name="exclude_terms"' in html and 'value="gezocht"' in html
    assert "volgende:" in html
    assert "confirm(" in html


# --- CSRF / health -----------------------------------------------------------

def test_cross_site_post_is_rejected(keyword):
    client = app.app.test_client()
    resp = client.post(f"/keyword/{keyword['id']}/delete", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert app.get_keyword(keyword["id"]) is not None

    resp = client.post(f"/keyword/{keyword['id']}/edit", data={"interval": "20"}, headers={"Origin": "http://localhost"})
    assert resp.status_code == 302
    assert app.get_keyword(keyword["id"])["interval_minutes"] == 20


def test_health_write_check_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_config_writable", lambda: calls.append(1) or True)
    monkeypatch.setattr(app, "_config_check", None)
    client = app.app.test_client()
    client.get("/health")
    client.get("/health")
    assert len(calls) == 1, "tweede aanroep binnen de TTL mag de schrijftest niet herhalen"


# --- locatie-extractie -------------------------------------------------------

def test_extract_location():
    assert app._extract_location({"location": {"cityName": "Utrecht"}}) == "Utrecht"
    assert app._extract_location({"location": "Zwolle"}) == "Zwolle"
    assert app._extract_location({"locationName": "Amsterdam"}) == "Amsterdam"
    assert app._extract_location({}) == ""


def test_find_price_drops_single_query(keyword):
    app.insert_new_ads(keyword["id"], [_ad("p1", "A", 1000), _ad("p2", "B", 2000)])
    drops = app.find_price_drops(keyword["id"], [_ad("p1", "A", 900), _ad("p2", "B", 2000), _ad("p3", "C", 100)])
    assert [(d[0]["ad_id"], d[1], d[2]) for d in drops] == [("p1", 1000, 900)]

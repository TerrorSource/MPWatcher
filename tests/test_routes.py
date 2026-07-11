import pytest

import app


@pytest.fixture()
def client():
    app.init_db()
    return app.app.test_client()


def _first_keyword_id():
    kws = app.load_keywords()
    assert kws, "verwachtte minstens één zoekwoord"
    return kws[-1]["id"]


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert "version" in data


def test_health_unhealthy_when_config_readonly(client, monkeypatch):
    monkeypatch.setattr(app, "_config_writable", lambda: False)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "unhealthy"


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_config_renders(client):
    resp = client.get("/config")
    assert resp.status_code == 200


def test_add_edit_delete_keyword(client):
    resp = client.post("/keyword/add", data={"term": "testfiets", "min_price": "50", "max_price": ""})
    assert resp.status_code == 302
    kid = _first_keyword_id()

    kw = app.get_keyword(kid)
    assert kw["term"] == "testfiets"
    assert kw["min_price"] == 50
    assert kw["max_price"] is None
    assert kw["last_run_at"] == "Nooit"

    # Bewerken: interval en max_price zetten, min_price leegmaken
    resp = client.post(
        f"/keyword/{kid}/edit",
        data={"term": "testfiets", "interval": "30", "min_price": "", "max_price": "200", "limit_per_run": "10"},
    )
    assert resp.status_code == 302
    kw = app.get_keyword(kid)
    assert kw["interval_minutes"] == 30
    assert kw["min_price"] is None
    assert kw["max_price"] == 200
    assert kw["limit_per_run"] == 10

    # Verwijderen
    resp = client.post(f"/keyword/{kid}/delete")
    assert resp.status_code == 302
    assert app.get_keyword(kid) is None


def test_add_keyword_empty_term_rejected(client):
    before = len(app.load_keywords())
    resp = client.post("/keyword/add", data={"term": "   "})
    assert resp.status_code == 302
    assert len(app.load_keywords()) == before


def test_flash_message_visible_after_add(client):
    client.post("/keyword/add", data={"term": "flashtest"})
    resp = client.get("/")
    assert "flashtest" in resp.get_data(as_text=True)
    assert "toegevoegd" in resp.get_data(as_text=True)
    client.post(f"/keyword/{_first_keyword_id()}/delete")


def test_blocklist_save_and_add(client):
    resp = client.post(
        "/blocklist/save",
        data={"blocklist_enabled": "ja", "blocked_sellers_text": "Gert\ngert\nArnaud\n"},
    )
    assert resp.status_code == 302
    settings = app.load_settings()
    assert settings["blocklist_enabled"] is True
    assert settings["blocked_sellers"] == ["Gert", "Arnaud"]

    resp = client.post("/blocklist/add", data={"seller": "Henk", "keyword_id": ""})
    assert resp.status_code == 302
    settings = app.load_settings()
    assert "Henk" in settings["blocked_sellers"]

    # opruimen
    client.post("/blocklist/save", data={"blocklist_enabled": "nee", "blocked_sellers_text": ""})


def test_settings_partial_save_keeps_other_keys(client):
    client.post("/config/telegram", data={"telegram_bot_id": "bot123", "telegram_chat_id": "chat456", "manual_telegram": "ja"})
    client.post(
        "/config/timer",
        data={
            "marketplace": "2dehands",
            "default_interval_minutes": "20",
            "default_limit_per_run": "8",
            "sleep_mode": "ja",
            "sleep_start": "22:00",
            "sleep_end": "06:00",
            "postcode": "1234AB",
            "radius_km": "10",
        },
    )
    settings = app.load_settings()
    # Telegram-instellingen niet overschreven door timer-save
    assert settings["telegram_bot_id"] == "bot123"
    assert settings["manual_telegram"] is True
    assert settings["marketplace"] == "2dehands"
    assert settings["default_interval_minutes"] == 20
    assert settings["sleep_mode"] is True


def test_results_dedup_across_keywords(client):
    client.post("/keyword/add", data={"term": "dedup-a"})
    kid_a = _first_keyword_id()
    client.post("/keyword/add", data={"term": "dedup-b"})
    kid_b = _first_keyword_id()

    ad = {
        "ad_id": "m123", "title": "Testad", "price": "€ 10,00", "price_cents": 1000,
        "url": "https://example.com/a", "image_url": "", "posted_at": "", "seller": "",
    }
    inserted_a = app.insert_new_ads(kid_a, [dict(ad)])
    assert inserted_a[0]["_seen_elsewhere"] is False

    inserted_b = app.insert_new_ads(kid_b, [dict(ad)])
    assert inserted_b[0]["_seen_elsewhere"] is True

    client.post(f"/keyword/{kid_a}/delete")
    client.post(f"/keyword/{kid_b}/delete")


def test_migration_preserves_keyword_ids(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(app, "DB_FILE", tmp_path / "results.db")
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(app, "KEYWORDS_FILE", tmp_path / "keywords.json")

    # Niet-opeenvolgende ids, zoals na verwijderde zoekwoorden in oude versies
    (tmp_path / "keywords.json").write_text(json.dumps([
        {"id": 2, "term": "alfa", "last_run_at": "Nooit"},
        {"id": 7, "term": "beta", "last_run_at": "2025-12-01T10:00:00"},
        {"id": 7, "term": "dubbel-id", "last_run_at": "Nooit"},
    ]), encoding="utf-8")

    app.init_db()
    kws = {k["term"]: k for k in app.load_keywords()}
    assert kws["alfa"]["id"] == 2
    assert kws["beta"]["id"] == 7
    assert kws["dubbel-id"]["id"] not in (2, 7)  # botsende id krijgt een nieuwe
    assert kws["beta"]["last_run_at"] == "2025-12-01T10:00:00"


def test_price_drop_detection(client):
    client.post("/keyword/add", data={"term": "prijsdaling-test"})
    kid = _first_keyword_id()

    ad = {
        "ad_id": "pd1", "title": "Fiets", "price": "€ 250,00", "price_cents": 25000,
        "url": "https://example.com/pd1", "image_url": "", "posted_at": "", "seller": "",
    }
    app.insert_new_ads(kid, [dict(ad)])

    # Zelfde prijs -> geen daling
    assert app.find_price_drops(kid, [dict(ad)]) == []

    # Lagere prijs -> daling gedetecteerd
    cheaper = dict(ad, price="€ 195,00", price_cents=19500)
    drops = app.find_price_drops(kid, [cheaper])
    assert len(drops) == 1
    _, old_c, new_c = drops[0]
    assert (old_c, new_c) == (25000, 19500)

    # Hogere prijs of onbekende prijs -> geen melding
    assert app.find_price_drops(kid, [dict(ad, price_cents=30000)]) == []
    assert app.find_price_drops(kid, [dict(ad, price_cents=None)]) == []

    # Na update_known_ads is de opgeslagen prijs ververst -> zelfde daling niet nogmaals
    app.update_known_ads(kid, [cheaper])
    assert app.find_price_drops(kid, [cheaper]) == []

    client.post(f"/keyword/{kid}/delete")


def test_result_counts(client):
    client.post("/keyword/add", data={"term": "count-test"})
    kid = _first_keyword_id()
    assert app.get_result_counts().get(kid, 0) == 0

    ads = [
        {"ad_id": f"c{i}", "title": f"Ad {i}", "price": "", "price_cents": None,
         "url": f"https://example.com/c{i}", "image_url": "", "posted_at": "", "seller": ""}
        for i in range(3)
    ]
    app.insert_new_ads(kid, ads)
    assert app.get_result_counts()[kid] == 3

    client.post(f"/keyword/{kid}/delete")


def test_format_relative_time():
    from datetime import datetime, timedelta
    now = datetime.now()
    assert app.format_relative_time("Nooit") == "Nooit"
    assert app.format_relative_time("") == "Nooit"
    assert app.format_relative_time(now.isoformat(timespec="seconds")) == "zojuist"
    assert app.format_relative_time((now - timedelta(minutes=5)).isoformat(timespec="seconds")) == "5 min geleden"
    assert app.format_relative_time((now - timedelta(hours=3)).isoformat(timespec="seconds")) == "3 uur geleden"
    assert app.format_relative_time((now - timedelta(days=1, hours=1)).isoformat(timespec="seconds")) == "gisteren"
    assert app.format_relative_time((now - timedelta(days=3)).isoformat(timespec="seconds")) == "3 dagen geleden"
    assert app.format_relative_time("onzin") == "onzin"


def test_format_cents():
    assert app.format_cents(19500) == "€ 195,00"
    assert app.format_cents(950) == "€ 9,50"

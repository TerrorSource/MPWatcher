"""Routetests: zoekwoorden, blocklists, categorie, resultaten, config, health, CSRF."""
import json

from mpwatcher import db, marketplace, web
from mpwatcher.config import MarketplaceError


def _last_keyword_id():
    kws = db.load_keywords()
    assert kws, "verwachtte minstens één zoekwoord"
    return kws[-1]["id"]


def _ad(ad_id, title, cents, seller="", location=""):
    return {
        "ad_id": ad_id, "title": title, "price": f"€ {cents/100:.2f}".replace(".", ","),
        "price_cents": cents, "url": f"https://example.com/{ad_id}", "image_url": "",
        "posted_at": "", "seller": seller, "location": location,
    }


# --- basis -------------------------------------------------------------------

def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok" and "version" in data


def test_health_unhealthy_when_config_readonly(client, monkeypatch):
    monkeypatch.setattr(db, "config_writable", lambda: False)
    monkeypatch.setattr(web, "_config_check", None)
    resp = client.get("/health")
    assert resp.status_code == 503


def test_health_write_check_is_cached(client, monkeypatch):
    calls = []
    monkeypatch.setattr(db, "config_writable", lambda: calls.append(1) or True)
    monkeypatch.setattr(web, "_config_check", None)
    client.get("/health")
    client.get("/health")
    assert len(calls) == 1


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/config").status_code == 200


# --- zoekwoorden -------------------------------------------------------------

def test_add_edit_delete_keyword(client):
    resp = client.post("/keyword/add", data={"term": "testfiets", "min_price": "50", "max_price": ""})
    assert resp.status_code == 302
    kid = _last_keyword_id()

    kw = db.get_keyword(kid)
    assert kw["term"] == "testfiets" and kw["min_price"] == 50 and kw["max_price"] is None
    assert kw["last_run_at"] != "Nooit"  # toevoegen draait direct een stille eerste run

    resp = client.post(
        f"/keyword/{kid}/edit",
        data={"term": "testfiets", "interval": "30", "min_price": "", "max_price": "200",
              "limit_per_run": "10", "exclude_terms": "Gezocht, gevraagd", "include_terms": ""},
    )
    assert resp.status_code == 302
    kw = db.get_keyword(kid)
    assert kw["interval_minutes"] == 30 and kw["min_price"] is None and kw["max_price"] == 200
    assert kw["limit_per_run"] == 10 and kw["exclude_terms"] == "gezocht, gevraagd"

    assert client.post(f"/keyword/{kid}/delete").status_code == 302
    assert db.get_keyword(kid) is None


def test_add_keyword_empty_term_rejected(client):
    before = len(db.load_keywords())
    assert client.post("/keyword/add", data={"term": "   "}).status_code == 302
    assert len(db.load_keywords()) == before


def test_flash_message_visible_after_add(client):
    client.post("/keyword/add", data={"term": "flashtest"})
    html = client.get("/").get_data(as_text=True)
    assert "flashtest" in html and "toegevoegd" in html
    client.post(f"/keyword/{_last_keyword_id()}/delete")


def test_manual_search_records_marketplace_error(client, monkeypatch):
    client.post("/keyword/add", data={"term": "fout-test"})
    kid = _last_keyword_id()

    def boom(term, settings, limit, category_id=None, category_parent_id=None):
        raise MarketplaceError("www.marktplaats.nl weigert de zoekopdracht (HTTP 429)")
    monkeypatch.setattr(marketplace, "fetch_market_results", boom)

    resp = client.post(f"/keyword/{kid}/manual", follow_redirects=True)
    assert "mislukt" in resp.get_data(as_text=True)
    assert "HTTP 429" in db.get_keyword(kid)["last_error"]

    monkeypatch.setattr(marketplace, "fetch_market_results", lambda *a, **k: [])
    client.post(f"/keyword/{kid}/manual")
    assert db.get_keyword(kid)["last_error"] == ""
    client.post(f"/keyword/{kid}/delete")


# --- categorie ---------------------------------------------------------------

def test_category_pick_and_clear(client, monkeypatch):
    client.post("/keyword/add", data={"term": "cat-test"})
    kid = _last_keyword_id()

    monkeypatch.setattr(marketplace, "fetch_categories", lambda term, settings: [
        {"id": 1984, "label": "Tickets en Kaartjes", "parent_id": None, "parent_label": "", "count": 0},
        {"id": 2002, "label": "Sport | Overige", "parent_id": 1984, "parent_label": "Tickets en Kaartjes", "count": 339},
    ])
    html = client.get(f"/keyword/{kid}/categories").get_data(as_text=True)
    assert "Sport | Overige" in html and "339 advertenties" in html

    client.post(f"/keyword/{kid}/category", data={
        "category_id": "2002", "category_parent_id": "1984", "category_label": "Tickets en Kaartjes › Sport | Overige",
    })
    kw = db.get_keyword(kid)
    assert (kw["category_id"], kw["category_parent_id"]) == (2002, 1984)
    assert "Sport | Overige" in client.get("/").get_data(as_text=True)

    client.post(f"/keyword/{kid}/category", data={"category_id": ""})
    kw = db.get_keyword(kid)
    assert kw["category_id"] is None and kw["category_label"] == ""
    client.post(f"/keyword/{kid}/delete")


def test_categories_page_handles_marketplace_error(client, monkeypatch):
    client.post("/keyword/add", data={"term": "cat-err"})
    kid = _last_keyword_id()

    def boom(term, settings):
        raise MarketplaceError("offline")
    monkeypatch.setattr(marketplace, "fetch_categories", boom)
    resp = client.get(f"/keyword/{kid}/categories")
    assert resp.status_code == 200 and "mislukt" in resp.get_data(as_text=True)
    client.post(f"/keyword/{kid}/delete")


# --- resultaten: zoeken en bladeren -----------------------------------------

def test_results_search_and_pagination(client, monkeypatch):
    monkeypatch.setattr(web, "RESULTS_PAGE_SIZE", 3)
    client.post("/keyword/add", data={"term": "page-test"})
    kid = _last_keyword_id()
    db.insert_new_ads(kid, [_ad(f"p{i}", f"Advertentie {i}", 1000 * i, location="Zwolle" if i % 2 else "Utrecht") for i in range(1, 8)])

    html = client.get(f"/keyword/{kid}/results").get_data(as_text=True)
    assert "1–3 van 7" in html and "Ouder →" in html and "← Nieuwer" not in html

    html = client.get(f"/keyword/{kid}/results?offset=6").get_data(as_text=True)
    assert "7–7 van 7" in html and "← Nieuwer" in html and "Ouder →" not in html

    html = client.get(f"/keyword/{kid}/results?q=Utrecht").get_data(as_text=True)
    assert "van 3" in html and "Wis filter" in html
    client.post(f"/keyword/{kid}/delete")


# --- blocklists --------------------------------------------------------------

def test_seller_blocklist_save_and_add(client):
    client.post("/blocklist/save", data={"blocklist_enabled": "ja", "blocked_sellers_text": "Gert\ngert\nArnaud\n"})
    s = db.load_settings()
    assert s["blocklist_enabled"] is True and s["blocked_sellers"] == ["Gert", "Arnaud"]

    client.post("/blocklist/add", data={"seller": "Henk", "keyword_id": ""})
    assert "Henk" in db.load_settings()["blocked_sellers"]
    client.post("/blocklist/save", data={"blocklist_enabled": "nee", "blocked_sellers_text": ""})


def test_title_blocklist_add_removes_existing_results(client):
    client.post("/keyword/add", data={"term": "title-block"})
    kid = _last_keyword_id()
    db.insert_new_ads(kid, [_ad("t1", "Gezocht: startbewijs Rotterdam", 5000), _ad("t2", "Startbewijs te koop", 6000)])

    resp = client.post("/blocklist/title/add", data={"title": "gezocht: startbewijs rotterdam", "keyword_id": str(kid)}, follow_redirects=True)
    assert resp.status_code == 200
    assert db.load_settings()["blocked_titles"] == ["gezocht: startbewijs rotterdam"]
    assert [r["title"] for r in db.get_results_for_keyword(kid)] == ["Startbewijs te koop"]

    # dubbel toevoegen (andere hoofdletters) verandert niets
    client.post("/blocklist/title/add", data={"title": "GEZOCHT: Startbewijs Rotterdam", "keyword_id": str(kid)})
    assert len(db.load_settings()["blocked_titles"]) == 1

    # via het tekstvak leegmaken
    client.post("/blocklist/titles/save", data={"blocked_titles_text": ""})
    assert db.load_settings()["blocked_titles"] == []
    client.post(f"/keyword/{kid}/delete")


# --- instellingen ------------------------------------------------------------

def test_settings_partial_save_keeps_other_keys(client):
    client.post("/config/telegram", data={"telegram_bot_id": "bot123", "telegram_chat_id": "chat456", "manual_telegram": "ja", "telegram_digest_threshold": "7"})
    client.post("/config/timer", data={
        "marketplace": "2dehands", "default_interval_minutes": "20", "default_limit_per_run": "8",
        "sleep_mode": "ja", "sleep_start": "22:00", "sleep_end": "06:00", "postcode": "1234AB",
        "radius_km": "10", "repost_dedup_days": "14",
    })
    s = db.load_settings()
    assert s["telegram_bot_id"] == "bot123" and s["manual_telegram"] is True and s["telegram_digest_threshold"] == 7
    assert s["marketplace"] == "2dehands" and s["default_interval_minutes"] == 20 and s["sleep_mode"] is True
    assert s["repost_dedup_days"] == 14


# --- data --------------------------------------------------------------------

def test_results_dedup_across_keywords(client):
    client.post("/keyword/add", data={"term": "dedup-a"})
    kid_a = _last_keyword_id()
    client.post("/keyword/add", data={"term": "dedup-b"})
    kid_b = _last_keyword_id()
    ad = _ad("m123", "Testad", 1000)
    assert db.insert_new_ads(kid_a, [dict(ad)])[0]["_seen_elsewhere"] is False
    assert db.insert_new_ads(kid_b, [dict(ad)])[0]["_seen_elsewhere"] is True
    client.post(f"/keyword/{kid_a}/delete")
    client.post(f"/keyword/{kid_b}/delete")


def test_price_drops_and_counts_and_recent(client):
    client.post("/keyword/add", data={"term": "prijs-test"})
    kid = _last_keyword_id()
    db.insert_new_ads(kid, [_ad("p1", "A", 1000), _ad("p2", "B", 2000)])
    drops = db.find_price_drops(kid, [_ad("p1", "A", 900), _ad("p2", "B", 2000), _ad("p3", "C", 100)])
    assert [(d[0]["ad_id"], d[1], d[2]) for d in drops] == [("p1", 1000, 900)]
    assert db.get_result_counts()[kid] == 2
    recent = db.get_recent_results(limit=5)
    assert recent and recent[0]["term"] == "prijs-test"
    client.post(f"/keyword/{kid}/delete")


def test_migration_preserves_keyword_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "results.db")
    monkeypatch.setattr(db, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(db, "KEYWORDS_FILE", tmp_path / "keywords.json")
    (tmp_path / "keywords.json").write_text(json.dumps([
        {"id": 2, "term": "alfa", "last_run_at": "Nooit"},
        {"id": 7, "term": "beta", "last_run_at": "2025-12-01T10:00:00"},
        {"id": 7, "term": "dubbel-id", "last_run_at": "Nooit"},
    ]), encoding="utf-8")
    db.init_db()
    kws = {k["term"]: k for k in db.load_keywords()}
    assert kws["alfa"]["id"] == 2 and kws["beta"]["id"] == 7
    assert kws["dubbel-id"]["id"] not in (2, 7)
    assert kws["beta"]["last_run_at"] == "2025-12-01T10:00:00"


# --- CSRF --------------------------------------------------------------------

def test_cross_site_post_is_rejected(client):
    client.post("/keyword/add", data={"term": "csrf-test"})
    kid = _last_keyword_id()
    resp = client.post(f"/keyword/{kid}/delete", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403 and db.get_keyword(kid) is not None
    resp = client.post(f"/keyword/{kid}/edit", data={"interval": "20"}, headers={"Origin": "http://localhost"})
    assert resp.status_code == 302 and db.get_keyword(kid)["interval_minutes"] == 20
    client.post(f"/keyword/{kid}/delete")


# --- v22 ---------------------------------------------------------------------

def test_duplicate_keyword_rejected(client):
    client.post("/keyword/add", data={"term": "Dubbel Test"})
    before = len(db.load_keywords())
    resp = client.post("/keyword/add", data={"term": "  dubbel test "}, follow_redirects=True)
    assert "bestaat al" in resp.get_data(as_text=True)
    assert len(db.load_keywords()) == before
    client.post(f"/keyword/{_last_keyword_id()}/delete")


def test_notifications_page(client):
    db.log_notification("new", None, "pagina-test", {"title": "Ad X", "url": "https://example.com/x", "price": "€ 1,00"}, sent=True)
    db.log_notification("suppressed", None, "pagina-test", {"title": "Ad Y"}, detail="herplaatsing van bekende advertentie")
    html = client.get("/notifications").get_data(as_text=True)
    assert "Ad X" in html and "verstuurd" in html
    assert "Ad Y" in html and "herplaatsing" in html and "Onderdrukt" in html


def test_config_skip_reserved_saved(client):
    client.post("/config/timer", data={"marketplace": "marktplaats", "default_interval_minutes": "15",
                                       "default_limit_per_run": "5", "skip_reserved": "ja", "sleep_mode": "nee"})
    assert db.load_settings()["skip_reserved"] is True
    client.post("/config/timer", data={"marketplace": "marktplaats", "skip_reserved": "nee", "sleep_mode": "nee"})
    assert db.load_settings()["skip_reserved"] is False

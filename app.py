import os
import json
import logging
import sqlite3
import threading
import time as time_module
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)

# ------------------------------------------------------------------------------
# Basisconfiguratie
# ------------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mpwatcher")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mpwatcher-dev")

BASE_DIR = Path(__file__).resolve().parent
# MPWATCHTER_CONFIG_DIR is de oude (typo) naam; fallback voor bestaande deployments
CONFIG_DIR = Path(
    os.environ.get("MPWATCHER_CONFIG_DIR")
    or os.environ.get("MPWATCHTER_CONFIG_DIR")
    or "/config"
)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Legacy JSON-bestanden; worden bij de eerste start eenmalig in de database
# geïmporteerd en daarna hernoemd naar *.imported.
SETTINGS_FILE = CONFIG_DIR / "settings.json"
KEYWORDS_FILE = CONFIG_DIR / "keywords.json"
DB_FILE = CONFIG_DIR / "results.db"

# Maximaal bewaarde resultaten per zoekwoord; oudere rijen worden opgeruimd.
MAX_RESULTS_PER_KEYWORD = 500

# /health meldt unhealthy als de scheduler zo lang geen teken van leven gaf.
SCHEDULER_STALE_SECONDS = 300

DEFAULT_SETTINGS = {
    "marketplace": "marktplaats",  # marktplaats | 2dehands
    "default_interval_minutes": 15,
    "default_limit_per_run": 5,

    "sleep_mode": False,
    "sleep_start": "23:00",
    "sleep_end": "07:00",

    "postcode": "",
    "radius_km": "alle",

    "telegram_bot_id": "",
    "telegram_chat_id": "",
    "manual_telegram": False,

    # Blocklist
    "blocklist_enabled": False,
    "blocked_sellers": [],
}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

# Eén sessie voor alle uitgaande HTTP-verkeer: hergebruikt TCP/TLS-verbindingen.
HTTP = requests.Session()
HTTP.headers["User-Agent"] = USER_AGENT


# ------------------------------------------------------------------------------
# Database
# ------------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword_id INTEGER NOT NULL,
                    ad_id TEXT NOT NULL,
                    title TEXT,
                    price TEXT,
                    url TEXT,
                    image_url TEXT,
                    seller TEXT,
                    first_seen_at TEXT,
                    posted_at TEXT,
                    posted_ts TEXT,
                    UNIQUE(keyword_id, ad_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term TEXT NOT NULL,
                    interval_minutes INTEGER,
                    min_price INTEGER,
                    max_price INTEGER,
                    limit_per_run INTEGER,
                    last_run_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            # Kolommen aanvullen voor databases uit oudere versies
            cols = [row[1] for row in conn.execute("PRAGMA table_info(results)")]
            for col in ("posted_at", "seller", "posted_ts"):
                if col not in cols:
                    conn.execute(f"ALTER TABLE results ADD COLUMN {col} TEXT")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_results_ad_id ON results(ad_id)"
            )

        _import_legacy_json(conn)
        _backfill_posted_ts(conn)
    finally:
        conn.close()


def _import_legacy_json(conn: sqlite3.Connection) -> None:
    """Eenmalige migratie van settings.json / keywords.json naar de database."""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.exception("settings.json kon niet gelezen worden tijdens migratie")
            data = {}

        if isinstance(data, dict):
            for key in ("sleep_mode", "manual_telegram", "blocklist_enabled"):
                if key in data:
                    data[key] = str(data[key]).strip().lower() == "ja"
            existing = {row["key"] for row in conn.execute("SELECT key FROM settings")}
            to_import = {
                k: v for k, v in data.items()
                if k in DEFAULT_SETTINGS and k not in existing
            }
            if to_import:
                with conn:
                    conn.executemany(
                        "INSERT INTO settings (key, value) VALUES (?, ?)",
                        [(k, json.dumps(v)) for k, v in to_import.items()],
                    )
        SETTINGS_FILE.rename(SETTINGS_FILE.parent / (SETTINGS_FILE.name + ".imported"))
        logger.info("settings.json geïmporteerd in de database")

    if KEYWORDS_FILE.exists():
        try:
            data = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8")) or []
        except Exception:
            logger.exception("keywords.json kon niet gelezen worden tijdens migratie")
            data = []

        if isinstance(data, dict):
            data = data.get("keywords") if isinstance(data.get("keywords"), list) else [data]

        has_keywords = conn.execute("SELECT 1 FROM keywords LIMIT 1").fetchone()
        if isinstance(data, list) and not has_keywords:
            rows = []
            for item in data:
                if not isinstance(item, dict):
                    item = {"term": str(item)}
                term = (item.get("term") or "").strip()
                if not term:
                    continue
                last_run = item.get("last_run_at")
                rows.append((
                    term,
                    _int_or_none(item.get("interval_minutes")),
                    _int_or_none(item.get("min_price")),
                    _int_or_none(item.get("max_price")),
                    _int_or_none(item.get("limit_per_run")),
                    None if (not last_run or last_run == "Nooit") else str(last_run),
                ))
            if rows:
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO keywords
                            (term, interval_minutes, min_price, max_price, limit_per_run, last_run_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
        KEYWORDS_FILE.rename(KEYWORDS_FILE.parent / (KEYWORDS_FILE.name + ".imported"))
        logger.info("keywords.json geïmporteerd in de database (%s zoekwoorden)", len(data))


def _backfill_posted_ts(conn: sqlite3.Connection) -> None:
    """Vul posted_ts (genormaliseerde sorteertijd) voor rijen uit oudere versies."""
    rows = conn.execute(
        "SELECT id, posted_at, first_seen_at FROM results WHERE posted_ts IS NULL"
    ).fetchall()
    if not rows:
        return
    updates = []
    for row in rows:
        dt = parse_posted_at_to_dt(row["posted_at"], row["first_seen_at"])
        updates.append((dt.isoformat(timespec="seconds"), row["id"]))
    with conn:
        conn.executemany("UPDATE results SET posted_ts = ? WHERE id = ?", updates)
    logger.info("posted_ts ingevuld voor %s bestaande resultaten", len(updates))


# ------------------------------------------------------------------------------
# Helpers: settings
# ------------------------------------------------------------------------------

def _form_bool(v) -> bool:
    """Formulier-selects sturen 'ja'/'nee'; intern werken we met booleans."""
    return str(v or "").strip().lower() == "ja"


def _int_or_none(v) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load_settings() -> dict:
    conn = _connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()

    merged = {
        k: (v.copy() if isinstance(v, (list, dict)) else v)
        for k, v in DEFAULT_SETTINGS.items()
    }
    for row in rows:
        if row["key"] not in DEFAULT_SETTINGS:
            continue
        try:
            merged[row["key"]] = json.loads(row["value"])
        except Exception:
            logger.warning("Instelling '%s' kon niet gelezen worden, default gebruikt", row["key"])

    merged["default_interval_minutes"] = _int_or_none(merged.get("default_interval_minutes")) or 15
    merged["default_limit_per_run"] = _int_or_none(merged.get("default_limit_per_run")) or 5

    mp = str(merged.get("marketplace") or "marktplaats").strip().lower()
    merged["marketplace"] = "2dehands" if mp in ("2dehands", "2dehands.be", "2dehandsbe") else "marktplaats"

    for key in ("sleep_mode", "manual_telegram", "blocklist_enabled"):
        merged[key] = bool(merged.get(key))

    if not isinstance(merged.get("blocked_sellers"), list):
        merged["blocked_sellers"] = []

    return merged


def save_settings(values: dict) -> None:
    """Sla (alleen) de meegegeven instellingen op; per key een upsert,
    zodat gelijktijdige schrijvers elkaars keys niet overschrijven."""
    if not values:
        return
    conn = _connect()
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                [(k, json.dumps(v)) for k, v in values.items()],
            )
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Helpers: keywords
# ------------------------------------------------------------------------------

def _row_to_keyword(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "term": row["term"],
        "interval_minutes": row["interval_minutes"],
        "min_price": row["min_price"],
        "max_price": row["max_price"],
        "limit_per_run": row["limit_per_run"],
        "last_run_at": row["last_run_at"] or "Nooit",
    }


def load_keywords() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM keywords ORDER BY id").fetchall()
    finally:
        conn.close()
    return [_row_to_keyword(r) for r in rows]


def get_keyword(keyword_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_keyword(row) if row else None


def add_keyword_row(term: str, interval_minutes, min_price, max_price, limit_per_run) -> int:
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO keywords (term, interval_minutes, min_price, max_price, limit_per_run, last_run_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (term, interval_minutes, min_price, max_price, limit_per_run),
            )
            return cur.lastrowid
    finally:
        conn.close()


def update_keyword_fields(keyword_id: int, **fields) -> bool:
    """Update alleen de meegegeven kolommen; scheduler (last_run_at) en UI-edits
    kunnen elkaar zo niet meer overschrijven."""
    if not fields:
        return get_keyword(keyword_id) is not None
    assignments = ", ".join(f"{col} = ?" for col in fields)
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                f"UPDATE keywords SET {assignments} WHERE id = ?",
                (*fields.values(), keyword_id),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def delete_keyword_row(keyword_id: int) -> bool:
    conn = _connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


def parse_time_str(value: str) -> time:
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except Exception:
        return time(23, 0)


def is_in_sleep_window(now_t: time, start: time, end: time) -> bool:
    if start < end:
        return start <= now_t < end
    return now_t >= start or now_t < end


# ------------------------------------------------------------------------------
# Marketplace helpers
# ------------------------------------------------------------------------------

def get_domain(settings: dict) -> str:
    return "www.2dehands.be" if (settings.get("marketplace") == "2dehands") else "www.marktplaats.nl"


def build_search_url(term: str, settings: dict) -> str:
    domain = get_domain(settings)
    postcode = (settings.get("postcode") or "").strip()
    radius_km = (settings.get("radius_km") or "alle").strip()

    query = quote_plus(term.strip())
    base = f"https://{domain}/q/{query}/#offeredSince:Altijd|sortBy:SORT_INDEX|sortOrder:DECREASING"

    if radius_km and radius_km != "alle":
        try:
            meters = int(radius_km) * 1000
            base += f"|distanceMeters:{meters}"
        except ValueError:
            pass

    if postcode:
        base += f"|postcode:{postcode}"

    return base


# ------------------------------------------------------------------------------
# posted_at parsing
# ------------------------------------------------------------------------------

def _format_epoch_to_str(v) -> str:
    try:
        ts = float(v)
    except Exception:
        return ""
    if ts > 10**12:
        ts = ts / 1000.0
    try:
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _extract_posted_at(item: dict) -> str:
    for key in ("date", "dateTime", "startTime", "startDateTime", "startDate", "postedAt"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            s = _format_epoch_to_str(v)
            if s:
                return s
        if isinstance(v, dict):
            for sub in ("value", "date", "iso", "iso8601"):
                sv = v.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()
                if isinstance(sv, (int, float)):
                    s = _format_epoch_to_str(sv)
                    if s:
                        return s

    for nk in ("dateInfo", "metadata", "timing"):
        nv = item.get(nk)
        if isinstance(nv, dict):
            for sub in ("date", "dateTime", "start", "value"):
                sv = nv.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()
                if isinstance(sv, (int, float)):
                    s = _format_epoch_to_str(sv)
                    if s:
                        return s
    return ""


def parse_posted_at_to_dt(posted_at: str | None, fallback_first_seen: str | None) -> datetime:
    if posted_at:
        s = posted_at.strip()
        if s:
            for sep in [",", "|"]:
                if sep in s:
                    s = s.split(sep, 1)[0].strip()
            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass

            month_map = {
                "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
            }
            try:
                parts = s.replace(".", "").split()
                if len(parts) >= 3:
                    day = int(parts[0])
                    mon_abbr = parts[1].lower()
                    year = int(parts[2])
                    if year < 100:
                        year += 2000
                    month = month_map.get(mon_abbr)
                    if month:
                        return datetime(year, month, day)
            except Exception:
                pass

    if fallback_first_seen:
        try:
            return datetime.fromisoformat(fallback_first_seen)
        except Exception:
            pass

    return datetime.min


# ------------------------------------------------------------------------------
# Prijs parsing
# ------------------------------------------------------------------------------

def parse_price_to_cents(price_info, price_display: str) -> Optional[int]:
    """Prijs in centen, bij voorkeur direct uit de API (priceCents).
    Fallback: weergavestring zoals '€ 1.250' of '€ 12,50' correct parsen.
    'Bieden'/'Gratis' e.d. leveren None op."""
    if isinstance(price_info, dict):
        cents = price_info.get("priceCents")
        if isinstance(cents, (int, float)) and cents > 0:
            return int(cents)

    if price_display:
        m = re.search(r"(\d[\d\.]*)(?:,(\d{1,2}))?", price_display)
        if m:
            try:
                euros = int(m.group(1).replace(".", ""))
                cents = int((m.group(2) or "0").ljust(2, "0"))
                return euros * 100 + cents
            except ValueError:
                pass

    return None


# ------------------------------------------------------------------------------
# Blocklist helpers
# ------------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    return (s or "").strip()


def get_blocklist(settings: dict) -> set[str]:
    if not settings.get("blocklist_enabled"):
        return set()
    names = settings.get("blocked_sellers") or []
    return {_norm_name(x).lower() for x in names if _norm_name(x)}


# ------------------------------------------------------------------------------
# Seller extraction (API + HTML fallback)
# ------------------------------------------------------------------------------

def _extract_seller_from_api_item(item: dict) -> str:
    direct_keys = (
        "sellerName", "seller", "vendor", "userName", "username",
        "advertiserName", "advertiser"
    )
    for k in direct_keys:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for sub in ("name", "displayName", "username", "userName", "sellerName"):
                sv = v.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()

    nested_candidates = (
        "sellerInformation", "contactInformation", "user", "account",
        "sellerInfo", "contact", "advertiser", "profile"
    )
    for nk in nested_candidates:
        nv = item.get(nk)
        if isinstance(nv, dict):
            for sub in ("name", "displayName", "username", "userName", "sellerName"):
                sv = nv.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()
    return ""


def _extract_seller_from_html(html: str) -> str:
    # 1) JSON-LD blocks
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE):
        blob = m.group(1).strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue

        # Sometimes it’s a list
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            seller = obj.get("seller")
            if isinstance(seller, dict):
                nm = seller.get("name")
                if isinstance(nm, str) and nm.strip():
                    return nm.strip()
            author = obj.get("author")
            if isinstance(author, dict):
                nm = author.get("name")
                if isinstance(nm, str) and nm.strip():
                    return nm.strip()

    # 2) Common preloaded state / inline JSON patterns
    patterns = [
        r'"sellerName"\s*:\s*"([^"]+)"',
        r'"displayName"\s*:\s*"([^"]+)"',
        r'"userName"\s*:\s*"([^"]+)"',
        r'"username"\s*:\s*"([^"]+)"',
        r'"advertiserName"\s*:\s*"([^"]+)"',
        r'"seller"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"',
        r'"account"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"',
    ]
    for pat in patterns:
        mm = re.search(pat, html, re.IGNORECASE)
        if mm and mm.group(1).strip():
            return mm.group(1).strip()

    return ""


def fetch_seller_from_ad_page(url: str) -> str:
    if not url:
        return ""
    try:
        resp = HTTP.get(url, timeout=8)
        resp.raise_for_status()
        return _extract_seller_from_html(resp.text)
    except Exception as exc:
        logger.warning("Seller ophalen mislukt voor %s: %s", url, exc)
        return ""


def enrich_ads_with_seller(ads: list[dict]) -> list[dict]:
    """
    Vul seller aan voor ads waar die leeg is (HTML fallback).
    Detailpagina's worden parallel opgehaald zodat een run niet
    minutenlang blokkeert bij veel advertenties.
    """
    todo = [
        ad for ad in ads
        if not (ad.get("seller") or "").strip() and (ad.get("url") or "").strip()
    ]
    if not todo:
        return ads

    with ThreadPoolExecutor(max_workers=4) as pool:
        sellers = pool.map(lambda ad: fetch_seller_from_ad_page(ad["url"].strip()), todo)
        for ad, seller in zip(todo, sellers):
            if seller:
                ad["seller"] = seller
    return ads


# ------------------------------------------------------------------------------
# Fetch results (API)
# ------------------------------------------------------------------------------

def fetch_market_results(term: str, settings: dict, limit: int) -> list[dict]:
    domain = get_domain(settings)
    postcode = (settings.get("postcode") or "").strip() or None
    radius_km = (settings.get("radius_km") or "alle").strip()

    distance_meters = None
    if radius_km and radius_km != "alle":
        try:
            distance_meters = int(radius_km) * 1000
        except Exception:
            distance_meters = None

    api_url = f"https://{domain}/lrp/api/search"
    params = {
        "query": term,
        "sortBy": "SORT_INDEX",
        "sortOrder": "DECREASING",
        "viewOptions": "list-view",
        "limit": limit,
        "offset": 0,
    }
    if postcode:
        params["postcode"] = postcode
    if distance_meters is not None:
        params["distanceMeters"] = distance_meters

    results: list[dict] = []

    try:
        resp = HTTP.get(api_url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        def find_listings(obj):
            if isinstance(obj, list):
                if obj and isinstance(obj[0], dict) and ("itemId" in obj[0] or "id" in obj[0]) and "title" in obj[0]:
                    return obj
                for item in obj:
                    res = find_listings(item)
                    if res is not None:
                        return res
            elif isinstance(obj, dict):
                for v in obj.values():
                    res = find_listings(v)
                    if res is not None:
                        return res
            return None

        listings = find_listings(data)

        if isinstance(listings, list):
            for item in listings[:limit]:
                ad_id = str(item.get("itemId") or item.get("id") or "")
                title = item.get("title") or ""
                price = ""
                url = ""
                image_url = ""
                posted_at = _extract_posted_at(item)
                seller = _extract_seller_from_api_item(item)

                price_info = item.get("priceInfo") or {}
                if "priceDisplay" in price_info:
                    price = price_info["priceDisplay"]
                elif "priceCents" in price_info:
                    cents = price_info["priceCents"]
                    if cents is not None:
                        price = f"€ {cents/100:.2f}".replace(".", ",")

                url_path = item.get("url") or item.get("vipUrl") or item.get("relativeUrl")
                if url_path:
                    if str(url_path).startswith("http"):
                        url = str(url_path)
                    else:
                        url = f"https://{domain}{url_path}"

                media = item.get("media") or {}
                if isinstance(media, dict):
                    imgs = media.get("images")
                    if isinstance(imgs, list) and imgs:
                        image_url = imgs[0].get("url") or ""
                elif isinstance(media, list) and media:
                    image_url = media[0].get("url") or ""

                if not ad_id or not url:
                    continue

                results.append(
                    {
                        "ad_id": ad_id,
                        "title": title,
                        "price": price,
                        "price_cents": parse_price_to_cents(price_info, price),
                        "url": url,
                        "image_url": image_url,
                        "posted_at": posted_at,
                        "seller": seller,
                    }
                )

    except Exception as exc:
        logger.warning("Zoekopdracht voor '%s' mislukt: %s", term, exc)
        return []

    return results[:limit]


# ------------------------------------------------------------------------------
# DB helpers: resultaten
# ------------------------------------------------------------------------------

def get_known_ad_ids(keyword_id: int) -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ad_id FROM results WHERE keyword_id = ?", (keyword_id,)
        ).fetchall()
    finally:
        conn.close()
    return {row["ad_id"] for row in rows}


def insert_new_ads(keyword_id: int, ads: list[dict]) -> list[dict]:
    """Voeg nieuwe advertenties toe. Markeert per ad of dezelfde advertentie al
    via een ander zoekwoord bekend was (dan geen dubbele Telegram-melding)."""
    if not ads:
        return []

    inserted: list[dict] = []
    conn = _connect()
    try:
        with conn:
            cur = conn.cursor()
            for ad in ads:
                now_iso = datetime.now().isoformat(timespec="seconds")
                posted_ts = parse_posted_at_to_dt(
                    ad.get("posted_at"), now_iso
                ).isoformat(timespec="seconds")

                seen_elsewhere = cur.execute(
                    "SELECT 1 FROM results WHERE ad_id = ? AND keyword_id != ? LIMIT 1",
                    (ad["ad_id"], keyword_id),
                ).fetchone() is not None

                try:
                    cur.execute(
                        """
                        INSERT INTO results
                            (keyword_id, ad_id, title, price, url, image_url, seller,
                             first_seen_at, posted_at, posted_ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            keyword_id, ad["ad_id"], ad.get("title", ""), ad.get("price", ""),
                            ad.get("url", ""), ad.get("image_url", ""), ad.get("seller", ""),
                            now_iso, ad.get("posted_at", ""), posted_ts,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue

                ad["_seen_elsewhere"] = seen_elsewhere
                inserted.append(ad)
    finally:
        conn.close()

    return inserted


def update_known_ads(keyword_id: int, ads: list[dict]) -> None:
    """Vul ontbrekende velden aan van reeds bekende advertenties met verse API-data."""
    if not ads:
        return

    conn = _connect()
    try:
        with conn:
            for ad in ads:
                posted_at = ad.get("posted_at", "")
                posted_ts = ""
                if posted_at:
                    dt = parse_posted_at_to_dt(posted_at, None)
                    if dt != datetime.min:
                        posted_ts = dt.isoformat(timespec="seconds")
                conn.execute(
                    """
                    UPDATE results
                    SET
                        title = COALESCE(NULLIF(?, ''), title),
                        price = COALESCE(NULLIF(?, ''), price),
                        url = COALESCE(NULLIF(?, ''), url),
                        image_url = CASE
                            WHEN (image_url IS NULL OR image_url = '') AND ? != '' THEN ?
                            ELSE image_url
                        END,
                        posted_at = CASE
                            WHEN (posted_at IS NULL OR posted_at = '') AND ? != '' THEN ?
                            ELSE posted_at
                        END,
                        posted_ts = CASE
                            WHEN (posted_at IS NULL OR posted_at = '') AND ? != '' THEN ?
                            ELSE posted_ts
                        END,
                        seller = CASE
                            WHEN (seller IS NULL OR seller = '') AND ? != '' THEN ?
                            ELSE seller
                        END
                    WHERE keyword_id = ? AND ad_id = ?
                    """,
                    (
                        ad.get("title", ""), ad.get("price", ""), ad.get("url", ""),
                        ad.get("image_url", ""), ad.get("image_url", ""),
                        posted_at, posted_at,
                        posted_ts, posted_ts,
                        ad.get("seller", ""), ad.get("seller", ""),
                        keyword_id, ad["ad_id"],
                    ),
                )
    finally:
        conn.close()


def get_results_for_keyword(keyword_id: int, limit: int = 200) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT title, price, url, image_url, seller, first_seen_at, posted_at
            FROM results
            WHERE keyword_id = ?
            ORDER BY COALESCE(posted_ts, first_seen_at) DESC
            LIMIT ?
            """,
            (keyword_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def reset_results_for_keyword(keyword_id: int) -> None:
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM results WHERE keyword_id = ?", (keyword_id,))
    finally:
        conn.close()


def prune_results_for_keyword(keyword_id: int) -> None:
    """Houd maximaal MAX_RESULTS_PER_KEYWORD resultaten per zoekwoord,
    zodat de database niet onbeperkt groeit."""
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                """
                DELETE FROM results
                WHERE keyword_id = ?
                  AND id NOT IN (
                      SELECT id FROM results
                      WHERE keyword_id = ?
                      ORDER BY first_seen_at DESC, id DESC
                      LIMIT ?
                  )
                """,
                (keyword_id, keyword_id, MAX_RESULTS_PER_KEYWORD),
            )
            if cur.rowcount:
                logger.info(
                    "%s oude resultaten opgeruimd voor zoekwoord %s",
                    cur.rowcount, keyword_id,
                )
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Telegram
# ------------------------------------------------------------------------------

def send_telegram_message(text: str, settings: dict) -> None:
    bot_id = (settings.get("telegram_bot_id") or "").strip()
    chat_id = (settings.get("telegram_chat_id") or "").strip()
    if not bot_id or not chat_id:
        return

    # Geen parse_mode: advertentietitels met _ * [ ] braken Markdown-parsing
    # waardoor Telegram het bericht stilletjes weigerde.
    url = f"https://api.telegram.org/bot{bot_id}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }
    try:
        resp = HTTP.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.warning("Telegram sendMessage mislukt (%s): %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Telegram sendMessage mislukt: %s", exc)


def send_telegram_ad(ad: dict, settings: dict) -> None:
    bot_id = (settings.get("telegram_bot_id") or "").strip()
    chat_id = (settings.get("telegram_chat_id") or "").strip()
    if not bot_id or not chat_id:
        return

    title = (ad.get("title") or "").strip()
    price = (ad.get("price") or "").strip()
    url = (ad.get("url") or "").strip()
    image_url = (ad.get("image_url") or "").strip()
    posted_at = (ad.get("posted_at") or "").strip()

    caption = f"Titel = {title}\nPrijs = {price}" + (f"\nDatum = {posted_at}" if posted_at else "")

    reply_markup = {"inline_keyboard": [[{"text": "Bekijk advertentie", "url": url}]]}

    try:
        if image_url:
            api_url = f"https://api.telegram.org/bot{bot_id}/sendPhoto"
            payload = {
                "chat_id": chat_id,
                "photo": image_url,
                "caption": caption,
                "reply_markup": reply_markup,
            }
        else:
            api_url = f"https://api.telegram.org/bot{bot_id}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": caption,
                "reply_markup": reply_markup,
            }
        resp = HTTP.post(api_url, json=payload, timeout=10)
        if not resp.ok:
            logger.warning(
                "Telegram-melding voor '%s' mislukt (%s): %s",
                title, resp.status_code, resp.text,
            )
    except Exception as exc:
        logger.warning("Telegram-melding voor '%s' mislukt: %s", title, exc)


# ------------------------------------------------------------------------------
# Search logic
# ------------------------------------------------------------------------------

def run_search_for_keyword(keyword: dict, settings: dict, manual: bool = False) -> tuple[int, int]:
    term = keyword["term"]
    limit_per_run = int(keyword.get("limit_per_run") or settings["default_limit_per_run"])
    limit_per_run = max(1, min(20, limit_per_run))
    min_price = _int_or_none(keyword.get("min_price"))
    max_price = _int_or_none(keyword.get("max_price"))

    raw_ads = fetch_market_results(term, settings, limit_per_run)

    # Prijsfilter op centen uit de API; advertenties zonder bruikbare prijs
    # ("Bieden", "Gratis") vallen weg zodra een grens is ingesteld.
    ads: list[dict] = []
    for ad in raw_ads:
        cents = ad.get("price_cents")
        if min_price is not None and (cents is None or cents < min_price * 100):
            continue
        if max_price is not None and (cents is None or cents > max_price * 100):
            continue
        ads.append(ad)

    # Alleen écht nieuwe advertenties verdienen de (dure) seller-fallback;
    # bekende ads krijgen hooguit een veld-update met verse API-data.
    known_ids = get_known_ad_ids(keyword["id"])
    new_candidates = [ad for ad in ads if ad["ad_id"] not in known_ids]
    known_ads = [ad for ad in ads if ad["ad_id"] in known_ids]

    enrich_ads_with_seller(new_candidates)

    # Blocklist ná de seller-verrijking, anders glippen verkopers door
    # waarvan de API geen naam meegeeft.
    blocked = get_blocklist(settings)
    if blocked:
        new_candidates = [
            ad for ad in new_candidates
            if (ad.get("seller") or "").strip().lower() not in blocked
        ]

    update_known_ads(keyword["id"], known_ads)
    new_ads = insert_new_ads(keyword["id"], new_candidates)
    prune_results_for_keyword(keyword["id"])

    notify = (not manual) or settings.get("manual_telegram")
    if notify:
        for ad in new_ads:
            if ad.get("_seen_elsewhere"):
                logger.info(
                    "Telegram overgeslagen voor '%s' (al gemeld via ander zoekwoord)",
                    ad.get("title"),
                )
                continue
            send_telegram_ad(ad, settings)

    return len(ads), len(new_ads)


# ------------------------------------------------------------------------------
# Background worker
# ------------------------------------------------------------------------------

_worker_thread_started = False
_worker_lock = threading.Lock()
_last_heartbeat: Optional[float] = None


def scheduler_loop():
    global _last_heartbeat
    while True:
        try:
            _last_heartbeat = time_module.monotonic()
            settings = load_settings()
            keywords = load_keywords()
            if not keywords:
                time_module.sleep(30)
                continue

            sleep_mode = bool(settings.get("sleep_mode"))
            sleep_start = parse_time_str(settings.get("sleep_start", "23:00"))
            sleep_end = parse_time_str(settings.get("sleep_end", "07:00"))

            now = datetime.now()
            now_t = now.time()
            in_sleep = sleep_mode and is_in_sleep_window(now_t, sleep_start, sleep_end)

            for kw in keywords:
                _last_heartbeat = time_module.monotonic()
                if not kw.get("term"):
                    continue

                interval_minutes = int(kw.get("interval_minutes") or settings["default_interval_minutes"])
                interval = timedelta(minutes=interval_minutes)
                eff_interval = timedelta(hours=1) if (in_sleep and interval < timedelta(hours=1)) else interval

                last_run_at_str = kw.get("last_run_at") or "Nooit"
                last_dt = None
                if last_run_at_str != "Nooit":
                    try:
                        last_dt = datetime.fromisoformat(last_run_at_str)
                    except Exception:
                        last_dt = None

                if (last_dt is None) or (now - last_dt >= eff_interval):
                    try:
                        total, new_count = run_search_for_keyword(kw, settings, manual=False)
                        logger.info(
                            "Zoekactie '%s': %s resultaten, %s nieuw",
                            kw["term"], total, new_count,
                        )
                        update_keyword_fields(
                            kw["id"],
                            last_run_at=datetime.now().isoformat(timespec="seconds"),
                        )
                    except Exception:
                        logger.exception("Zoekactie voor '%s' mislukt", kw.get("term"))
                        continue

            time_module.sleep(60)
        except Exception:
            logger.exception("Onverwachte fout in scheduler")
            time_module.sleep(60)


def start_background_worker():
    global _worker_thread_started
    if os.environ.get("MPWATCHER_DISABLE_WORKER") == "1":
        return
    with _worker_lock:
        if not _worker_thread_started:
            init_db()
            t = threading.Thread(target=scheduler_loop, daemon=True)
            t.start()
            _worker_thread_started = True
            logger.info("Achtergrondworker gestart (config: %s)", CONFIG_DIR)


# ------------------------------------------------------------------------------
# Routes – UI
# ------------------------------------------------------------------------------

@app.route("/")
def index():
    settings = load_settings()
    keywords = load_keywords()

    for kw in keywords:
        kw["mp_url"] = build_search_url(kw["term"], settings)

    return render_template(
        "index.html",
        keywords=keywords,
        default_interval=settings["default_interval_minutes"],
        default_limit_per_run=settings["default_limit_per_run"],
        settings=settings,
    )


@app.route("/keyword/add", methods=["POST"])
def add_keyword():
    settings = load_settings()

    term = (request.form.get("term") or "").strip()
    if not term:
        flash("Zoekwoord mag niet leeg zijn.", "error")
        return redirect(url_for("index"))

    add_keyword_row(
        term=term,
        interval_minutes=settings["default_interval_minutes"],
        min_price=_int_or_none(request.form.get("min_price")),
        max_price=_int_or_none(request.form.get("max_price")),
        limit_per_run=settings["default_limit_per_run"],
    )
    flash(f"Zoekwoord '{term}' toegevoegd.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/edit", methods=["POST"])
def edit_keyword(keyword_id: int):
    fields: dict = {}

    term = (request.form.get("term") or "").strip()
    if term:
        fields["term"] = term

    interval = request.form.get("interval")
    if interval:
        try:
            fields["interval_minutes"] = max(1, int(interval))
        except ValueError:
            pass

    if "min_price" in request.form:
        fields["min_price"] = _int_or_none(request.form.get("min_price"))
    if "max_price" in request.form:
        fields["max_price"] = _int_or_none(request.form.get("max_price"))

    limit_per_run = request.form.get("limit_per_run")
    if limit_per_run:
        try:
            fields["limit_per_run"] = max(1, min(20, int(limit_per_run)))
        except ValueError:
            pass

    if not update_keyword_fields(keyword_id, **fields):
        flash("Zoekwoord niet gevonden.", "error")

    return redirect(url_for("index"))  # silent save


@app.route("/keyword/<int:keyword_id>/manual", methods=["POST"])
def manual_search(keyword_id: int):
    settings = load_settings()
    kw = get_keyword(keyword_id)
    if not kw:
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    total, new_count = run_search_for_keyword(kw, settings, manual=True)
    update_keyword_fields(keyword_id, last_run_at=datetime.now().isoformat(timespec="seconds"))

    flash(
        f"Handmatige zoekactie voor '{kw['term']}' uitgevoerd ({total} resultaten, {new_count} nieuw).",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/reset", methods=["POST"])
def reset_keyword(keyword_id: int):
    kw = get_keyword(keyword_id)
    if not kw:
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    reset_results_for_keyword(keyword_id)
    update_keyword_fields(keyword_id, last_run_at=None)
    flash(f"Resultaten voor '{kw['term']}' zijn gereset.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/delete", methods=["POST"])
def delete_keyword(keyword_id: int):
    if not delete_keyword_row(keyword_id):
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    reset_results_for_keyword(keyword_id)
    flash("Zoekwoord verwijderd.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/results")
def results(keyword_id: int):
    settings = load_settings()
    kw = get_keyword(keyword_id)
    if not kw:
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    ads = get_results_for_keyword(keyword_id, limit=200)

    return render_template(
        "results.html",
        keyword=kw,
        ads=ads,
        settings=settings,
    )


# ------------------------------------------------------------------------------
# Blocklist routes
# ------------------------------------------------------------------------------

@app.route("/blocklist/save", methods=["POST"])
def blocklist_save():
    enabled = _form_bool(request.form.get("blocklist_enabled"))
    raw = request.form.get("blocked_sellers_text", "") or ""
    lines = [x.strip() for x in raw.splitlines()]

    cleaned = []
    seen = set()
    for x in lines:
        if not x:
            continue
        xl = x.lower()
        if xl in seen:
            continue
        seen.add(xl)
        cleaned.append(x)

    save_settings({"blocklist_enabled": enabled, "blocked_sellers": cleaned})

    flash("Blocklist opgeslagen.", "success")
    return redirect(url_for("config_view"))


@app.route("/blocklist/add", methods=["POST"])
def blocklist_add():
    seller = _norm_name(request.form.get("seller", ""))
    keyword_id = request.form.get("keyword_id", "")

    if seller:
        settings = load_settings()
        current = settings.get("blocked_sellers") or []
        existing_lower = {str(x).strip().lower() for x in current if str(x).strip()}
        if seller.lower() not in existing_lower:
            current.append(seller)
        save_settings({"blocklist_enabled": True, "blocked_sellers": current})
        flash(f"Verkoper '{seller}' geblokkeerd.", "success")

    try:
        kid = int(keyword_id)
        return redirect(url_for("results", keyword_id=kid))
    except Exception:
        return redirect(url_for("config_view"))


# ------------------------------------------------------------------------------
# Configuratie UI
# ------------------------------------------------------------------------------

@app.route("/config", methods=["GET"])
def config_view():
    settings = load_settings()
    blocked = settings.get("blocked_sellers") or []
    blocked_text = "\n".join(blocked)
    return render_template("config.html", settings=settings, blocked_text=blocked_text)


@app.route("/config/timer", methods=["POST"])
def config_save_timer():
    updates: dict = {}

    marketplace = (request.form.get("marketplace") or "marktplaats").strip().lower()
    updates["marketplace"] = "2dehands" if marketplace == "2dehands" else "marktplaats"

    default_interval = _int_or_none(request.form.get("default_interval_minutes"))
    if default_interval is not None:
        updates["default_interval_minutes"] = max(1, default_interval)

    default_limit = _int_or_none(request.form.get("default_limit_per_run"))
    if default_limit is not None:
        updates["default_limit_per_run"] = max(1, min(20, default_limit))

    updates["sleep_mode"] = _form_bool(request.form.get("sleep_mode"))
    updates["sleep_start"] = request.form.get("sleep_start") or "23:00"
    updates["sleep_end"] = request.form.get("sleep_end") or "07:00"
    updates["postcode"] = (request.form.get("postcode") or "").strip()
    updates["radius_km"] = request.form.get("radius_km") or "alle"

    save_settings(updates)
    flash("Instellingen opgeslagen.", "success")
    return redirect(url_for("config_view"))


@app.route("/config/telegram", methods=["POST"])
def config_save_telegram():
    save_settings({
        "telegram_bot_id": (request.form.get("telegram_bot_id") or "").strip(),
        "telegram_chat_id": (request.form.get("telegram_chat_id") or "").strip(),
        "manual_telegram": _form_bool(request.form.get("manual_telegram")),
    })
    flash("Telegram-instellingen opgeslagen.", "success")
    return redirect(url_for("config_view"))


@app.route("/config/telegram/test", methods=["POST"])
def config_test_telegram():
    settings = load_settings()
    send_telegram_message("✅ Testbericht van MPWatcher", settings)
    flash("Testbericht naar Telegram verstuurd (indien juist geconfigureerd).", "success")
    return redirect(url_for("config_view"))


# ------------------------------------------------------------------------------
# Healthcheck
# ------------------------------------------------------------------------------

@app.route("/health")
def health():
    if _worker_thread_started and _last_heartbeat is not None:
        age = time_module.monotonic() - _last_heartbeat
        if age > SCHEDULER_STALE_SECONDS:
            return {"status": "unhealthy", "scheduler_stale_seconds": int(age)}, 503
    return {"status": "ok"}


# ------------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------------

init_db()
start_background_worker()

if __name__ == "__main__":
    # Alleen voor lokaal draaien; in de container draait gunicorn (zie Dockerfile).
    # debug=True is bewust uit: de Werkzeug-debugger geeft remote code execution
    # en de auto-reloader start een tweede scheduler (dubbele meldingen).
    app.run(host="0.0.0.0", port=8000, debug=False)

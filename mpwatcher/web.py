"""Flask-app en routes."""
import time as time_module
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from . import db
from . import marketplace as mp
from . import notify
from . import scheduler
from .config import (
    APP_VERSION,
    BASE_DIR,
    CONFIG_CHECK_TTL_SECONDS,
    RESULTS_PAGE_SIZE,
    MarketplaceError,
    logger,
)
from .utils import (
    clean_lines,
    form_bool,
    format_relative_time,
    int_or_none,
    normalize_terms,
    normalize_title,
)

app = Flask(
    "mpwatcher",
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = __import__("os").environ.get("FLASK_SECRET_KEY", "mpwatcher-dev")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


@app.context_processor
def _inject_globals():
    return {"app_version": APP_VERSION}


@app.before_request
def _block_cross_site_posts():
    """Eenvoudige CSRF-bescherming: een POST vanaf een andere site (Origin/Referer
    met een andere host) wordt geweigerd. Requests zonder deze headers (curl,
    scripts) blijven toegestaan."""
    if request.method != "POST":
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return None
    source_host = urlparse(source).netloc
    if source_host and source_host != request.host:
        logger.warning("Cross-site POST geweigerd vanaf %s naar %s", source_host, request.path)
        abort(403)
    return None


def _flash_not_found():
    flash("Zoekwoord niet gevonden.", "error")
    return redirect(url_for("index"))


# ------------------------------------------------------------------------------
# Overzicht
# ------------------------------------------------------------------------------

@app.route("/")
def index():
    settings = db.load_settings()
    keywords = db.load_keywords()
    counts = db.get_result_counts()
    now = datetime.now()

    for kw in keywords:
        kw["mp_url"] = mp.build_search_url(kw["term"], settings)
        kw["result_count"] = counts.get(kw["id"], 0)
        kw["last_run_display"] = format_relative_time(kw["last_run_at"])
        kw["next_run_display"] = scheduler.format_next_run(scheduler.compute_next_run(kw, settings, now), now)

    recent = db.get_recent_results(limit=10)
    for ad in recent:
        ad["first_seen_display"] = format_relative_time(ad.get("first_seen_at"))

    return render_template(
        "index.html",
        keywords=keywords,
        recent=recent,
        default_interval=settings["default_interval_minutes"],
        default_limit_per_run=settings["default_limit_per_run"],
        settings=settings,
        scheduler=scheduler.get_scheduler_status(),
        telegram_configured=bool(
            (settings.get("telegram_bot_id") or "").strip()
            and (settings.get("telegram_chat_id") or "").strip()
        ),
        in_sleep=scheduler.in_sleep_now(settings, now),
        error_count=sum(1 for kw in keywords if kw.get("last_error")),
    )


# ------------------------------------------------------------------------------
# Zoekwoorden
# ------------------------------------------------------------------------------

@app.route("/keyword/add", methods=["POST"])
def add_keyword():
    settings = db.load_settings()

    term = (request.form.get("term") or "").strip()
    if not term:
        flash("Zoekwoord mag niet leeg zijn.", "error")
        return redirect(url_for("index"))
    if db.keyword_exists(term):
        flash(f"Zoekwoord '{term}' bestaat al.", "error")
        return redirect(url_for("index"))

    kid = db.add_keyword_row(
        term=term,
        interval_minutes=settings["default_interval_minutes"],
        min_price=int_or_none(request.form.get("min_price")),
        max_price=int_or_none(request.form.get("max_price")),
        limit_per_run=settings["default_limit_per_run"],
        exclude_terms=request.form.get("exclude_terms", ""),
        include_terms=request.form.get("include_terms", ""),
    )

    # Direct een eerste (stille) zoekactie zodat de resultatenpagina meteen
    # gevuld is — zonder Telegram-burst van bestaande advertenties.
    kw = db.get_keyword(kid)
    try:
        total, _ = scheduler.run_search_for_keyword(kw, settings, manual=True)
        scheduler.record_search_success(kid)
        flash(f"Zoekwoord '{term}' toegevoegd; {total} bestaande advertenties stil opgeslagen.", "success")
    except MarketplaceError as exc:
        scheduler.record_search_failure(kw, str(exc))
        flash(f"Zoekwoord '{term}' toegevoegd, maar de eerste zoekactie mislukte: {exc}", "error")
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
        fields["min_price"] = int_or_none(request.form.get("min_price"))
    if "max_price" in request.form:
        fields["max_price"] = int_or_none(request.form.get("max_price"))

    limit_per_run = request.form.get("limit_per_run")
    if limit_per_run:
        try:
            fields["limit_per_run"] = max(1, min(20, int(limit_per_run)))
        except ValueError:
            pass

    if "exclude_terms" in request.form:
        fields["exclude_terms"] = normalize_terms(request.form.get("exclude_terms"))
    if "include_terms" in request.form:
        fields["include_terms"] = normalize_terms(request.form.get("include_terms"))

    if not db.update_keyword_fields(keyword_id, **fields):
        flash("Zoekwoord niet gevonden.", "error")

    return redirect(url_for("index"))  # silent save


@app.route("/keyword/<int:keyword_id>/manual", methods=["POST"])
def manual_search(keyword_id: int):
    settings = db.load_settings()
    kw = db.get_keyword(keyword_id)
    if not kw:
        return _flash_not_found()

    try:
        total, new_count = scheduler.run_search_for_keyword(kw, settings, manual=True)
    except MarketplaceError as exc:
        scheduler.record_search_failure(kw, str(exc))
        flash(f"Zoekactie voor '{kw['term']}' mislukt: {exc}", "error")
        return redirect(url_for("index"))

    scheduler.record_search_success(keyword_id)
    flash(
        f"Handmatige zoekactie voor '{kw['term']}' uitgevoerd ({total} resultaten, {new_count} nieuw).",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/reset", methods=["POST"])
def reset_keyword(keyword_id: int):
    kw = db.get_keyword(keyword_id)
    if not kw:
        return _flash_not_found()

    db.reset_results_for_keyword(keyword_id)
    db.update_keyword_fields(keyword_id, last_run_at=None)
    flash(f"Resultaten voor '{kw['term']}' zijn gereset.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/delete", methods=["POST"])
def delete_keyword(keyword_id: int):
    if not db.delete_keyword_row(keyword_id):
        return _flash_not_found()

    db.reset_results_for_keyword(keyword_id)
    flash("Zoekwoord verwijderd.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/results")
def results(keyword_id: int):
    settings = db.load_settings()
    kw = db.get_keyword(keyword_id)
    if not kw:
        return _flash_not_found()

    query = (request.args.get("q") or "").strip()
    offset = max(0, int_or_none(request.args.get("offset")) or 0)

    total = db.count_results_for_keyword(keyword_id, query)
    ads = db.get_results_for_keyword(keyword_id, limit=RESULTS_PAGE_SIZE, offset=offset, query=query)

    return render_template(
        "results.html",
        keyword=kw,
        ads=ads,
        settings=settings,
        query=query,
        total=total,
        offset=offset,
        page_size=RESULTS_PAGE_SIZE,
        prev_offset=max(0, offset - RESULTS_PAGE_SIZE) if offset > 0 else None,
        next_offset=offset + RESULTS_PAGE_SIZE if offset + RESULTS_PAGE_SIZE < total else None,
    )


# ------------------------------------------------------------------------------
# Meldingenlogboek
# ------------------------------------------------------------------------------

@app.route("/notifications")
def notifications():
    items = db.get_notifications(limit=200)
    for it in items:
        it["created_display"] = format_relative_time(it.get("created_at"))
    return render_template("notifications.html", items=items)


# ------------------------------------------------------------------------------
# Categorie per zoekwoord
# ------------------------------------------------------------------------------

@app.route("/keyword/<int:keyword_id>/categories")
def keyword_categories(keyword_id: int):
    settings = db.load_settings()
    kw = db.get_keyword(keyword_id)
    if not kw:
        return _flash_not_found()

    categories: list[dict] = []
    try:
        categories = mp.fetch_categories(kw["term"], settings)
    except MarketplaceError as exc:
        flash(f"Categorieën ophalen mislukt: {exc}", "error")

    return render_template("categories.html", keyword=kw, categories=categories)


@app.route("/keyword/<int:keyword_id>/category", methods=["POST"])
def keyword_set_category(keyword_id: int):
    kw = db.get_keyword(keyword_id)
    if not kw:
        return _flash_not_found()

    cid = int_or_none(request.form.get("category_id"))
    if cid is None:
        db.update_keyword_fields(keyword_id, category_id=None, category_parent_id=None, category_label="")
        flash(f"Categoriefilter voor '{kw['term']}' verwijderd.", "success")
    else:
        parent = int_or_none(request.form.get("category_parent_id"))
        label = (request.form.get("category_label") or str(cid)).strip()[:120]
        db.update_keyword_fields(keyword_id, category_id=cid, category_parent_id=parent, category_label=label)
        flash(f"'{kw['term']}' zoekt voortaan alleen in categorie '{label}'.", "success")
    return redirect(url_for("index"))


# ------------------------------------------------------------------------------
# Blocklists
# ------------------------------------------------------------------------------

@app.route("/blocklist/save", methods=["POST"])
def blocklist_save():
    db.save_settings({
        "blocklist_enabled": form_bool(request.form.get("blocklist_enabled")),
        "blocked_sellers": clean_lines(request.form.get("blocked_sellers_text")),
    })
    flash("Blocklist verkopers opgeslagen.", "success")
    return redirect(url_for("config_view"))


@app.route("/blocklist/add", methods=["POST"])
def blocklist_add():
    seller = (request.form.get("seller") or "").strip()
    keyword_id = int_or_none(request.form.get("keyword_id"))

    if seller:
        settings = db.load_settings()
        current = settings.get("blocked_sellers") or []
        if seller.lower() not in {str(x).strip().lower() for x in current}:
            current.append(seller)
        db.save_settings({"blocklist_enabled": True, "blocked_sellers": current})
        flash(f"Verkoper '{seller}' geblokkeerd.", "success")

    if keyword_id is not None:
        return redirect(url_for("results", keyword_id=keyword_id))
    return redirect(url_for("config_view"))


@app.route("/blocklist/titles/save", methods=["POST"])
def blocklist_titles_save():
    titles = clean_lines(request.form.get("blocked_titles_text"))
    db.save_settings({"blocked_titles": titles})
    removed = db.delete_results_with_titles(titles)
    flash(
        f"Genegeerde advertenties opgeslagen ({len(titles)} titels"
        + (f", {removed} bestaande resultaten verwijderd" if removed else "") + ").",
        "success",
    )
    return redirect(url_for("config_view"))


@app.route("/blocklist/title/add", methods=["POST"])
def blocklist_title_add():
    title = (request.form.get("title") or "").strip()
    keyword_id = int_or_none(request.form.get("keyword_id"))

    if title:
        settings = db.load_settings()
        current = settings.get("blocked_titles") or []
        if normalize_title(title) not in {normalize_title(x) for x in current}:
            current.append(title)
        db.save_settings({"blocked_titles": current})
        removed = db.delete_results_with_titles([title])
        flash(
            f"Advertentie '{title[:60]}' wordt voortaan genegeerd"
            + (f" ({removed} resultaten verwijderd)" if removed else "") + ".",
            "success",
        )

    if keyword_id is not None:
        return redirect(url_for("results", keyword_id=keyword_id))
    return redirect(url_for("config_view"))


# ------------------------------------------------------------------------------
# Configuratie
# ------------------------------------------------------------------------------

@app.route("/config", methods=["GET"])
def config_view():
    settings = db.load_settings()
    return render_template(
        "config.html",
        settings=settings,
        blocked_text="\n".join(settings.get("blocked_sellers") or []),
        blocked_titles_text="\n".join(settings.get("blocked_titles") or []),
    )


@app.route("/config/timer", methods=["POST"])
def config_save_timer():
    updates: dict = {}

    marketplace = (request.form.get("marketplace") or "marktplaats").strip().lower()
    updates["marketplace"] = "2dehands" if marketplace == "2dehands" else "marktplaats"

    default_interval = int_or_none(request.form.get("default_interval_minutes"))
    if default_interval is not None:
        updates["default_interval_minutes"] = max(1, default_interval)

    default_limit = int_or_none(request.form.get("default_limit_per_run"))
    if default_limit is not None:
        updates["default_limit_per_run"] = max(1, min(20, default_limit))

    repost = int_or_none(request.form.get("repost_dedup_days"))
    if repost is not None:
        updates["repost_dedup_days"] = max(0, min(365, repost))

    updates["skip_reserved"] = form_bool(request.form.get("skip_reserved"))
    updates["sleep_mode"] = form_bool(request.form.get("sleep_mode"))
    updates["sleep_start"] = request.form.get("sleep_start") or "23:00"
    updates["sleep_end"] = request.form.get("sleep_end") or "07:00"
    updates["postcode"] = (request.form.get("postcode") or "").strip()
    updates["radius_km"] = request.form.get("radius_km") or "alle"

    db.save_settings(updates)
    flash("Instellingen opgeslagen.", "success")
    return redirect(url_for("config_view"))


@app.route("/config/telegram", methods=["POST"])
def config_save_telegram():
    digest = int_or_none(request.form.get("telegram_digest_threshold"))
    db.save_settings({
        "telegram_bot_id": (request.form.get("telegram_bot_id") or "").strip(),
        "telegram_chat_id": (request.form.get("telegram_chat_id") or "").strip(),
        "manual_telegram": form_bool(request.form.get("manual_telegram")),
        "price_drop_alerts": form_bool(request.form.get("price_drop_alerts")),
        "telegram_digest_threshold": max(0, digest) if digest is not None else 5,
    })
    flash("Telegram-instellingen opgeslagen.", "success")
    return redirect(url_for("config_view"))


@app.route("/config/telegram/test", methods=["POST"])
def config_test_telegram():
    settings = db.load_settings()
    notify.send_telegram_message("✅ Testbericht van MPWatcher", settings)
    flash("Testbericht naar Telegram verstuurd (indien juist geconfigureerd).", "success")
    return redirect(url_for("config_view"))


# ------------------------------------------------------------------------------
# Healthcheck
# ------------------------------------------------------------------------------

_config_check: Optional[tuple[float, bool]] = None  # (monotonic-tijd, resultaat)


def _config_writable_cached() -> bool:
    """De schrijftest hoeft niet elke 30 s (Docker-healthcheck); cache het resultaat."""
    global _config_check
    now = time_module.monotonic()
    if _config_check is not None and now - _config_check[0] < CONFIG_CHECK_TTL_SECONDS:
        return _config_check[1]
    ok = db.config_writable()
    _config_check = (now, ok)
    return ok


@app.route("/health")
def health():
    if not _config_writable_cached():
        return {"status": "unhealthy", "reason": "config-dir niet schrijfbaar", "version": APP_VERSION}, 503
    status = scheduler.get_scheduler_status()
    if status["state"] == "stale":
        return {
            "status": "unhealthy",
            "scheduler_stale_seconds": status["age_seconds"],
            "version": APP_VERSION,
        }, 503
    return {"status": "ok", "version": APP_VERSION, "scheduler": status["state"]}

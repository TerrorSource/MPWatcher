"""Kleine hulpfuncties zonder afhankelijkheden op de rest van de app."""
import re
from datetime import datetime
from typing import Optional


def form_bool(v) -> bool:
    """Formulier-selects sturen 'ja'/'nee'; intern werken we met booleans."""
    return str(v or "").strip().lower() == "ja"


def int_or_none(v) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_terms(raw: str | None) -> list[str]:
    """'Gezocht, gevraagd ,,' -> ['gezocht', 'gevraagd'] (voor titelfilters)."""
    if not raw:
        return []
    seen: list[str] = []
    for part in str(raw).split(","):
        t = part.strip().lower()
        if t and t not in seen:
            seen.append(t)
    return seen


def normalize_terms(raw: str | None) -> str:
    return ", ".join(parse_terms(raw))


def title_passes_filters(title: str, include_terms: str | None, exclude_terms: str | None) -> bool:
    """Uitsluitwoorden: één match in de titel is genoeg om de advertentie te
    negeren. Verplichte woorden: minstens één ervan moet in de titel staan."""
    t = (title or "").lower()
    for word in parse_terms(exclude_terms):
        if word in t:
            return False
    required = parse_terms(include_terms)
    if required and not any(word in t for word in required):
        return False
    return True


_WS = re.compile(r"\s+")


def normalize_title(title: str | None) -> str:
    """Voor titel-blocklist en herplaatsings-dedup: kleine letters, enkele spaties."""
    return _WS.sub(" ", (title or "").strip()).casefold()


def clean_lines(raw: str | None) -> list[str]:
    """Tekstvak met één item per regel -> lijst zonder lege regels en dubbelen."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        x = line.strip()
        if not x:
            continue
        key = normalize_title(x)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(x)
    return cleaned


def format_cents(cents: int) -> str:
    return f"€ {cents/100:.2f}".replace(".", ",")


def format_relative_time(iso_str: str | None) -> str:
    """'2026-06-12T14:03:00' -> '5 min geleden' (voor het overzicht)."""
    if not iso_str or iso_str == "Nooit":
        return "Nooit"
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return iso_str

    secs = int((datetime.now() - dt).total_seconds())
    if secs < 60:
        return "zojuist"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min geleden"
    hours = mins // 60
    if hours < 24:
        return f"{hours} uur geleden"
    days = hours // 24
    if days == 1:
        return "gisteren"
    if days < 7:
        return f"{days} dagen geleden"
    return dt.strftime("%d-%m-%Y")

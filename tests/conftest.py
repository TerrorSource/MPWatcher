import os
import sys
import tempfile
from pathlib import Path

import pytest

# Tests draaien tegen een wegwerp-configmap en zonder schedulerthread,
# zodat er geen netwerkverkeer of achtergrondwerk plaatsvindt.
os.environ["MPWATCHER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="mpwatcher-test-")
os.environ["MPWATCHER_DISABLE_WORKER"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mpwatcher import marketplace as _marketplace  # noqa: E402

# De echte functie, voor tests die de client zelf willen testen (met gemockte _api_get).
_REAL_FETCH_MARKET_RESULTS = _marketplace.fetch_market_results


@pytest.fixture()
def real_fetch_market_results():
    return _REAL_FETCH_MARKET_RESULTS


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Geen echte calls naar Marktplaats/Telegram vanuit de tests.
    Individuele tests kunnen deze mocks zelf weer overschrijven."""
    from mpwatcher import marketplace, notify

    monkeypatch.setattr(
        marketplace, "fetch_market_results",
        lambda term, settings, limit, category_id=None, category_parent_id=None: [],
    )
    monkeypatch.setattr(marketplace, "fetch_seller_from_ad_page", lambda url: "")
    monkeypatch.setattr(notify, "_telegram_post", lambda method, payload, settings, label="": True)


@pytest.fixture()
def client():
    import app  # noqa: F401  — initialiseert de database, worker staat uit
    from mpwatcher import db

    db.init_db()
    return app.app.test_client()

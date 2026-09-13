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


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Geen echte calls naar Marktplaats/Telegram vanuit de tests.
    Individuele tests kunnen deze mocks zelf weer overschrijven."""
    import app

    monkeypatch.setattr(app, "fetch_market_results", lambda term, settings, limit: [])
    monkeypatch.setattr(app, "fetch_seller_from_ad_page", lambda url: "")
    monkeypatch.setattr(app, "_telegram_post", lambda method, payload, settings, label="": True)

"""Entrypoint: `gunicorn app:app` (container) of `python app.py` (lokaal).

De code zelf staat in het package `mpwatcher/`:
  config · utils · db · marketplace · notify · scheduler · web
"""
from mpwatcher import db, scheduler
from mpwatcher.config import CONFIG_DIR, logger
from mpwatcher.web import app  # noqa: F401  — gunicorn zoekt `app`

db.init_db()
scheduler.start_background_worker()
logger.info("MPWatcher gestart (config: %s)", CONFIG_DIR)

if __name__ == "__main__":
    # Alleen voor lokaal draaien; in de container draait gunicorn (zie Dockerfile).
    # debug=True is bewust uit: de Werkzeug-debugger geeft remote code execution
    # en de auto-reloader start een tweede scheduler (dubbele meldingen).
    app.run(host="0.0.0.0", port=8000, debug=False)

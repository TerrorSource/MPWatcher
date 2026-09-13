"""MPWatcher — Marktplaats/2dehands advertentie-watcher.

Modules:
  config      constanten, paden, logger, default-instellingen
  utils       kleine hulpfuncties (formattering, filters)
  db          SQLite: migraties, instellingen, zoekwoorden, resultaten
  marketplace API-client en parsing van advertenties
  notify      Telegram
  scheduler   zoeklogica en de achtergrondworker
  web         Flask-routes
"""

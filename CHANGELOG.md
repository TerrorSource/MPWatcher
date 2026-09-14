# Changelog

Per versie de belangrijkste wijzigingen. De sectie van de actuele versie wordt
door `commit-git.sh -r` als release-tekst op GitHub gebruikt.

## v25

- **Trivy blokkeert nu écht**: het image wordt eerst lokaal gebouwd en gescand; alleen zonder CRITICAL-kwetsbaarheden (met fix beschikbaar) wordt het multi-arch gebouwd en gepusht. HIGH wordt informatief gerapporteerd.
- **Image-hardening**: Debian-security-updates en een actuele pip/setuptools worden tijdens de build geïnstalleerd (loste 3 CRITICAL en 11 HIGH uit de eerste Trivy-scan op).
- **Meerdere Telegram-ontvangers**: het chat-id-veld accepteert een komma-gescheiden lijst; per zoekwoord kun je een eigen chat (of lijst) instellen.
- **Kenmerkfilter per zoekwoord**: minstens één opgegeven woord moet in de kenmerken (conditie, maat, …) voorkomen.
- **Meldingen filteren** op soort, zoekwoord en tekst.
- **Nieuw sinds je laatste bezoek**: op de resultatenpagina en in het recent-blok worden advertenties gemarkeerd die zijn bijgekomen sinds je die pagina voor het laatst opende (per browser, via localStorage).
- **Marketplace per zoekwoord**: Marktplaats.nl of 2dehands.be per zoekwoord, los van de globale keuze.

## v24

- Fix: de Trivy-scan in CI verwees naar een niet-bestaande versie-tag (`0.28.0`); nu `v0.36.0`. Geen functionele wijzigingen.

## v23

- **Verlopen advertenties**: advertenties van de afgelopen 7 dagen (instelbaar) worden dagelijks gecontroleerd; verwijderde exemplaren (HTTP 410) krijgen het label *verlopen* in resultaten en overzicht.
- **Verkopersprofiel**: de verkopersnaam in de resultaten linkt naar het Marktplaats-profiel (andere advertenties, beoordelingen).
- **Kenmerken en omschrijving**: conditie/maat e.d. onder de titel en in de Telegram-melding; de eerste regels van de omschrijving in de melding en als tooltip op de titel.
- **Nu zoeken** op de resultatenpagina.
- Favicon (SVG) en app-icoon voor het beginscherm van je telefoon; geen 404 meer op `/favicon.ico`.
- De aan/uit-schakelaar van de verkopers-blocklist is weg: een lege lijst betekent uit, net als bij genegeerde titels.
- Beheer: Trivy-kwetsbaarheidsscan van het image in CI (informatief); test voor de Telegram-retry.

## v22

- **Backoff bij blokkade**: na een mislukte zoekopdracht (bijv. HTTP 403/429) wordt het interval van dat zoekwoord per mislukking verdubbeld (max 6 uur) in plaats van elk interval opnieuw te hameren. Herstelt vanzelf na een geslaagde run.
- **Meldingenlogboek** (nieuwe pagina *Meldingen*): wat er via Telegram is gestuurd én wat is onderdrukt en waarom (herplaatsing, al gemeld via ander zoekwoord, stille eerste run).
- **Gereserveerd**: gereserveerde advertenties krijgen een label; optioneel volledig overslaan (Configuratie → Zoekinstellingen).
- **Afstand**: bij een ingestelde postcode staat de afstand bij de plaats ("Utrecht · 12 km"), ook in Telegram.
- Dubbele zoekwoorden worden geweigerd.
- User-Agent bijgewerkt en instelbaar via `MPWATCHER_USER_AGENT`; `Accept-Language` meegestuurd.
- Beheer: lint (ruff) in CI, lokale testgate in `commit-git.sh`, release-notes uit dit bestand, nieuwe screenshot.

## v21

- Code opgesplitst in het package `mpwatcher/` (config, utils, db, marketplace, notify, scheduler, web).
- **Advertenties negeren op titel** (Negeren → Advertentie) naast het blokkeren van verkopers; genegeerde titels verdwijnen ook uit bestaande resultaten.
- **Herplaatsings-dedup**: dezelfde titel/verkoper/prijs met een nieuw advertentie-id binnen 30 dagen (instelbaar) wordt niet nogmaals gemeld.
- **Categoriefilter** per zoekwoord, gekozen uit de live categorielijst van de marketplace (hoofdcategorie via de API, subcategorie lokaal).
- Overzicht toont de laatste 10 nieuwe advertenties over alle zoekwoorden.
- Resultatenpagina: zoeken (titel/verkoper/plaats) en bladeren door de volledige historie.
- Telegram-meldingen vermelden het zoekwoord.
- Fixes: datums als "Vandaag"/"Gisteren" worden geparsed; "Zie omschrijving"/"Bieden" werd als € 0,00 getoond.
- Docker-hardening in compose: `read_only`, `tmpfs /tmp`, `cap_drop ALL`, `no-new-privileges`.
- Schedulerlus los getrokken en getest.

## v20

- Titelfilters per zoekwoord: uitsluitwoorden en verplichte woorden.
- Stille eerste run bij een nieuw zoekwoord (geen Telegram-burst van oude advertenties); toevoegen zoekt direct.
- Echte statuskaart (scheduler-hartslag, Telegram, slaapstand, fouten) en per zoekwoord de volgende run en de laatste fout.
- Telegram: retry bij rate-limiting, plaats van de verkoper in meldingen, samenvatting vanaf N nieuwe advertenties.
- Bevestiging bij Verwijderen/Reset; resultatenpagina werkt op mobiel.
- Tests in CI vóór de image-build; Dependabot-bumps (Python 3.13, gunicorn 26, Flask 3.1.3, …).
- CSRF-bescherming via Origin/Referer-check.
- `Dockerfile` hernoemd (was `dockerfile`); `commit-git.sh` waarschuwt bij upstream-commits.

## v19

- Multi-arch image (amd64 + arm64), CI-buildcache, versienummer in footer en `/health`.
- Prijsverlaging-meldingen, thumbnails, bewerkbare zoekterm, resultaataantallen, relatieve tijden.
- `/health` controleert ook of `/config` schrijfbaar is; spreiding van zoekacties.

## v18

- Naam-typo "MPWatchter" overal hersteld; instellingen en zoekwoorden naar SQLite (automatische migratie).
- gunicorn i.p.v. de Flask-debugserver; non-root container; healthcheck; logging.
- Blocklist ná seller-verrijking; prijsfilter op `priceCents`; cross-keyword dedup; testsuite.
